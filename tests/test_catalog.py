import json
import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from api.catalog.normalize import album, artist, song
from api.catalog.service import CatalogService, LanguageCatalog, _apply_album_artwork
from api.cache.redis_cache import RedisCache
from api.catalog.search.ranking import rank
from api.provider_search import encoded_query, search_entries


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


def test_song_never_uses_artist_portrait_as_artwork():
    value = song({
        "id": "song-one",
        "title": "Song One",
        "artists": "Artist One",
        "artist_image": "https://images.test/artist.jpg",
    })
    assert value["image_url"] is None


def test_song_prioritizes_high_quality_and_preserves_stream_urls():
    value = song({
        "id": "song-one",
        "title": "Song One",
        "artists": "Artist One",
        "stream_urls": {
            "urls": {
                "very_high_quality": "https://cdn.gaana.com/320.mp4",
                "high_quality": "https://cdn.gaana.com/128.mp4",
                "medium_quality": "https://cdn.gaana.com/64.mp4",
                "low_quality": "https://cdn.gaana.com/16.mp4",
            }
        }
    })
    assert value["stream_url"] == "https://cdn.gaana.com/128.mp4"
    assert value["stream_urls"]["urls"]["high_quality"] == "https://cdn.gaana.com/128.mp4"
    assert value["stream_urls"]["urls"]["very_high_quality"] == "https://cdn.gaana.com/320.mp4"


def test_search_song_uses_matching_album_artwork():
    songs = [{
        "id": "song-one",
        "title": "Song One",
        "album": {"id": "album-one", "title": "Album One"},
        "image_url": None,
    }]
    albums = [{
        "id": "album-one",
        "title": "Album One",
        "artworkUrl": "https://images.test/album.jpg",
    }]
    result = _apply_album_artwork(songs, albums)
    assert result[0]["image_url"] == "https://images.test/album.jpg"
    assert result[0]["album"]["artworkUrl"] == "https://images.test/album.jpg"


def test_search_prefers_real_word_matches_over_fuzzy_neighbours():
    values = [
        {"id": "wrong", "type": "song", "title": "Chennai Pattanam"},
        {"id": "right", "type": "song", "title": "Pattampoochi Pattalam"},
    ]

    results = rank("pattalam", values, "song", 10)

    assert [item["id"] for item in results] == ["right"]


def test_search_prefers_exact_title_and_artist_over_title_only_hit():
    values = [
        {"id": "cover", "type": "song", "title": "Believer", "artists": "Local Covers"},
        {"id": "original", "type": "song", "title": "Believer", "artists": "Imagine Dragons"},
        {"id": "wrong-artist", "type": "song", "title": "Believer", "artists": "Rag'n'Bone Man"},
    ]

    results = rank("believer imagine dragons", values, "song", 10)

    assert results[0]["id"] == "original"


def test_search_title_and_artist_match_outranks_a_longer_title_match():
    values = [
        {"id": "title-only", "type": "song", "title": "Believer Imagine Dragons Lyrics", "artists": "Unofficial"},
        {"id": "original", "type": "song", "title": "Believer", "artists": "Imagine Dragons"},
    ]

    results = rank("believer imagine dragons", values, "song", 10)

    assert results[0]["id"] == "original"


def test_exact_song_search_does_not_include_broad_old_matches():
    values = [
        {"id": "exact", "type": "song", "title": "Believer", "artists": "Imagine Dragons"},
        {"id": "old", "type": "song", "title": "Believer - Live Cover", "artists": "A Cover Band"},
        {"id": "related", "type": "song", "title": "Believer Stories", "artists": "Another Artist"},
    ]

    results = rank("believer", values, "song", 10)

    assert [item["id"] for item in results] == ["exact"]


def test_movie_search_keeps_tamil_soundtrack_album_with_same_title():
    values = [
        {"id": "sarkar-hindi", "type": "album", "name": "Sarkar", "language": "Hindi"},
        {"id": "sarkar-tamil", "type": "album", "name": "Sarkar (Tamil) (Original Motion Picture Soundtrack)", "language": "Tamil"},
    ]

    results = rank("sarkar", values, "album", 10)

    assert {item["id"] for item in results} == {"sarkar-hindi", "sarkar-tamil"}


def test_provider_search_accepts_new_result_envelope_and_encodes_query():
    assert encoded_query("Jilla songs") == "Jilla%20songs"
    assert search_entries({"data": {"results": [{"seokey": "jilla-song"}]}}) == [
        {"seokey": "jilla-song"}
    ]


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

    values = [
        {"id": "sarkar-hindi", "type": "album", "name": "Sarkar", "language": "Hindi"},
        {"id": "sarkar-tamil", "type": "album", "name": "Sarkar (Tamil) (Original Motion Picture Soundtrack)", "language": "Tamil"},
    ]

    results = rank("sarkar", values, "album", 10)

    assert {item["id"] for item in results} == {"sarkar-hindi", "sarkar-tamil"}


