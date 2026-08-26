import json
from unittest.mock import AsyncMock

import pytest

from api.catalog.normalize import album, artist, song
from api.catalog.service import CatalogService, LanguageCatalog


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
    assert song(raw_song)["type"] == "song"
    assert song(raw_song)["duration_ms"] == 215_000
    assert album(raw_album)["type"] == "album"


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
async def test_discovery_reads_nested_new_release_albums():
    catalog = FakeCatalog()
    service = CatalogService(catalog, configured_languages())

    sections = await service.discover(10)

    releases = next(section for section in sections if section["id"] == "new_releases")
    assert releases["items"][0]["id"] == "new-album"
