import pytest
from unittest.mock import AsyncMock, MagicMock
from api.catalog.service import CatalogService, LanguageCatalog
from api.catalog.models import Language


@pytest.fixture
def mock_catalog():
    catalog = MagicMock()
    # Mock album details (matches format_json_albums output)
    catalog.get_album_info = AsyncMock(return_value=[{
        "seokey": "test-album-1",
        "title": "Test Album 1",
        "language": "Hindi",
        "release_date": "2024-01-01",
        "artists": "Arijit Singh",
        "artist_seokeys": "arijit-singh",
        "recordlevel": "Romantic",
        "tracks": [{"track_id": "1", "title": "Song 1", "artists": "Arijit Singh"}]
    }])
    
    # Mock search albums
    catalog.search_albums = AsyncMock(return_value=[
        {
            "seokey": "arijit-hit-album",
            "title": "Arijit Hit Album",
            "language": "Hindi",
            "release_date": "2024-05-01",
            "artists": "Arijit Singh",
            "artist_seokeys": "arijit-singh",
            "track_count": 8,
        },
        {
            "seokey": "test-album-1", # Same album - should be excluded
            "title": "Test Album 1",
            "language": "Hindi",
            "release_date": "2024-01-01",
            "artists": "Arijit Singh",
            "artist_seokeys": "arijit-singh",
        },
        {
            "seokey": "shreya-hit-album",
            "title": "Shreya Romantic Hits",
            "language": "Hindi",
            "release_date": "2023-01-01",
            "artists": "Shreya Ghoshal",
            "artist_seokeys": "shreya-ghoshal",
            "track_count": 10,
        }
    ])
    catalog.get_new_releases = AsyncMock(return_value=[
        {
            "seokey": "new-hindi-album",
            "title": "New Hindi Album",
            "language": "Hindi",
            "release_date": "2025-01-01",
            "artists": "Vishal Mishra",
            "artist_seokeys": "vishal-mishra",
            "track_count": 6,
        }
    ])
    return catalog


@pytest.fixture
def service(mock_catalog):
    languages = LanguageCatalog([Language(id="hindi", name="Hindi", native_name="हिन्दी")])
    return CatalogService(mock_catalog, languages, None)


@pytest.mark.asyncio
async def test_album_recommendations_ranking(service):
    res = await service.album_recommendations("test-album-1")
    assert res["albumId"] == "test-album-1"
    assert res["primaryArtist"] == "Arijit Singh"
    
    # Verify current album is excluded
    all_rec_ids = [r["id"] for r in res["recommendations"]]
    assert "test-album-1" not in all_rec_ids
    
    # Verify same-artist albums are separated into moreByArtist and have same_artist reason
    assert len(res["moreByArtist"]) > 0
    assert res["moreByArtist"][0]["id"] == "arijit-hit-album"
    assert res["moreByArtist"][0]["reason"] == "same_artist"
    
    # Verify other recommendations are in youMightAlsoLike
    assert len(res["youMightAlsoLike"]) > 0
    ym_ids = [r["id"] for r in res["youMightAlsoLike"]]
    assert "shreya-hit-album" in ym_ids or "new-hindi-album" in ym_ids


@pytest.mark.asyncio
async def test_album_recommendations_missing_album(service, mock_catalog):
    mock_catalog.get_album_info = AsyncMock(return_value=[])
    res = await service.album_recommendations("non-existent")
    assert res["albumId"] == "non-existent"
    assert res["recommendations"] == []
    assert res["moreByArtist"] == []
    assert res["youMightAlsoLike"] == []


@pytest.mark.asyncio
async def test_album_recommendations_deduplication(service, mock_catalog):
    mock_catalog.search_albums = AsyncMock(return_value=[
        {
            "seokey": "duplicate-album-1",
            "title": "Arijit Superhit",
            "language": "Hindi",
            "release_date": "2024-05-01",
            "artists": "Arijit Singh",
            "artist_seokeys": "arijit-singh",
        },
        {
            "seokey": "duplicate-album-2",
            "title": "Arijit Superhit (Original Motion Picture Soundtrack)",
            "language": "Hindi",
            "release_date": "2024-05-01",
            "artists": "Arijit Singh",
            "artist_seokeys": "arijit-singh",
        },
        {
            "seokey": "test-album-1-ost",
            "title": "Test Album 1 (OST)",
            "language": "Hindi",
            "release_date": "2024-01-01",
            "artists": "Arijit Singh",
            "artist_seokeys": "arijit-singh",
        },
    ])
    mock_catalog.get_new_releases = AsyncMock(return_value=[])
    res = await service.album_recommendations("test-album-1")
    
    rec_ids = [r["id"] for r in res["recommendations"]]
    rec_titles = [r["title"] for r in res["recommendations"]]
    
    # "Test Album 1 (OST)" should be stripped as duplicate of current album
    assert "test-album-1-ost" not in rec_ids
    # "Arijit Superhit" duplicate variant should appear only once
    assert len([t for t in rec_titles if "Arijit Superhit" in t]) == 1
