import asyncio
import json
from typing import Any

from pydantic import TypeAdapter, ValidationError

from api.catalog.models import Language
from api.catalog.normalize import album, artist, items, playlist, song


class LanguageCatalog:
    def __init__(self, languages: list[Language]):
        self.languages = languages
        self.by_id = {language.id: language for language in languages}

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
        return [self.by_id[item] for item in ids if item in self.by_id]


def _clean(result: Any) -> Any:
    return [] if isinstance(result, dict) and "error" in result else result


class CatalogService:
    def __init__(self, catalog: Any, languages: LanguageCatalog):
        self.catalog = catalog
        self.languages = languages

    async def artists_for_languages(self, language_ids: list[str], limit: int) -> list[dict[str, Any]]:
        languages = self.languages.resolve(language_ids)
        if not languages:
            return []
        per_language = max(1, limit // len(languages) + 1)
        results = await asyncio.gather(*[
            self.catalog.search_artists(language.name, per_language) for language in languages
        ], return_exceptions=True)
        unique: dict[str, dict[str, Any]] = {}
        for result in results:
            if isinstance(result, Exception):
                continue
            for value in items(_clean(result), "artist"):
                unique.setdefault(value["id"], value)
        return list(unique.values())[:limit]

    async def search(self, query: str, kind: str | None, page: int, limit: int) -> dict[str, Any]:
        methods = {
            "song": self.catalog.search_songs,
            "artist": self.catalog.search_artists,
            "album": self.catalog.search_albums,
            "playlist": self.catalog.search_playlists,
        }
        if kind:
            requested = page * limit
            result = _clean(await methods[kind](query, requested + 1))
            normalized = items(result, kind)
            start = (page - 1) * limit
            page_items = normalized[start:start + limit]
            return {
                "items": page_items,
                "page": page,
                "limit": limit,
                "has_more": len(normalized) > start + limit,
                "type": kind,
            }

        preview = min(limit, 6)
        results = await asyncio.gather(*[
            method(query, preview) for method in methods.values()
        ], return_exceptions=True)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for result_kind, result in zip(methods, results):
            grouped[f"{result_kind}s"] = [] if isinstance(result, Exception) else items(_clean(result), result_kind)
        priority = ("artists", "songs", "albums", "playlists")
        top = next(
            ({"type": key[:-1], "item": grouped[key][0]} for key in priority if grouped[key]),
            None,
        )
        return {"query": query, "top_result": top, **grouped}

    async def discover(self, limit: int) -> list[dict[str, Any]]:
        if not self.languages.languages:
            return []
        language = self.languages.languages[0]
        trending, releases, artists_result = await asyncio.gather(
            self.catalog.get_trending(language.name, limit),
            self.catalog.get_new_releases(language.name, limit),
            self.catalog.search_artists(language.name, limit),
            return_exceptions=True,
        )
        candidates = [
            ("trending", "songs", trending, "song"),
            ("new_releases", "albums", releases, "album"),
            ("popular_artists", "artists", artists_result, "artist"),
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
        result = _clean(await self.catalog.get_album_info([album_id], True))
        if not isinstance(result, list) or not result:
            return None
        normalized_album = album(result[0])
        if not normalized_album["id"] or not normalized_album["name"]:
            return None
        return normalized_album

    async def home(self, uid: str, repository: Any, recommendations: Any, limit: int) -> list[dict[str, Any]]:
        profile, history, favorites, albums_raw, artists_raw, playlists_raw, recommended = await asyncio.gather(
            repository.get_profile(uid),
            repository.list_history(uid, limit),
            repository.list_favorites(uid),
            repository.list_saved_albums(uid),
            repository.list_followed_artists(uid),
            repository.list_playlists(uid),
            recommendations.recommendations(uid, self.catalog, limit),
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
        return sections
