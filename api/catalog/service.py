import asyncio
import base64
import json
import logging
import re
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
    def __init__(self, catalog: Any, languages: LanguageCatalog, cache: Any | None = None, db_pool: Any | None = None):
        self.catalog = catalog
        self.languages = languages
        self.cache = cache
        self.db_pool = db_pool
        self._local_tasks: dict[str, asyncio.Task] = {}

    async def _cached(self, key: str, ttl: int, loader, stale_ttl: int | None = None):
        started = time.perf_counter()
        if self.cache is not None and hasattr(self.cache, "get_or_set"):
            value = await self.cache.get_or_set(key, loader, ttl, stale_ttl)
        else:
            value = await loader()
        logger.info("catalog_timing key=%s total_ms=%.1f cache=%s", key, (time.perf_counter() - started) * 1000, bool(self.cache))
        return value

    async def _db_artists_for_language(self, language_id: str) -> list[dict[str, Any]]:
        pool = getattr(self, "db_pool", None)
        if not pool:
            return []
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT a.id, a.name, a.image_url, a.metadata
                    FROM artists a
                    JOIN artist_languages al ON a.id = al.artist_id
                    WHERE al.language_id = $1
                    ORDER BY al.confidence DESC, a.name ASC;
                    """,
                    language_id,
                )
                res: list[dict[str, Any]] = []
                for row in rows:
                    img = str(row["image_url"] or "").strip()
                    if "dzcdn.net" in img or img.startswith("{") or "placeholder" in img:
                        img = ""
                    res.append({
                        "id": row["id"],
                        "seokey": row["id"],
                        "name": row["name"],
                        "image_url": img,
                        "imageUrl": img,
                        "image_status": "verified" if img else "placeholder",
                        "languages": [language_id],
                        "source": "database",
                        "verified": True,
                    })
                return res
        except Exception as exc:
            logger.warning("db artists query failed language=%s error=%s", language_id, exc)
            return []

    def _async_persist_artists(self, artists: list[dict[str, Any]]) -> None:
        pool = getattr(self, "db_pool", None)
        if not pool or not artists:
            return

        async def _save():
            try:
                async with pool.acquire() as conn:
                    for item in artists:
                        art_id = str(item.get("id") or item.get("seokey") or "").strip()
                        name = str(item.get("name") or "").strip()
                        img = item.get("image_url") or item.get("imageUrl") or None
                        langs = item.get("languages") or []
                        if not art_id or not name or len(name) < 2:
                            continue
                        metadata = json.dumps({"source": "auto_discovery"})
                        await conn.execute(
                            """
                            INSERT INTO artists (id, name, image_url, metadata, updated_at)
                            VALUES ($1, $2, $3, $4::jsonb, now())
                            ON CONFLICT (id) DO UPDATE SET
                                image_url = COALESCE(artists.image_url, EXCLUDED.image_url),
                                updated_at = now();
                            """,
                            art_id,
                            name,
                            img,
                            metadata,
                        )
                        for lang_id in langs:
                            if isinstance(lang_id, str) and lang_id:
                                await conn.execute(
                                    """
                                    INSERT INTO artist_languages (artist_id, language_id, source, confidence, updated_at)
                                    VALUES ($1, $2, 'discovery', 0.8, now())
                                    ON CONFLICT (artist_id, language_id) DO NOTHING;
                                    """,
                                    art_id,
                                    lang_id,
                                )
            except Exception as exc:
                logger.debug("Failed to async persist discovered artists: %s", exc)

        try:
            asyncio.create_task(_save())
        except Exception:
            pass

    async def _artist_candidates_for_language(self, language: Language, limit: int, artist_limit: int | None = None) -> list[dict[str, Any]]:
        async def load():
            db_task = self._db_artists_for_language(language.id)
            return await asyncio.gather(
                db_task,
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
            if isinstance(result, list):
                for item in result:
                    if isinstance(item, dict) and item.get("source") == "database":
                        candidates.append(item)
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
            # 1. Database check (curated, verified original portraits)
            pool = getattr(self, "db_pool", None)
            if pool:
                try:
                    async with pool.acquire() as conn:
                        row = await conn.fetchrow(
                            """
                            SELECT id, name, image_url
                            FROM artists
                            WHERE (id = $1 AND id != '')
                               OR (lower(name) = lower($2) AND name != '')
                            LIMIT 1;
                            """,
                            artist_id, name,
                        )
                        if row and row["image_url"]:
                            img = str(row["image_url"]).strip()
                            if img and "dzcdn.net" not in img and not img.startswith("{") and "placeholder" not in img:
                                return {
                                    "id": row["id"],
                                    "name": row["name"],
                                    "image_url": img,
                                    "imageUrl": img,
                                    "image_status": "verified",
                                    "source": "database",
                                }
                except Exception as exc:
                    logger.debug("db artist portrait lookup failed: %s", exc)

            # 2. Gaana catalog get_artist_info
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

            # 3. Gaana catalog search_artists
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

            # Never use unverified external web scrapers (Deezer/Wikipedia) that return wrong persons
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
        missing = [value for value in values if not value.get("image_url") and not value.get("imageUrl")][:4]
        if not missing:
            return values
        semaphore = asyncio.Semaphore(4)

        async def resolve(value):
            async with semaphore:
                try:
                    return await asyncio.wait_for(self._artist_with_image(value), timeout=0.8)
                except Exception:
                    return value

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
        self._async_persist_artists(page)
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
            result = await self._cached(f"music:search:{kind}:{normalized_query}:{requested}:v10", config.TTL_SEARCH, load, config.STALE_CACHE_TTL)
            normalized = rank(normalized_query, items(result, kind), kind, requested)
            # A movie search often has no movie name in the individual song
            # titles. Include the real soundtrack tracks when the query is an
            # exact/strong album match (for example, "Operation Java").
            if kind == "song":
                extra_candidates: list[dict[str, Any]] = []
                if hasattr(self.catalog, "get_album_info"):
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
                                try:
                                    details = await self.catalog.get_album_info([str(item["id"])], True)
                                    if isinstance(details, list) and details and isinstance(details[0], dict):
                                        return album(details[0]).get("songs") or []
                                    return []
                                except Exception as exc:
                                    logger.warning("typed soundtrack search failed query=%r error=%s", query, exc)
                                    return []

                            expanded = await asyncio.gather(*(soundtrack_tracks(item) for item in matched_albums))
                            for tracks in expanded:
                                if isinstance(tracks, list):
                                    extra_candidates.extend(tracks)
                    except Exception as exc:
                        logger.warning("typed soundtrack search failed query=%r error=%s", query, exc)

                # Artist top-hits deep scan
                if hasattr(self.catalog, "get_top_tracks"):
                    try:
                        artist_result = await self.catalog.search_artists(normalized_query, 5)
                        matched_artists = [
                            item for item in rank(
                                normalized_query,
                                items(_clean(artist_result), "artist"),
                                "artist",
                                5,
                            )
                            if _strong_match(normalized_query, item, "artist")
                        ][:3]
                        if matched_artists:
                            async def artist_tracks(item):
                                try:
                                    res = await self.catalog.get_top_tracks(str(item["id"]), limit=15)
                                    if isinstance(res, dict) and "tracks" in res:
                                        return items(res["tracks"], "song")
                                    elif isinstance(res, list):
                                        return items(res, "song")
                                    return []
                                except Exception:
                                    return []

                            expanded_artists = await asyncio.gather(*(artist_tracks(item) for item in matched_artists))
                            for tracks in expanded_artists:
                                if isinstance(tracks, list):
                                    extra_candidates.extend(tracks)
                    except Exception:
                        pass

                # Multi-keyword deep scan for combined queries like "believer imagine dragons"
                query_words = [w for w in normalized_query.split() if len(w) > 2]
                if len(query_words) > 1 and len(normalized) < 4:
                    try:
                        async def sub_search(w):
                            try:
                                return items(_clean(await self.catalog.search_songs(w, 10)), "song")
                            except Exception:
                                return []
                        sub_res = await asyncio.gather(*(sub_search(w) for w in query_words))
                        for sub_list in sub_res:
                            extra_candidates.extend(sub_list)
                    except Exception:
                        pass

                if extra_candidates:
                    normalized = rank(
                        normalized_query,
                        items(_clean(result), "song") + extra_candidates,
                        "song",
                        requested,
                    )
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
            res = await asyncio.gather(*[
                method(normalized_query, preview) for method in methods.values()
            ], return_exceptions=True)
            return [
                [] if isinstance(r, Exception) or (isinstance(r, dict) and "error" in r) else r
                for r in res
            ]
        results = await self._cached(f"music:search:all:{normalized_query}:{preview}:v10", config.TTL_SEARCH, load_all, config.STALE_CACHE_TTL)
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

        # Deep scan 1: Movie-name / soundtrack expansion across all languages
        matched_albums = [
            item for item in grouped.get("albums", [])
            if _strong_match(normalized_query, item, "album")
        ]
        matched_albums.sort(
            key=lambda item: (
                normalize_query(str(item.get("title") or item.get("name") or "")) == normalized_query,
                "soundtrack" in normalize_query(str(item.get("title") or item.get("name") or "")),
                normalize_query(str(item.get("title") or item.get("name") or "")).startswith(normalized_query),
            ),
            reverse=True,
        )
        matched_albums = matched_albums[:2]
        extra_songs: list[dict[str, Any]] = []
        if matched_albums and hasattr(self.catalog, "get_album_info"):
            async def soundtrack_tracks(item):
                try:
                    details = await asyncio.wait_for(
                        self.catalog.get_album_info([str(item["id"])], True),
                        timeout=1.5,
                    )
                    if isinstance(details, list) and details and isinstance(details[0], dict):
                        return album(details[0]).get("songs") or []
                except Exception as exc:
                    logger.warning(
                        "search soundtrack expansion failed album_id=%s error=%s",
                        item.get("id"), exc,
                    )
                return []

            expanded = await asyncio.gather(*(soundtrack_tracks(item) for item in matched_albums))
            for tracks in expanded:
                if isinstance(tracks, list):
                    extra_songs.extend(tracks)

        # Deep scan 2: Artist top-tracks expansion when searching artist names
        matched_artists = [
            item for item in grouped.get("artists", [])
            if _strong_match(normalized_query, item, "artist")
        ][:1]
        if matched_artists and hasattr(self.catalog, "get_top_tracks"):
            async def artist_top_tracks(item):
                try:
                    target_id = str(item.get("artist_id") or item.get("provider_id") or item.get("id") or "")
                    if target_id:
                        res = await asyncio.wait_for(
                            self.catalog.get_top_tracks(target_id, limit=10),
                            timeout=2.5,
                        )
                        if isinstance(res, dict) and "tracks" in res:
                            return items(res["tracks"], "song")
                        elif isinstance(res, list):
                            return items(res, "song")
                except Exception as exc:
                    logger.warning("search artist top tracks expansion failed artist_id=%s error=%s", item.get("id"), exc)
                return []

            expanded_artist_tracks = await asyncio.gather(*(artist_top_tracks(item) for item in matched_artists))
            for tracks in expanded_artist_tracks:
                if isinstance(tracks, list):
                    extra_songs.extend(tracks)

        # Deep scan 3: Multi-word query fallback when direct song search is sparse
        stopwords = {"movie", "film", "songs", "song", "track", "audio", "album", "soundtrack", "the", "and"}
        query_words = [w for w in normalized_query.split() if len(w) > 2 and w.lower() not in stopwords]
        if len(query_words) > 1 and len(grouped.get("songs", [])) < 2:
            async def sub_search(w):
                try:
                    return items(_clean(await asyncio.wait_for(self.catalog.search_songs(w, 8), timeout=1.2)), "song")
                except Exception:
                    return []
            sub_res = await asyncio.gather(*(sub_search(w) for w in query_words))
            for sub_list in sub_res:
                extra_songs.extend(sub_list)

        if extra_songs:
            grouped["songs"] = rank(
                normalized_query,
                grouped.get("songs", []) + extra_songs,
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
            if top_confidence >= 0.50:
                top = {"type": top_kind, "item": top_item, "confidence": round(top_confidence, 3)}

        # When Top Result is an Album/Movie (e.g. "Ghilli"), prioritize its soundtrack songs at the top of songs list
        if top and top.get("type") == "album" and top.get("item"):
            top_album_title = normalize_query(str(top["item"].get("title") or top["item"].get("name") or ""))
            top_album_id = str(top["item"].get("id") or "")
            album_tracks: list[dict[str, Any]] = []
            other_tracks: list[dict[str, Any]] = []
            for s in grouped.get("songs", []):
                s_album_val = s.get("album")
                s_album_title = ""
                s_album_id = ""
                if isinstance(s_album_val, dict):
                    s_album_title = normalize_query(str(s_album_val.get("title") or s_album_val.get("name") or ""))
                    s_album_id = str(s_album_val.get("id") or "")
                elif isinstance(s_album_val, str):
                    s_album_title = normalize_query(s_album_val)
                if (top_album_title and s_album_title == top_album_title) or (top_album_id and s_album_id == top_album_id):
                    album_tracks.append(s)
                else:
                    other_tracks.append(s)
            grouped["songs"] = album_tracks + other_tracks

        # Spotify-style response grouping limits: Top 1, Songs 15, Albums 6, Artists 6, Playlists 6
        if "songs" in grouped:
            grouped["songs"] = grouped["songs"][:15]
        if "albums" in grouped:
            grouped["albums"] = grouped["albums"][:6]
        if "artists" in grouped:
            grouped["artists"] = grouped["artists"][:6]
        if "playlists" in grouped:
            grouped["playlists"] = grouped["playlists"][:6]

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
        async def load():
            result = _clean(await self.catalog.get_artist_info([artist_id], True, limit, 1))
            if not isinstance(result, list) or not result:
                # Fallback search if direct seokey lookup failed (e.g. p-jayachandran -> p-jayachandran-1)
                queries = [
                    artist_id,
                    artist_id.replace('-', ' ').replace('_', ' ').strip(),
                    re.sub(r'^[a-zA-Z][\.\s]+', '', artist_id.replace('-', ' ').replace('_', ' ').strip()),
                ]
                for q in queries:
                    if not q:
                        continue
                    try:
                        search_res = _clean(await self.catalog.search_artists(q, 5))
                        if isinstance(search_res, list) and search_res:
                            for cand in search_res:
                                cand_seo = cand.get('seokey') or cand.get('id') or cand.get('artist_id')
                                if cand_seo and str(cand_seo) != artist_id:
                                    cand_info = _clean(await self.catalog.get_artist_info([str(cand_seo)], True, limit, 1))
                                    if isinstance(cand_info, list) and cand_info:
                                        result = cand_info
                                        break
                            if isinstance(result, list) and result:
                                break
                    except Exception:
                        pass

            pool = getattr(self, "db_pool", None)
            if not isinstance(result, list) or not result:
                if pool:
                    try:
                        async with pool.acquire() as conn:
                            db_row = await conn.fetchrow(
                                """
                                SELECT id, name, image_url
                                FROM artists
                                WHERE (id = $1 AND id != '')
                                   OR (lower(name) = lower($2) AND name != '')
                                LIMIT 1;
                                """,
                                artist_id, artist_id.replace('-', ' '),
                            )
                            if db_row:
                                img = str(db_row["image_url"] or "").strip()
                                if "dzcdn.net" in img or img.startswith("{") or "placeholder" in img:
                                    img = ""
                                raw = {
                                    "id": db_row["id"],
                                    "seokey": db_row["id"],
                                    "name": db_row["name"],
                                    "image_url": img,
                                    "imageUrl": img,
                                    "top_tracks": [],
                                }
                                result = [raw]
                    except Exception as exc:
                        logger.debug("artist_details db fallback failed: %s", exc)

            if not isinstance(result, list) or not result:
                return None
            raw = result[0]
            normalized_artist = artist(raw)
            if not normalized_artist["id"] or not normalized_artist["name"]:
                return None

            if pool and (not normalized_artist.get("image_url") or normalized_artist.get("image_status") == "placeholder"):
                try:
                    async with pool.acquire() as conn:
                        db_row = await conn.fetchrow(
                            """
                            SELECT image_url
                            FROM artists
                            WHERE (id = $1 AND id != '')
                               OR (lower(name) = lower($2) AND name != '')
                            LIMIT 1;
                            """,
                            normalized_artist.get("id") or artist_id,
                            normalized_artist.get("name") or "",
                        )
                        if db_row and db_row["image_url"]:
                            db_img = str(db_row["image_url"]).strip()
                            if db_img and "dzcdn.net" not in db_img and not db_img.startswith("{") and "placeholder" not in db_img:
                                normalized_artist["image_url"] = db_img
                                normalized_artist["imageUrl"] = db_img
                                normalized_artist["image_status"] = "verified"
                except Exception as exc:
                    logger.debug("artist_details db image hydration failed: %s", exc)

            artist_name = normalized_artist.get("name") or artist_id
            album_list = []
            album_queries = [
                artist_name,
                re.sub(r'^[a-zA-Z][\.\s]+', '', artist_name).strip(),
                artist_id.replace('-', ' '),
            ]
            seen_album_titles = set()
            unique_queries = [aq for aq in album_queries if aq and len(aq) > 1][:2]

            album_results = await asyncio.gather(*[
                self.catalog.search_albums(aq, 10) for aq in unique_queries
            ], return_exceptions=True)

            for raw_albums in album_results:
                if isinstance(raw_albums, list):
                    for a in items(_clean(raw_albums), "album"):
                        title_key = (a.get("name") or a.get("title") or "").lower().strip()
                        if title_key and title_key not in seen_album_titles:
                            seen_album_titles.add(title_key)
                            album_list.append(a)

            # Also extract unique albums from top_tracks if needed
            for t in raw.get("top_tracks", []):
                alb_id = str(t.get("album_id") or t.get("album_seokey") or "")
                alb_title = t.get("album") or ""
                title_key = alb_title.lower().strip()
                if alb_id and alb_title and title_key not in seen_album_titles:
                    seen_album_titles.add(title_key)
                    album_list.append({
                        "id": alb_id,
                        "name": alb_title,
                        "title": alb_title,
                        "artist": artist_name,
                        "image_url": (t.get("images", {}).get("urls", {}).get("large_artwork") or t.get("image_url") or ""),
                        "images": t.get("images", {}),
                        "track_count": 0,
                        "release_date": t.get("release_date", ""),
                        "language": t.get("language", ""),
                    })

            return {
                "artist": normalized_artist,
                "popular_songs": items(raw.get("top_tracks"), "song"),
                "albums": album_list,
                "singles": [],
                "appears_on": [],
                "related_artists": [],
            }

        return await self._cached(
            f"music:artist_details:{artist_id}:{limit}:v1",
            getattr(config, "TTL_ARTIST_DETAILS", config.TTL_ARTIST),
            load,
            config.STALE_CACHE_TTL,
        )

    async def album_details(self, album_id: str) -> dict[str, Any] | None:
        normalized_album = None
        try:
            result = _clean(await self.catalog.get_album_info([album_id], True))
            if isinstance(result, list) and result:
                normalized_album = album(result[0])
                if (normalized_album.get("id") or normalized_album.get("provider_id")) and normalized_album.get("name"):
                    tracks = normalized_album.get("tracks") or normalized_album.get("songs") or []
                    if tracks:
                        logger.info(
                            "album_details album_id=%s provider_count=%d track_count=%d",
                            album_id, len(result), len(tracks),
                        )
                        return normalized_album
        except Exception:
            logger.exception("album_details provider_failure album_id=%s", album_id)

        # Resilient fallback: Search by album title / query if direct album info returned empty
        album_meta = normalized_album or {}
        album_title = album_meta.get("name") or album_meta.get("title") or album_id.replace("-", " ").replace("_", " ").strip()
        clean_query = re.sub(r'[^a-zA-Z0-9\s]+', ' ', album_title).strip()
        query_candidates = [q for q in (album_title, clean_query) if q]

        for query in query_candidates:
            try:
                # Try search_albums first
                album_search = _clean(await self.catalog.search_albums(query, 5))
                if isinstance(album_search, list) and album_search:
                    for cand in album_search:
                        cand_id = str(cand.get("album_id") or cand.get("id") or cand.get("seokey") or "")
                        if cand_id and cand_id != album_id:
                            info = _clean(await self.catalog.get_album_info([cand_id], True))
                            if isinstance(info, list) and info:
                                norm = album(info[0])
                                tracks = norm.get("tracks") or norm.get("songs") or []
                                if norm.get("name") and tracks:
                                    return norm
            except Exception as exc:
                logger.warning("album_details search fallback failed query=%s error=%s", query, exc)

            try:
                # Try search_songs to build album and tracklist
                song_search = _clean(await self.catalog.search_songs(query, 25))
                if isinstance(song_search, list) and song_search:
                    song_items = items(song_search, "song")
                    if song_items:
                        first = song_items[0]
                        first_album_raw = first.get("album")
                        first_album_name = (
                            first_album_raw.get("title") or first_album_raw.get("name") or album_title
                            if isinstance(first_album_raw, dict)
                            else str(first_album_raw or album_title)
                        )
                        first_album_img = (
                            album_meta.get("image_url")
                            or (first_album_raw.get("artworkUrl") if isinstance(first_album_raw, dict) else None)
                            or first.get("image_url") or ""
                        )
                        matched_songs = [
                            s for s in song_items
                            if (
                                (isinstance(s.get("album"), dict) and s["album"].get("name", "").lower() == first_album_name.lower())
                                or str(s.get("album") or "").lower() == first_album_name.lower()
                            )
                        ]
                        resolved_songs = matched_songs if matched_songs else song_items
                        return {
                            "id": str(album_meta.get("id") or first.get("album_id") or album_id),
                            "provider_id": str(album_meta.get("provider_id") or first.get("album_id") or album_id),
                            "type": "album",
                            "name": album_title or first_album_name,
                            "title": album_title or first_album_name,
                            "image_url": first_album_img,
                            "imageUrl": first_album_img,
                            "artworkUrl": first_album_img,
                            "artists": album_meta.get("artists") or first.get("artists") or [],
                            "artistNames": album_meta.get("artistNames") or ([first.get("artist") or ""] if first.get("artist") else []),
                            "artistIds": album_meta.get("artistIds") or [],
                            "song_count": len(resolved_songs),
                            "trackCount": len(resolved_songs),
                            "songs": resolved_songs,
                            "tracks": resolved_songs,
                            "language": album_meta.get("language") or "",
                            "release_date": album_meta.get("release_date"),
                            "releaseYear": album_meta.get("releaseYear"),
                        }
            except Exception as exc:
                logger.warning("album_details song search fallback failed query=%s error=%s", query, exc)

        if normalized_album:
            return normalized_album

        return None

    async def album_recommendations(
        self, album_id: str, user_profile: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Generate smart album recommendations for the currently viewed album."""
        cache_key = f"music:album:recs:{album_id}:v2"

        async def compute_recommendations() -> dict[str, Any]:
            alb = await self.album_details(album_id)
            if not alb:
                return {
                    "albumId": album_id,
                    "primaryArtist": "",
                    "recommendations": [],
                    "moreByArtist": [],
                    "youMightAlsoLike": [],
                }

            canonical_id = alb.get("id") or album_id
            current_title = (alb.get("title") or alb.get("name") or "").lower().strip()
            artist_names = [n for n in (alb.get("artistNames") or [alb.get("artist") or ""]) if n]
            primary_artist = artist_names[0] if artist_names else ""
            album_lang = str(alb.get("language") or "").strip()
            album_year = alb.get("releaseYear")
            album_genre = str(alb.get("genre") or alb.get("label") or "").strip()

            candidates_raw: list[dict[str, Any]] = []
            tasks = []

            # 1. Primary artist albums
            if primary_artist and hasattr(self.catalog, "search_albums"):
                async def fetch_primary():
                    try:
                        res = await asyncio.wait_for(self.catalog.search_albums(primary_artist, 12), timeout=3.5)
                        if isinstance(res, list):
                            return res
                    except Exception:
                        pass
                    return []
                tasks.append(fetch_primary())

            # 2. Secondary participating artists albums
            for sec_artist in artist_names[1:3]:
                if hasattr(self.catalog, "search_albums"):
                    async def fetch_sec(art=sec_artist):
                        try:
                            res = await asyncio.wait_for(self.catalog.search_albums(art, 6), timeout=3.5)
                            if isinstance(res, list):
                                return res
                        except Exception:
                            pass
                        return []
                    tasks.append(fetch_sec())

            # 3. Same language new releases and search
            if album_lang:
                if hasattr(self.catalog, "get_new_releases"):
                    async def fetch_new():
                        try:
                            res = await asyncio.wait_for(self.catalog.get_new_releases(album_lang, 12), timeout=3.5)
                            if isinstance(res, list):
                                return res
                            elif isinstance(res, dict) and "albums" in res:
                                return res["albums"]
                        except Exception:
                            pass
                        return []
                    tasks.append(fetch_new())
                if hasattr(self.catalog, "search_albums"):
                    async def fetch_lang_search():
                        try:
                            res = await asyncio.wait_for(self.catalog.search_albums(album_lang, 12), timeout=3.5)
                            if isinstance(res, list):
                                return res
                        except Exception:
                            pass
                        return []
                    tasks.append(fetch_lang_search())

            # 4. User preferred language fallback
            user_langs = (user_profile or {}).get("languages") or []
            for u_lang in user_langs[:2]:
                if u_lang.lower() != album_lang.lower() and hasattr(self.catalog, "get_new_releases"):
                    async def fetch_user_lang(l=u_lang):
                        try:
                            res = await asyncio.wait_for(self.catalog.get_new_releases(l, 8), timeout=3.5)
                            if isinstance(res, list):
                                return res
                        except Exception:
                            pass
                        return []
                    tasks.append(fetch_user_lang())

            if tasks:
                results_list = await asyncio.gather(*tasks, return_exceptions=True)
                for res in results_list:
                    if isinstance(res, list):
                        candidates_raw.extend(res)

            seen_ids: set[str] = {album_id.lower().strip(), canonical_id.lower().strip()}
            seen_semantic_keys: set[str] = set()
            scored: list[tuple[int, dict[str, Any]]] = []

            def _clean_title(t: str) -> str:
                t = t.lower().strip()
                t = re.sub(r'[\(\[\{].*?[\)\]\}]', '', t)
                t = re.sub(r'\b(?:original motion picture soundtrack|soundtrack|ost|vol(?:ume)?\.?\s*\d+|ep|single)\b', '', t, flags=re.I)
                return re.sub(r'[^a-z0-9]+', '', t).strip()

            current_clean_title = _clean_title(current_title)
            if current_clean_title:
                curr_art_key = re.sub(r'[^a-z0-9]+', '', primary_artist.lower()).strip()
                seen_semantic_keys.add(f"{current_clean_title}-{curr_art_key}")

            user_fav_artists = [a.lower() for a in ((user_profile or {}).get("favorite_artists") or [])]
            user_pref_langs = [l.lower() for l in user_langs]

            for raw in candidates_raw:
                if not isinstance(raw, dict):
                    continue
                norm = album(raw)
                cid = str(norm.get("id") or norm.get("seokey") or "").strip()
                ctitle = str(norm.get("title") or norm.get("name") or "").strip()
                if not cid or not ctitle:
                    continue

                cid_lower = cid.lower()
                cand_clean_title = _clean_title(ctitle)
                cand_artists = [a.lower() for a in (norm.get("artistNames") or [norm.get("artist") or ""])]
                primary_cand_art = cand_artists[0] if cand_artists else ""
                art_key = re.sub(r'[^a-z0-9]+', '', primary_cand_art).strip()
                semantic_key = f"{cand_clean_title}-{art_key}" if cand_clean_title else ""

                if cid_lower in seen_ids:
                    continue
                if semantic_key and semantic_key in seen_semantic_keys:
                    continue
                if cand_clean_title and cand_clean_title == current_clean_title:
                    continue

                seen_ids.add(cid_lower)
                if semantic_key:
                    seen_semantic_keys.add(semantic_key)

                score = 0
                reason = "genre_language"

                # Priority 1: Same primary artist (+50)
                if primary_artist and any(primary_artist.lower() in a for a in cand_artists):
                    score += 50
                    reason = "same_artist"
                # Priority 1b: Participating artists (+35)
                elif any(any(orig.lower() in a for orig in artist_names) for a in cand_artists):
                    score += 35
                    reason = "same_artist"
                # Priority 2: Related artist / user liked artist (+15 to +25)
                elif any(fa in a for fa in user_fav_artists for a in cand_artists):
                    score += 25
                    reason = "related_artist"

                # Priority 3: Same language (+20)
                cand_lang = norm.get("language", "").lower()
                if album_lang and cand_lang == album_lang.lower():
                    score += 20
                elif cand_lang in user_pref_langs:
                    score += 15

                # Genre matching (+20)
                cand_genre = (norm.get("genre") or norm.get("label") or "").lower()
                if album_genre and cand_genre and (album_genre.lower() in cand_genre or cand_genre in album_genre.lower()):
                    score += 20

                # Priority 4: Release year similarity (+10 or +5)
                cyear = norm.get("releaseYear")
                if cyear and album_year:
                    diff = abs(cyear - album_year)
                    if diff <= 3:
                        score += 10
                    elif diff <= 6:
                        score += 5

                # Base popularity (+5)
                if norm.get("song_count", 0) > 0:
                    score += 5

                rec_item = {
                    "id": cid,
                    "title": norm.get("title") or norm.get("name") or "",
                    "artistId": (norm.get("artistIds") or [""])[0],
                    "artistName": (norm.get("artistNames") or [""])[0] or norm.get("artist") or "",
                    "imageUrl": norm.get("imageUrl") or norm.get("image_url") or "",
                    "releaseYear": norm.get("releaseYear"),
                    "language": norm.get("language") or "",
                    "genre": norm.get("genre") or "",
                    "songCount": norm.get("song_count") or norm.get("trackCount") or 0,
                    "reason": reason,
                }
                scored.append((score, rec_item))

            scored.sort(key=lambda x: x[0], reverse=True)

            used_rec_ids: set[str] = set()
            more_by_artist: list[dict[str, Any]] = []
            for sc, item in scored:
                iid = str(item.get("id") or "").lower()
                if item["reason"] == "same_artist" and iid not in used_rec_ids:
                    used_rec_ids.add(iid)
                    more_by_artist.append(item)
                if len(more_by_artist) >= 12:
                    break

            you_might_like: list[dict[str, Any]] = []
            for sc, item in scored:
                iid = str(item.get("id") or "").lower()
                if iid not in used_rec_ids:
                    used_rec_ids.add(iid)
                    you_might_like.append(item)
                if len(you_might_like) >= 16:
                    break

            all_recs = more_by_artist + you_might_like

            return {
                "albumId": album_id,
                "primaryArtist": primary_artist,
                "recommendations": all_recs,
                "moreByArtist": more_by_artist,
                "youMightAlsoLike": you_might_like,
            }

        return await self._cached(
            cache_key,
            config.TTL_ALBUM if hasattr(config, "TTL_ALBUM") else 1800,
            compute_recommendations,
            config.STALE_CACHE_TTL if hasattr(config, "STALE_CACHE_TTL") else 3600,
        )

    async def album_details(self, album_id: str) -> dict[str, Any] | None:
        album_id = (album_id or "").strip()
        if not album_id:
            return None

        async def load():
            # 1. Try get_album_info by seokey or numeric id
            try:
                raw = await self.catalog.get_album_info([album_id], True)
                if isinstance(raw, list) and raw:
                    first = raw[0]
                    if isinstance(first, dict) and (first.get("tracks") or first.get("title") or first.get("name")):
                        return album(first)
                elif isinstance(raw, dict) and (raw.get("tracks") or raw.get("title") or raw.get("name")):
                    return album(raw)
            except Exception as exc:
                logger.warning("get_album_info failed id=%s error=%s", album_id, exc)

            # 2. Try searching albums by clean query
            search_query = album_id.replace("-", " ").replace("_", " ")
            try:
                search_res = await self.catalog.search_albums(search_query, 5)
                album_items = items(_clean(search_res), "album")
                for candidate in album_items:
                    cand_id = str(candidate.get("id") or candidate.get("seokey") or "")
                    if cand_id and (cand_id.lower() == album_id.lower() or normalize_query(cand_id) == normalize_query(album_id)):
                        try:
                            full_raw = await self.catalog.get_album_info([cand_id], True)
                            if isinstance(full_raw, list) and full_raw:
                                return album(full_raw[0])
                            elif isinstance(full_raw, dict):
                                return album(full_raw)
                        except Exception:
                            pass
                        return album(candidate)
            except Exception as exc:
                logger.warning("search_albums fallback failed query=%s error=%s", search_query, exc)

            # 3. Try finding songs belonging to this album
            try:
                song_search = await self.catalog.search_songs(search_query, 25)
                song_items = [song(s) for s in items(_clean(song_search), "song") if isinstance(s, dict)]
                matching_songs = [
                    s for s in song_items
                    if (s.get("album_id") == album_id or s.get("album_seokey") == album_id or
                        normalize_query(str(s.get("album") or "")) == normalize_query(search_query))
                ]
                if matching_songs:
                    first = matching_songs[0]
                    return {
                        "id": album_id,
                        "seokey": album_id,
                        "provider_id": first.get("album_id") or album_id,
                        "type": "album",
                        "title": first.get("album") or search_query,
                        "name": first.get("album") or search_query,
                        "image_url": first.get("image_url") or "",
                        "imageUrl": first.get("image_url") or "",
                        "artworkUrl": first.get("image_url") or "",
                        "artist": first.get("artist") or "",
                        "artists": [{"name": first.get("artist") or "", "id": first.get("artist_ids") or ""}],
                        "track_count": len(matching_songs),
                        "trackCount": len(matching_songs),
                        "tracks": matching_songs,
                        "songs": matching_songs,
                    }
            except Exception as exc:
                logger.warning("search_songs fallback failed query=%s error=%s", search_query, exc)

            return None

        return await self._cached(
            f"catalog:album:{album_id}",
            config.TTL_ALBUM if hasattr(config, "TTL_ALBUM") else 21600,
            load,
            config.STALE_CACHE_TTL if hasattr(config, "STALE_CACHE_TTL") else 3600,
        )

    async def home_page(
        self,
        uid: str,
        repository: Any,
        recommendations: Any,
        limit: int,
        offset: int = 0,
        content_type: str = "all",
        refresh_generation: int = 0,
        session_id: str | None = None,
        exclude_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Build one bounded page of the home feed.

        The cursor is applied after composition so every section remains typed
        and the client can append pages without mixing songs with entities.
        ``request_limit`` is bounded and is never the size of the catalogue.
        """
        # Fetch one look-ahead item so ``has_more`` is accurate without
        # requesting the complete catalogue.
        request_limit = min(max(limit + offset + 1, limit), 100)
        # Call recommendations supporting both new parameters and test mocks
        async def _fetch_recs():
            try:
                return await recommendations.recommendations(
                    uid,
                    self.catalog,
                    request_limit,
                    refresh_generation=refresh_generation,
                    session_id=session_id,
                    exclude_ids=exclude_ids,
                )
            except TypeError:
                return await recommendations.recommendations(uid, self.catalog, request_limit)

        profile, history, favorites, albums_raw, artists_raw, playlists_raw, recommended = await asyncio.gather(
            repository.get_profile(uid),
            repository.list_history(uid, request_limit),
            repository.list_favorites(uid),
            repository.list_saved_albums(uid),
            repository.list_followed_artists(uid),
            repository.list_playlists(uid),
            _fetch_recs(),
        )
        sections: list[dict[str, Any]] = []

        def add(section_id: str, section_type: str, title: str, values: list[dict[str, Any]]) -> None:
            if values:
                sections.append({"id": section_id, "type": section_type, "title": title, "items": values})

        # 1. Made For You (Top personalized recommendation tracks)
        add("made_for_you", "songs", "Made For You", [song(value) for value in recommended[:20]])

        # 2. Recently Played (only when user has history)
        if history:
            add("recently_played", "songs", "Recently Played", [song(value) for value in history[:15]])

        # 3. Dynamic taste-based sections from taste profile
        if hasattr(recommendations, "get_taste_profile"):
            try:
                taste_profile = await recommendations.get_taste_profile(uid, session_id=session_id)
                
                # 3a. Session intent section (if user engaged in a session pivot)
                session_arts = taste_profile.current_session.get("artists", {})
                if session_arts:
                    top_session_art = next(iter(session_arts.keys()))
                    session_matches = [
                        song(v) for v in recommended
                        if any(top_session_art in str(a.get("name") if isinstance(a, dict) else a).lower()
                               for a in (v.get("artists") or [v.get("artist")] or []))
                    ][:15]
                    if session_matches:
                        add("because_session", "songs", f"Because You Explored {top_session_art.title()}", session_matches)

                # 3b. "Because You Listened to [Top Artist]"
                top_arts = sorted(taste_profile.artists.items(), key=lambda x: x[1], reverse=True)
                if top_arts:
                    fav_artist_name = top_arts[0][0]
                    artist_matches = [
                        song(v) for v in recommended
                        if any(fav_artist_name in str(a.get("name") if isinstance(a, dict) else a).lower()
                               for a in (v.get("artists") or [v.get("artist")] or []))
                    ][:15]
                    if artist_matches:
                        add("because_favorite_artist", "songs", f"Because You Love {fav_artist_name.title()}", artist_matches)

                # 3c. "Your [Top Language] Mix"
                top_langs = sorted(taste_profile.languages.items(), key=lambda x: x[1], reverse=True)
                if top_langs:
                    fav_lang = top_langs[0][0]
                    lang_matches = [
                        song(v) for v in recommended
                        if str(v.get("language") or "").lower().strip() == fav_lang.lower().strip()
                    ][:15]
                    if lang_matches:
                        add("your_language_mix", "songs", f"Your {fav_lang.title()} Mix", lang_matches)

                # 3d. "On Repeat" (tracks played 2+ times with completion)
                repeat_counts: dict[str, int] = {}
                repeat_tracks: dict[str, dict[str, Any]] = {}
                for h in history:
                    sk = str(h.get("id") or h.get("seokey") or "").lower().strip()
                    if sk:
                        repeat_counts[sk] = repeat_counts.get(sk, 0) + 1
                        if sk not in repeat_tracks:
                            repeat_tracks[sk] = song(h)
                repeated = [repeat_tracks[k] for k, c in repeat_counts.items() if c >= 2][:12]
                if repeated:
                    add("on_repeat", "songs", "On Repeat", repeated)

            except Exception as exc:
                logger.debug("Dynamic section personalization notice: %s", exc)

        # 4. User library sections
        add("liked_songs", "songs", "Liked Songs", [song(value) for value in favorites[:20]])
        add("saved_albums", "albums", "Saved Albums", [album(value) for value in albums_raw[:20]])
        add("followed_artists", "artists", "Followed Artists", [artist(value) for value in artists_raw[:20]])
        add("your_playlists", "playlists", "Your Playlists", [playlist(value) for value in playlists_raw[:20]])

        # 5. Trending & New Releases in user preferred languages
        preferred = self.languages.resolve(profile.get("language_ids") or [])
        if preferred:
            # Rotate language index when refresh_generation > 0 so refreshing explores all user languages
            lang_idx = refresh_generation % len(preferred)
            chosen_lang = preferred[lang_idx].name
            trending, releases = await asyncio.gather(
                self.catalog.get_trending(chosen_lang, limit),
                self.catalog.get_new_releases(chosen_lang, limit),
                return_exceptions=True,
            )
            if not isinstance(trending, Exception):
                add("trending", "songs", f"Trending in {chosen_lang.title()}", items(_clean(trending), "song"))
            if not isinstance(releases, Exception):
                add("new_releases", "albums", f"New Releases in {chosen_lang.title()}", items(_clean(releases), "album"))

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