def test_provider_search_accepts_new_result_envelope_and_encodes_query():
    assert encoded_query("Jilla songs") == "Jilla%20songs"
    assert search_entries({"data": {"results": [{"seokey": "jilla-song"}]}}) == [
        {"seokey": "jilla-song"}
    ]


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


def test_ranking_ghilli_movie_and_songs_dominate_results():
    values = [
        {"id": "ghilli-album-song", "type": "song", "title": "Appadi Podu", "album": "Ghilli", "artists": "KK, Anuradha Sriram"},
        {"id": "unrelated-pop-song", "type": "song", "title": "Vaathi Coming", "album": "Master", "artists": "Anirudh", "popularity": 1.0},
        {"id": "ghilli-title-song", "type": "song", "title": "Ghilli Theme", "album": "Ghilli", "artists": "Vidyasagar"},
        {"id": "ghilli-exact-song", "type": "song", "title": "Ghilli", "album": "Ghilli", "artists": "Vidyasagar"},
    ]
    results = rank("Ghilli", values, "song", 10)
    # The exact song title "Ghilli" must be first
    assert results[0]["id"] == "ghilli-exact-song"
    # All Ghilli songs must rank above unrelated popular songs
    ghilli_ids = {"ghilli-album-song", "ghilli-title-song", "ghilli-exact-song"}
    top_3_ids = {r["id"] for r in results[:3]}
    assert ghilli_ids == top_3_ids


def test_ranking_exact_song_appadi_podu_first():
    values = [
        {"id": "cover", "type": "song", "title": "Appadi Podu (Remix)", "artists": "DJ X"},
        {"id": "exact", "type": "song", "title": "Appadi Podu", "album": "Ghilli", "artists": "KK, Anuradha Sriram"},
        {"id": "related", "type": "song", "title": "Appadi Podu Short Version", "artists": "KK"},
    ]
    results = rank("Appadi Podu", values, "song", 10)
    assert results[0]["id"] == "exact"


def test_ranking_artist_plus_song_ar_rahman_vennilave():
    values = [
        {"id": "wrong-artist-song", "type": "song", "title": "Vennilave", "artists": "Hariharan, Deva", "album": "Other Movie"},
        {"id": "ar-rahman-exact", "type": "song", "title": "Vennilave", "artists": "A.R. Rahman, Hariharan", "album": "Minsara Kanavu"},
        {"id": "random-rahman", "type": "song", "title": "Urvasi Urvasi", "artists": "A.R. Rahman", "album": "Kadhalan"},
    ]
    results = rank("AR Rahman Vennilave", values, "song", 10)
    assert results[0]["id"] == "ar-rahman-exact"


def test_ranking_malayalam_intent_vs_partial_character_match():
    values = [
        {"id": "mal-song", "type": "song", "title": "Malayalam Super Hits", "language": "malayalam"},
        {"id": "partial-match", "type": "song", "title": "Mala Mala", "language": "tamil", "popularity": 1.0},
    ]
    results = rank("Malayalam", values, "song", 10)
    assert results[0]["id"] == "mal-song"


def test_ranking_illuminati_exact_song_before_trending_songs():
    values = [
        {"id": "aavesham-other", "type": "song", "title": "Jaada", "album": "Aavesham", "artists": "Sushin Shyam", "popularity": 1.0},
        {"id": "illuminati-exact", "type": "song", "title": "Illuminati", "album": "Aavesham", "artists": "Sushin Shyam, Dabzee", "popularity": 0.5},
        {"id": "unrelated-viral", "type": "song", "title": "Tauba Tauba", "artists": "Karan Aujla", "popularity": 1.0},
    ]
    results = rank("illuminati", values, "song", 10)
    assert results[0]["id"] == "illuminati-exact"


def test_ranking_tier_a_beats_high_popularity_tier_c():
    values = [
        {"id": "tier-c-viral", "type": "song", "title": "Mega Viral Hit", "artists": "Famous Singer", "popularity": 1.0},
        {"id": "tier-a-exact", "type": "song", "title": "Nee Singam Dhan", "artists": "A.R. Rahman", "popularity": 0.05},
    ]
    results = rank("Nee Singam Dhan", values, "song", 10)
    assert results[0]["id"] == "tier-a-exact"


def test_ranking_personalization_cannot_defeat_exact_intent():
    # User normally prefers Malayalam, but explicitly searches for a Tamil song "Ghilli"
    values = [
        {"id": "tamil-exact", "type": "song", "title": "Ghilli", "language": "tamil", "artists": "Vidyasagar"},
        {"id": "malayalam-unrelated", "type": "song", "title": "Ghilli Malayalam Dubbed", "language": "malayalam", "artists": "Other"},
    ]
    results = rank(
        "Ghilli",
        values,
        "song",
        10,
        user_languages=["malayalam"],
        previous_searches=["malayalam"],
    )
    assert results[0]["id"] == "tamil-exact"


