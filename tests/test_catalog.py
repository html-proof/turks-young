import json
import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from api.catalog.normalize import album, artist, song
from api.catalog.service import CatalogService, LanguageCatalog
from api.cache.redis_cache import RedisCache
from api.catalog.search.ranking import rank


class FakeCatalog:
    def __init__(self):
        self.search_songs = AsyncMock(return_value=[{
            "seokey": "song-one", "track_id": "1", "title": "Song One",
            "artists": "Artist One", "artist_seokeys": "artist-one",
            "album": "Album One", "album_seokey": "album-one", "duration": "03:00",
        }])
        self.search_artists = AsyncMock(return_value=[{
            "seokey": "artist-one", "artist_id": "10", "name": "Artist One",
            "images": {"urls": {"large_artwork": "https://images.test/artist.jpg"}},
        }])
        self.search_albums = AsyncMock(return_value=[{
            "seokey": "album-one", "album_id": "20", "title": "Album One",
            "artists": "Artist One", "artist_seokeys": "artist-one",
        }])
        self.search_playlists = AsyncMock(return_value=[{
            "seokey": "playlist-one", "title": "Playlist One",
        }])
        self.get_trending = AsyncMock(return_value=[])
        self.get_new_releases = AsyncMock(return_value={
            "tracks": [],
            "albums": [{"seokey": "new-album", "title": "New Album"}],
        })
        self.get_artist_info = AsyncMock(return_value=[{
            "seokey": "artist-from-song", "artist_id": "91", "name": "Artist From Song",
            "images": {"urls": {"large_artwork": "https://images.test/resolved.jpg"}},
        }])


class FakeHomeRepository:
    async def get_profile(self, uid): return {"language_ids": []}
    async def list_history(self, uid, limit): return [
        {"seokey": "song-1", "title": "Song One", "artists": "Artist One"},
        {"seokey": "song-2", "title": "Song Two", "artists": "Artist Two"},
    ][:limit]
    async def list_favorites(self, uid): return []
    async def list_saved_albums(self, uid): return [{"seokey": "album-1", "title": "Album One"}]
    async def list_followed_artists(self, uid): return [{"seokey": "artist-1", "name": "Artist One"}]
    async def list_playlists(self, uid): return [{"seokey": "playlist-1", "name": "Playlist One"}]


class FakeRecommendations:
    async def recommendations(self, uid, catalog, limit): return []


def configured_languages():
    return LanguageCatalog.from_json(json.dumps([{
        "id": "language-one",
        "name": "Language One",
        "native_name": "Language One",
        "image_url": "https://images.test/language.jpg",
    }]))


def test_empty_language_configuration_stays_empty():
    assert LanguageCatalog.from_json("").languages == []


def test_language_configuration_rejects_duplicates():
    raw = json.dumps([
        {"id": "same", "name": "One", "native_name": "One"},
        {"id": "same", "name": "Two", "native_name": "Two"},
    ])
    with pytest.raises(RuntimeError):
        LanguageCatalog.from_json(raw)


def test_canonical_models_keep_types_and_provider_images():
    raw_artist = {
        "seokey": "artist-one", "name": "Artist One",
        "images": {"urls": {"large_artwork": "https://images.test/a.jpg"}},
    }
    raw_song = {
        "seokey": "song-one", "title": "Song One", "artists": "Artist One",
        "artist_seokeys": "artist-one", "duration": "03:35",
    }
    raw_album = {"seokey": "album-one", "title": "Album One", "artists": "Artist One"}

    assert artist(raw_artist)["type"] == "artist"
    assert artist(raw_artist)["image_url"] == "https://images.test/a.jpg"
    assert artist(raw_artist)["imageUrl"] == "https://images.test/a.jpg"
    assert song(raw_song)["type"] == "song"
    assert song(raw_song)["duration_ms"] == 215_000
    assert song({**raw_song, "language": "Tamil"})["language"] == "Tamil"
    assert album(raw_album)["type"] == "album"
    assert album(raw_album)["title"] == "Album One"
    assert album(raw_album)["artistNames"] == ["Artist One"]
    assert album(raw_album)["artworkUrl"] is None


def test_artist_image_provider_aliases_are_normalized():
    for field in ("image", "image_url", "imageUrl", "thumbnail", "photo", "artist_image"):
        value = artist({"id": "artist-one", "name": "Artist One", field: "https://images.test/a.jpg"})
        assert value["imageUrl"] == "https://images.test/a.jpg"


def test_search_prefers_real_word_matches_over_fuzzy_neighbours():
    values = [
        {"id": "wrong", "type": "song", "title": "Chennai Pattanam"},
        {"id": "right", "type": "song", "title": "Pattampoochi Pattalam"},
    ]

    results = rank("pattalam", values, "song", 10)

    assert [item["id"] for item in results] == ["right"]


