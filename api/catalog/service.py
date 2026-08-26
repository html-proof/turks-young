import asyncio
import base64
import json
import logging
import time
from typing import Any

from pydantic import TypeAdapter, ValidationError

from api.catalog.models import Language
from api.catalog.normalize import album, artist, items, playlist, song
from api.catalog.search.ranking import confidence, normalize_query, rank, _strong_match
from api.core import config

logger = logging.getLogger(__name__)


def _apply_album_artwork(
    songs: list[dict[str, Any]],
    albums: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach the matching album cover to songs without guessing by artist."""
    artwork_by_key: dict[str, str] = {}
    for value in albums:
        artwork = str(
            value.get("artworkUrl") or value.get("image_url")
            or value.get("imageUrl") or ""
        ).strip()
        if not artwork:
            continue
        for identity in (value.get("id"), value.get("provider_id")):
            if identity:
                artwork_by_key[f"id:{identity}"] = artwork
        title = normalize_query(str(value.get("title") or value.get("name") or ""))
        if title:
            artwork_by_key[f"title:{title}"] = artwork

    hydrated: list[dict[str, Any]] = []
    for value in songs:
        result = dict(value)
        album_value = result.get("album") if isinstance(result.get("album"), dict) else {}
        keys = [
            f"id:{album_value.get('id')}",
            f"id:{album_value.get('provider_id')}",
            f"title:{normalize_query(str(album_value.get('title') or album_value.get('name') or ''))}",
        ]
        artwork = next((artwork_by_key[key] for key in keys if key in artwork_by_key), None)
        if artwork:
            result["image_url"] = artwork
            result["artworkUrl"] = artwork
            if album_value:
                result["album"] = {**album_value, "artworkUrl": artwork}
        hydrated.append(result)
    return hydrated


class LanguageCatalog:
    _STANDARD_CODES = {
        "malayalam": "ml", "tamil": "ta", "hindi": "hi", "english": "en",
        "telugu": "te", "kannada": "kn", "bengali": "bn", "punjabi": "pa",
        "marathi": "mr", "gujarati": "gu", "odia": "or",
    }
    def __init__(self, languages: list[Language]):
        self.languages = languages
        self.by_id = {language.id: language for language in languages}
        self._aliases: dict[str, str] = {}
        for language in languages:
            values = {language.id, language.name, language.native_name}
            self._aliases.update({normalize_query(value): language.id for value in values if value})
            self._aliases[normalize_query(f"{language.name} songs")] = language.id
            code = self._STANDARD_CODES.get(normalize_query(language.name))
            if code:
                self._aliases[code] = language.id

    @classmethod
    def from_json(cls, raw: str) -> "LanguageCatalog":
        if not raw.strip():
            return cls([])
        try:
            values = TypeAdapter(list[Language]).validate_python(json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise RuntimeError("MUSIC_LANGUAGES_JSON is invalid") from exc
        if len({item.id for item in values}) != len(values):
            raise RuntimeError("MUSIC_LANGUAGES_JSON contains duplicate IDs")
        return cls(values)

    def resolve(self, ids: list[str]) -> list[Language]:
        resolved: list[Language] = []
        seen: set[str] = set()
        for value in ids:
            language_id = self._aliases.get(normalize_query(value))
            if language_id and language_id not in seen:
                seen.add(language_id)
                resolved.append(self.by_id[language_id])
        return resolved

    def normalize_ids(self, ids: list[str]) -> list[str]:
        return [language.id for language in self.resolve(ids)]


def _clean(result: Any) -> Any:
    return [] if isinstance(result, dict) and "error" in result else result


class CatalogService:
    def __init__(self, catalog: Any, languages: LanguageCatalog, cache: Any | None = None):
        self.catalog = catalog
        self.languages = languages
        self.cache = cache
        self._local_tasks: dict[str, asyncio.Task] = {}

    async def _cached(self, key: str, ttl: int, loader, stale_ttl: int | None = None):
        started = time.perf_counter()
        if self.cache is not None and hasattr(self.cache, "get_or_set"):
            value = await self.cache.get_or_set(key, loader, ttl, stale_ttl)
        else:
            value = await loader()
        logger.info("catalog_timing key=%s total_ms=%.1f cache=%s", key, (time.perf_counter() - started) * 1000, bool(self.cache))
        return value

    async def _artist_candidates_for_language(self, language: Language, limit: int, artist_limit: int | None = None) -> list[dict[str, Any]]:
        async def load():
            return await asyncio.gather(
                self.catalog.get_trending(language.name, limit),
            # Preserve the provider's artist-search contract while the song
            # and chart sources supply the broader discovery pool.
            self.catalog.search_artists(language.name, artist_limit or limit),
                self.catalog.search_songs(language.name, limit),
                return_exceptions=True,
            )
        results = await self._cached(f"music:artists:{language.id}:v1", config.TTL_ARTIST_DISCOVERY, load, config.STALE_CACHE_TTL)
        candidates: list[dict[str, Any]] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning("artist discovery failed language=%s error=%s", language.id, result)
                continue
            candidates.extend(items(_clean(result), "artist"))
            for track in items(_clean(result), "song"):
                candidates.extend(track.get("artists") or [])
        return candidates

    async def _artist_with_image(self, value: dict[str, Any]) -> dict[str, Any]:
        """Fill missing artwork from the catalog's artist detail endpoint.

        Song search results often contain an artist ID and name but no image.
        Resolve that ID through the provider instead of making the mobile app
        guess or downloading unrelated search-engine images.
        """
        if value.get("image_url") or value.get("imageUrl"):
            return value
        artist_id = str(value.get("id") or "").strip()
        name = str(value.get("name") or "").strip()
        if not artist_id and not name:
            return value

        cache_id = artist_id or normalize_query(name)

        async def load():
            if artist_id:
                try:
                    details = await self.catalog.get_artist_info([artist_id], False)
                    if isinstance(details, list):
                        for raw in details:
                            if isinstance(raw, dict):
                                normalized = artist(raw)
                                if normalized.get("image_url"):
                                    return normalized
                except Exception as exc:
                    logger.warning("artist image detail failed id=%s error=%s", artist_id, exc)
            if name:
                try:
                    results = items(_clean(await self.catalog.search_artists(name, 5)), "artist")
                    exact = next(
                        (item for item in results
                         if (artist_id and item.get("id") == artist_id)
                         or normalize_query(str(item.get("name") or "")) == normalize_query(name)),
                        None,
                    )
                    if exact and (exact.get("image_url") or exact.get("imageUrl")):
                        return exact
                except Exception as exc:
                    logger.warning("artist image search failed name=%s error=%s", name, exc)
            return {}

        resolved = await self._cached(
            f"music:artist-image:{cache_id}:v1",
            config.TTL_ARTIST,
            load,
            config.STALE_CACHE_TTL,
        )
        if isinstance(resolved, dict) and (resolved.get("image_url") or resolved.get("imageUrl")):
            result = dict(value)
            result["image_url"] = resolved.get("image_url") or resolved.get("imageUrl")
            result["imageUrl"] = result["image_url"]
            result["image_status"] = "verified"
            return result
        logger.info("artist image unresolved id=%s name=%s", artist_id, name)
        return value

    async def _hydrate_artist_images(self, values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        missing = [value for value in values if not value.get("image_url") and not value.get("imageUrl")]
        if not missing:
            return values
        semaphore = asyncio.Semaphore(8)

        async def resolve(value):
            async with semaphore:
                return await self._artist_with_image(value)

        resolved = await asyncio.gather(*(resolve(value) for value in missing), return_exceptions=True)
        by_key = {}
        for original, result in zip(missing, resolved):
            key = str(original.get("id") or normalize_query(str(original.get("name") or "")))
            by_key[key] = original if isinstance(result, Exception) else result
        return [by_key.get(str(value.get("id") or normalize_query(str(value.get("name") or ""))), value) if value in missing else value for value in values]

    async def artist_page(self, language_ids: list[str], limit: int, cursor: str | None = None) -> dict[str, Any]:
        languages = self.languages.resolve(language_ids)
        if not languages:
            return {"items": [], "next_cursor": None, "has_more": False, "language_ids": []}
        requested_offset = 0
        if cursor:
            try:
                requested_offset = int(base64.urlsafe_b64decode(cursor.encode()).decode())
            except (ValueError, UnicodeDecodeError, base64.binascii.Error):
                requested_offset = 0
        per_language = min(max(limit * 3, 60), 180)
        language_results = await asyncio.gather(*[
            self._artist_candidates_for_language(language, per_language, limit + 1) for language in languages
        ])
        buckets: list[list[dict[str, Any]]] = []
        for language, values in zip(languages, language_results):
            unique: dict[str, dict[str, Any]] = {}
            for value in values:
                key = str(value.get("id") or normalize_query(str(value.get("name") or "")))
                if key and key not in unique:
                    enriched = dict(value)
                    enriched["languages"] = [language.id]
                    unique[key] = enriched
            buckets.append(list(unique.values()))
        # Round-robin keeps one globally popular language from filling the page.
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        index = 0
        while True:
            added = False
            for bucket in buckets:
                if index < len(bucket):
                    value = bucket[index]
                    key = str(value.get("id") or normalize_query(str(value.get("name") or "")))
                    if key not in seen:
                        seen.add(key)
                        merged.append(value)
                    else:
                        existing = next(item for item in merged if str(item.get("id") or normalize_query(str(item.get("name") or ""))) == key)
                        existing["languages"] = sorted(set(existing.get("languages", [])) | set(value.get("languages", [])))
                    added = True
            if not added:
                break
            index += 1
        page = merged[requested_offset:requested_offset + limit]
        before_images = sum(1 for value in page if value.get("image_url") or value.get("imageUrl"))
        page = await self._hydrate_artist_images(page)
        after_images = sum(1 for value in page if value.get("image_url") or value.get("imageUrl"))
        # Candidate responses already include provider artwork. Avoid an extra
        # per-page provider request on the onboarding critical path.
        next_offset = requested_offset + len(page)
        has_more = next_offset < len(merged)
        next_cursor = base64.urlsafe_b64encode(str(next_offset).encode()).decode() if has_more else None
        logger.info("artist_discovery languages=%s before=%d after=%d offset=%d images_before=%d images_after=%d", [l.id for l in languages], sum(map(len, language_results)), len(merged), requested_offset, before_images, after_images)
        return {"items": page, "next_cursor": next_cursor, "has_more": has_more, "language_ids": [l.id for l in languages]}

    async def artists_for_languages(self, language_ids: list[str], limit: int) -> list[dict[str, Any]]:
        return (await self.artist_page(language_ids, limit))["items"]

    async def search(self, query: str, kind: str | None, page: int, limit: int) -> dict[str, Any]:
        methods = {
            "song": self.catalog.search_songs,
            "artist": self.catalog.search_artists,
            "album": self.catalog.search_albums,
            "playlist": self.catalog.search_playlists,
        }
        normalized_query = normalize_query(query)
        if kind:
            requested = min(page * limit + 10, 100)
            async def load():
                return _clean(await methods[kind](normalized_query, requested))
            # v2 invalidates older cache entries that were populated before
            # exact-match ranking and language metadata were fixed.
            result = await self._cached(f"music:search:{kind}:{normalized_query}:{requested}:v4", config.TTL_SEARCH, load, config.STALE_CACHE_TTL)
            normalized = rank(normalized_query, items(result, kind), kind, requested)
            # A movie search often has no movie name in the individual song
            # titles. Include the real soundtrack tracks when the query is an
            # exact/strong album match (for example, "Operation Java").
            if kind == "song" and hasattr(self.catalog, "get_album_info"):
                try:
                    album_result = await self.catalog.search_albums(normalized_query, 10)
                    matched_albums = [
                        item for item in rank(
                            normalized_query,
                            items(_clean(album_result), "album"),
                            "album",
                            10,
                        )
                        if _strong_match(normalized_query, item, "album")
                    ][:5]
                    if matched_albums:
                        async def soundtrack_tracks(item):
                            details = await self.catalog.get_album_info([str(item["id"])], True)
                            if isinstance(details, list) and details and isinstance(details[0], dict):
                                return album(details[0]).get("songs") or []
                            return []

                        expanded = await asyncio.gather(
                            *(soundtrack_tracks(item) for item in matched_albums),
                            return_exceptions=True,
                        )
                        soundtrack_songs = [
                            track for tracks in expanded
                            if isinstance(tracks, list) for track in tracks
                        ]
                        if soundtrack_songs:
                            normalized = rank(
                                normalized_query,
                                items(_clean(result), "song") + soundtrack_songs,
                                "song",
                                requested,
                            )
                except Exception as exc:
                    logger.warning("typed soundtrack search failed query=%r error=%s", query, exc)
            if kind == "artist":
                normalized = await self._hydrate_artist_images(normalized)
            logger.info(
                "catalog_search query=%r type=%s provider_count=%d normalized_count=%d",
                query, kind, len(result) if isinstance(result, list) else 0, len(normalized),
            )
            start = (page - 1) * limit
            page_items = normalized[start:start + limit]
            return {
                "items": page_items,
                "page": page,
                "limit": limit,
                "has_more": len(normalized) > start + limit,
                "type": kind,
            }

        preview = min(max(limit * 3, 12), 50)
        async def load_all():
            return await asyncio.gather(*[
                method(normalized_query, preview) for method in methods.values()
            ], return_exceptions=True)
        # Keep the version in the key so old broad/fuzzy result sets cannot
        # hide a valid soundtrack such as Sarkar (Tamil).
        results = await self._cached(f"music:search:all:{normalized_query}:{preview}:v4", config.TTL_SEARCH, load_all, config.STALE_CACHE_TTL)
        grouped: dict[str, list[dict[str, Any]]] = {}
        cursor = 0
        for result_kind in methods:
            candidates: list[dict[str, Any]] = []
            result = results[cursor]
            cursor += 1
            if not isinstance(result, Exception):
                candidates.extend(items(_clean(result), result_kind))
            grouped[f"{result_kind}s"] = rank(normalized_query, candidates, result_kind, preview)
            if result_kind == "artist":
                grouped["artists"] = await self._hydrate_artist_images(grouped["artists"])
            logger.info(
                "catalog_search query=%r type=%s provider_count=%d normalized_count=%d",
                query, result_kind,
                len(result) if isinstance(result, list) else 0,
                len(grouped[f"{result_kind}s"]),
            )

        # A movie-name search must also find songs whose titles do not repeat
        # the movie name. For example, searching "sarkar" should expose the
        # Tamil soundtrack tracks through the matching album's real tracklist.
        matched_albums = [
            item for item in grouped.get("albums", [])
            if _strong_match(normalized_query, item, "album")
        ]
        matched_albums.sort(
            key=lambda item: (
                "soundtrack" in normalize_query(str(item.get("title") or item.get("name") or "")),
                normalize_query(str(item.get("title") or item.get("name") or "")) == normalized_query,
            ),
            reverse=True,
        )
        matched_albums = matched_albums[:5]
        if matched_albums and hasattr(self.catalog, "get_album_info"):
            async def soundtrack_tracks(item):
                try:
                    details = await self.catalog.get_album_info([str(item["id"])], True)
                    if isinstance(details, list) and details and isinstance(details[0], dict):
                        return album(details[0]).get("songs") or []
                except Exception as exc:
                    logger.warning(
                        "search soundtrack expansion failed album_id=%s error=%s",
                        item.get("id"), exc,
                    )
                return []

            expanded = await asyncio.gather(*(soundtrack_tracks(item) for item in matched_albums))
            soundtrack_songs = [track for tracks in expanded for track in tracks]
            if soundtrack_songs:
                grouped["songs"] = rank(
                    normalized_query,
                    grouped.get("songs", []) + soundtrack_songs,
                    "song",
                    preview,
                )
        grouped["songs"] = _apply_album_artwork(
            grouped.get("songs", []),
            grouped.get("albums", []),
        )
        all_ranked = [(confidence(normalized_query, item, key[:-1]), key[:-1], item)
                      for key in ("artists", "songs", "albums", "playlists") for item in grouped[key]]
        top = None
        if all_ranked:
            top_confidence, top_kind, top_item = max(all_ranked, key=lambda value: value[0])
            if top_confidence >= 0.55:
                top = {"type": top_kind, "item": top_item, "confidence": round(top_confidence, 3)}
        return {"query": query, "normalized_query": normalized_query, "top_result": top, **grouped}

    async def discover(self, limit: int) -> list[dict[str, Any]]:
        if not self.languages.languages:
            return []
        language = self.languages.languages[0]
        async def load():
            return await asyncio.gather(
                self.catalog.get_trending(language.name, limit),
                self.catalog.get_new_releases(language.name, limit),
                return_exceptions=True,
            )
        trending, releases = await self._cached(f"music:discover:{language.id}:{limit}:v1", config.TTL_DISCOVER, load, config.STALE_CACHE_TTL)
        candidates = [
            ("trending", "songs", trending, "song"),
            ("new_releases", "albums", releases, "album"),
        ]
        sections = []
        for section_id, section_type, result, item_type in candidates:
            values = [] if isinstance(result, Exception) else items(_clean(result), item_type)
            if values:
                sections.append({"id": section_id, "type": section_type, "items": values})
        return sections

    async def artist_details(self, artist_id: str, limit: int) -> dict[str, Any] | None:
        result = _clean(await self.catalog.get_artist_info([artist_id], True, limit, 1))
        if not isinstance(result, list) or not result:
            return None
        raw = result[0]
        normalized_artist = artist(raw)
        if not normalized_artist["id"] or not normalized_artist["name"]:
            return None
        return {
            "artist": normalized_artist,
            "popular_songs": items(raw.get("top_tracks"), "song"),
            "albums": [],
            "singles": [],
            "appears_on": [],
            "related_artists": [],
        }

    async def album_details(self, album_id: str) -> dict[str, Any] | None:
        try:
            result = _clean(await self.catalog.get_album_info([album_id], True))
        except Exception:
            logger.exception("album_details provider_failure album_id=%s", album_id)
            raise
        if not isinstance(result, list) or not result:
            logger.warning("album_details empty_provider_response album_id=%s", album_id)
            return None
        try:
            normalized_album = album(result[0])
        except Exception:
            logger.exception("album_details normalization_failure album_id=%s", album_id)
            raise
        if not normalized_album["id"] or not normalized_album["name"]:
            logger.warning("album_details invalid_normalized_album album_id=%s", album_id)
            return None
        logger.info(
            "album_details album_id=%s provider_count=%d track_count=%d",
            album_id, len(result), len(normalized_album.get("tracks") or []),
        )
        return normalized_album

    async def home_page(
        self,
        uid: str,
        repository: Any,
        recommendations: Any,
        limit: int,
        offset: int = 0,
        content_type: str = "all",
    ) -> dict[str, Any]:
        """Build one bounded page of the home feed.

        The cursor is applied after composition so every section remains typed
        and the client can append pages without mixing songs with entities.
        ``request_limit`` is bounded and is never the size of the catalogue.
        """
        # Fetch one look-ahead item so ``has_more`` is accurate without
        # requesting the complete catalogue.
        request_limit = min(max(limit + offset + 1, limit), 100)
        profile, history, favorites, albums_raw, artists_raw, playlists_raw, recommended = await asyncio.gather(
            repository.get_profile(uid),
            repository.list_history(uid, request_limit),
            repository.list_favorites(uid),
            repository.list_saved_albums(uid),
            repository.list_followed_artists(uid),
            repository.list_playlists(uid),
            recommendations.recommendations(uid, self.catalog, request_limit),
        )
        sections: list[dict[str, Any]] = []

        def add(section_id: str, section_type: str, title: str, values: list[dict[str, Any]]) -> None:
            if values:
                sections.append({"id": section_id, "type": section_type, "title": title, "items": values})

        add("recently_played", "songs", "Recently Played", [song(value) for value in history])
        add("made_for_you", "songs", "Made For You", [song(value) for value in recommended])
        add("liked_songs", "songs", "Liked Songs", [song(value) for value in favorites])
        add("saved_albums", "albums", "Saved Albums", [album(value) for value in albums_raw])
        add("followed_artists", "artists", "Followed Artists", [artist(value) for value in artists_raw])
        add("your_playlists", "playlists", "Your Playlists", [playlist(value) for value in playlists_raw])

        preferred = self.languages.resolve(profile.get("language_ids") or [])
        if preferred:
            trending, releases = await asyncio.gather(
                self.catalog.get_trending(preferred[0].name, limit),
                self.catalog.get_new_releases(preferred[0].name, limit),
                return_exceptions=True,
            )
            if not isinstance(trending, Exception):
                add("trending", "songs", "Trending Now", items(_clean(trending), "song"))
            if not isinstance(releases, Exception):
                add("new_releases", "albums", "New Releases", items(_clean(releases), "album"))

        allowed = {"all": None, "song": "song", "album": "album", "artist": "artist", "playlist": "playlist"}
        selected = allowed.get(content_type, None)
        if selected:
            sections = [
                {**section, "items": [item for item in section["items"] if item.get("type") == selected]}
                for section in sections
            ]
            sections = [section for section in sections if section["items"]]

        # De-duplicate globally within each typed section while preserving order.
        section_has_more = False
        for section in sections:
            seen: set[str] = set()
            unique = []
            for item in section["items"]:
                key = f"{item.get('type')}:{item.get('id')}"
                if key not in seen:
                    seen.add(key)
                    unique.append(item)
            section_has_more = section_has_more or len(unique) > offset + limit
            section["items"] = unique[offset:offset + limit]
        sections = [section for section in sections if section["items"]]
        has_more = section_has_more
        next_cursor = str(offset + limit) if has_more else None
        return {"sections": sections, "next_cursor": next_cursor, "has_more": has_more, "content_type": content_type}

    async def home(self, uid: str, repository: Any, recommendations: Any, limit: int) -> list[dict[str, Any]]:
        """Compatibility wrapper for existing callers."""
        return (await self.home_page(uid, repository, recommendations, limit))["sections"]