@pytest.mark.asyncio
async def test_catalog_service_loads_curated_artists_from_db():
    languages = LanguageCatalog.from_json(json.dumps([
        {"id": "tamil", "name": "Tamil", "native_name": "Tamil"}
    ]))

    class MockConn:
        async def fetch(self, query, *args):
            return [
                {
                    "id": "anirudh-ravichander",
                    "name": "Anirudh Ravichander",
                    "image_url": "https://cdn.example.com/anirudh.jpg",
                    "metadata": {},
                }
            ]

    class MockPool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(self):
                    return MockConn()
                async def __aexit__(self, *a):
                    pass
            return _Ctx()

    catalog = FakeCatalog()
    service = CatalogService(catalog, languages, db_pool=MockPool())
    res = await service.artist_page(["tamil"], 10)
    assert len(res["items"]) >= 1
    first = res["items"][0]
    assert first["id"] == "anirudh-ravichander"
    assert first["name"] == "Anirudh Ravichander"
    assert first["image_url"] == "https://cdn.example.com/anirudh.jpg"
    assert first["image_status"] == "verified"


@pytest.mark.asyncio
async def test_catalog_service_handles_db_failure_gracefully():
    languages = LanguageCatalog.from_json(json.dumps([
        {"id": "hindi", "name": "Hindi", "native_name": "Hindi"}
    ]))

    class FailingPool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(self):
                    raise RuntimeError("Database unreachable")
                async def __aexit__(self, *a):
                    pass
            return _Ctx()

    catalog = FakeCatalog()
    service = CatalogService(catalog, languages, db_pool=FailingPool())
    # Should not raise, falls back to catalog provider
    res = await service.artist_page(["hindi"], 10)
    assert isinstance(res["items"], list)


def test_artist_normalization_idempotence_and_stringified_dict_recovery():
    # 1. Normalizing raw song
    raw = {
        "id": "ethir-neechal-14",
        "title": "Ethir Neechal",
        "artists": "Anirudh Ravichander, Yo Yo Honey Singh, Hiphop Tamizha",
        "artist_seokeys": "anirudh-ravichander, yo-yo-honey-singh, hip-hop-tamizha",
    }
    norm1 = song(raw)
    assert len(norm1["artists"]) == 3
    assert norm1["artist"]["name"] == "Anirudh Ravichander"
    assert norm1["artist"]["id"] == "anirudh-ravichander"
    assert norm1["artists"][2]["name"] == "Hiphop Tamizha"
    assert norm1["artists"][2]["id"] == "hip-hop-tamizha"

    # 2. Idempotent: re-normalizing already normalized song
    norm2 = song(norm1)
    assert norm2["artist"]["name"] == "Anirudh Ravichander"
    assert not norm2["artist"]["name"].startswith("{")
    assert norm2["artists"][2]["name"] == "Hiphop Tamizha"
    assert not norm2["artists"][2]["name"].startswith("{")

    # 3. Corrupted stringified dictionaries recovery
    corrupted = {
        "id": "corrupted-1",
        "title": "Corrupted Song",
        "artists": ["{'id': 'hip-hop-tamizha', 'name': 'Hiphop Tamizha'}", "{'id': 'pritam', 'name': 'Pritam'}"],
    }
    norm3 = song(corrupted)
    assert norm3["artist"]["name"] == "Hiphop Tamizha"
    assert norm3["artist"]["id"] == "hip-hop-tamizha"
    assert norm3["artists"][1]["name"] == "Pritam"
    assert norm3["artists"][1]["id"] == "pritam"


def test_artist_portrait_extraction_from_artist_detail():
    raw_song = {
        "id": "arabic-kuthu",
        "title": "Arabic Kuthu",
        "artist_image": "https://a10.gaanacdn.com/gn_img/artists/a7LWBaz3zX/7LWB0AOKzX/size_m_1716892617.webp",
        "artist_detail": [
            {
                "artist_id": "54397",
                "seokey": "anirudh-ravichander",
                "name": "Anirudh Ravichander",
                "atw": "https://a10.gaanacdn.com/gn_img/artists/a7LWBaz3zX/7LWB0AOKzX/size_m_1716892617.webp",
            },
            {
                "artist_id": "171956",
                "seokey": "jonita-gandhi",
                "name": "Jonita Gandhi",
                "atw": "https://a10.gaanacdn.com/gn_img/artists/BZgWoQOK2d/ZgWozE4m32/size_m_1720782848.webp",
            },
        ],
        "artists": "Anirudh Ravichander, Jonita Gandhi",
    }
    norm = song(raw_song)
    assert norm["artist_image"] == "https://a10.gaanacdn.com/gn_img/artists/a7LWBaz3zX/7LWB0AOKzX/size_l_1716892617.webp"
    assert len(norm["artists"]) == 2
    assert norm["artists"][0]["image_url"] == "https://a10.gaanacdn.com/gn_img/artists/a7LWBaz3zX/7LWB0AOKzX/size_l_1716892617.webp"
    assert norm["artists"][1]["image_url"] == "https://a10.gaanacdn.com/gn_img/artists/BZgWoQOK2d/ZgWozE4m32/size_l_1720782848.webp"
    assert norm["artist"]["image_url"] == norm["artists"][0]["image_url"]