def test_movie_search_keeps_tamil_soundtrack_album_with_same_title():
    values = [
        {"id": "sarkar-hindi", "type": "album", "name": "Sarkar", "language": "Hindi"},
        {"id": "sarkar-tamil", "type": "album", "name": "Sarkar (Tamil) (Original Motion Picture Soundtrack)", "language": "Tamil"},
    ]

    results = rank("sarkar", values, "album", 10)

    assert {item["id"] for item in results} == {"sarkar-hindi", "sarkar-tamil"}


@pytest.mark.asyncio
async def test_all_search_results_are_categorized():
    catalog = FakeCatalog()
    service = CatalogService(catalog, configured_languages())

    result = await service.search("one", None, 1, 20)

    assert result["top_result"]["type"] == "artist"
    assert result["artists"][0]["type"] == "artist"
    assert result["songs"][0]["type"] == "song"
    assert result["albums"][0]["type"] == "album"
    assert result["playlists"][0]["type"] == "playlist"


@pytest.mark.asyncio
async def test_filtered_search_has_pagination_contract():
    catalog = FakeCatalog()
    service = CatalogService(catalog, configured_languages())

    result = await service.search("one", "song", 1, 20)

    assert result == {
        "items": [result["items"][0]],
        "page": 1,
        "limit": 20,
        "has_more": False,
        "type": "song",
    }


@pytest.mark.asyncio
async def test_artist_onboarding_uses_configured_language_and_provider_image():
    catalog = FakeCatalog()
    service = CatalogService(catalog, configured_languages())

    values = await service.artists_for_languages(["language-one"], 10)

    catalog.search_artists.assert_awaited_once_with("Language One", 11)
    assert values[0]["id"] == "artist-one"
    assert values[0]["image_url"] == "https://images.test/artist.jpg"


@pytest.mark.asyncio
async def test_artist_onboarding_resolves_images_for_song_only_artists():
    catalog = FakeCatalog()
    catalog.search_artists = AsyncMock(return_value=[])
    catalog.get_trending = AsyncMock(return_value=[{
        "seokey": "track-one", "title": "Track One", "artists": "Artist From Song",
        "artist_seokeys": "artist-from-song",
    }])
    service = CatalogService(catalog, configured_languages())

    values = await service.artists_for_languages(["language-one"], 10)

    assert values[0]["id"] == "artist-from-song"
    assert values[0]["imageUrl"] == "https://images.test/resolved.jpg"
    catalog.get_artist_info.assert_awaited()


@pytest.mark.asyncio
async def test_discovery_reads_nested_new_release_albums():
    catalog = FakeCatalog()
    service = CatalogService(catalog, configured_languages())

    sections = await service.discover(10)

    releases = next(section for section in sections if section["id"] == "new_releases")
    assert releases["items"][0]["id"] == "new-album"


@pytest.mark.asyncio
async def test_artist_languages_are_fetched_concurrently_and_cached_per_language():
    languages = LanguageCatalog.from_json(json.dumps([
        {"id": name.lower(), "name": name, "native_name": name}
        for name in ("Hindi", "Tamil", "Malayalam", "English")
    ]))

    class SlowCatalog(FakeCatalog):
        def __init__(self):
            super().__init__()
            for attr in ("get_trending", "search_artists", "search_songs"):
                delattr(self, attr)
            self.calls = 0

        async def get_trending(self, language, limit):
            self.calls += 1
            await asyncio.sleep(0.05)
            return []

        async def search_artists(self, query, limit):
            self.calls += 1
            await asyncio.sleep(0.05)
            return [{"seokey": f"{query.lower()}-artist", "name": query}]

        async def search_songs(self, query, limit):
            self.calls += 1
            await asyncio.sleep(0.05)
            return []

    catalog = SlowCatalog()
    cache = RedisCache("", "")
    service = CatalogService(catalog, languages, cache)
    started = time.perf_counter()
    first = await service.artists_for_languages(["hindi", "tamil", "malayalam", "english"], 10)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.25  # four language groups run in parallel, not 4 x 150 ms
    assert len(first) == 4
    assert catalog.calls == 12

    await service.artists_for_languages(["hindi", "tamil", "malayalam", "english"], 10)
    assert catalog.calls == 12


@pytest.mark.asyncio
async def test_cache_coalesces_concurrent_misses():
    cache = RedisCache("", "")
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return {"ok": True}

    values = await asyncio.gather(*[
        cache.get_or_set("same-key", loader, 60) for _ in range(20)
    ])
    assert calls == 1
    assert all(value == {"ok": True} for value in values)


@pytest.mark.asyncio
async def test_home_page_filters_typed_content_and_paginates():
    service = CatalogService(FakeCatalog(), configured_languages())
    repository = FakeHomeRepository()
    recommendations = FakeRecommendations()

    albums = await service.home_page("u1", repository, recommendations, 1, 0, "album")
    assert albums["content_type"] == "album"
    assert albums["sections"][0]["items"][0]["type"] == "album"
    assert albums["sections"][0]["items"][0]["id"] == "album-1"

    songs = await service.home_page("u1", repository, recommendations, 1, 0, "song")
    assert all(item["type"] == "song" for section in songs["sections"] for item in section["items"])
    assert songs["has_more"] is True
    assert songs["next_cursor"] == "1"
