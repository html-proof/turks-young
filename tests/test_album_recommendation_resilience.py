from unittest.mock import AsyncMock, MagicMock

import pytest

from api.catalog import service as service_module
from api.catalog.models import Language
from api.catalog.service import CatalogService, LanguageCatalog


class RecordingCache:
    def __init__(self):
        self.store = {}
        self.ttls = {}

    async def get_with_stale(self, key):
        return self.store.get(key), key in self.store

    async def set_with_stale(self, key, value, ttl, stale_ttl=None):
        self.store[key] = value
        self.ttls[key] = (ttl, stale_ttl)


def _service(search_results, cache=None):
    catalog = MagicMock()
    catalog.get_album_info = AsyncMock(return_value=[{
        "seokey": "current-album", "title": "Current Album", "language": "Hindi",
        "release_date": "2024-01-01", "artists": "Arijit Singh", "artist_seokeys": "arijit-singh",
        "tracks": [{"track_id": "1", "title": "Song 1", "artists": "Arijit Singh"}],
    }])
    catalog.search_albums = AsyncMock(return_value=search_results)
    catalog.get_new_releases = AsyncMock(return_value=[])
    languages = LanguageCatalog([Language(id="hindi", name="Hindi", native_name="हिन्दी")])
    svc = CatalogService(catalog, languages, None)
    svc.cache = cache
    return svc


def _album(seokey, title, artist="Arijit Singh", **extra):
    return {"seokey": seokey, "title": title, "language": "Hindi", "release_date": "2023-01-01",
            "artists": artist, "artist_seokeys": artist.lower().replace(" ", "-"),
            "track_count": 5, **extra}


@pytest.mark.asyncio
async def test_single_candidate_failure_does_not_fail_response(monkeypatch, caplog):
    real_album = service_module.album

    def flaky_album(raw):
        if raw.get("seokey") == "broken-album":
            raise ValueError("malformed provider record")
        return real_album(raw)

    monkeypatch.setattr(service_module, "album", flaky_album)
    svc = _service([_album("broken-album", "Broken"), _album("good-album", "Good Album"),
                    _album("odd-year", "Odd Year", release_date="unknown")])
    res = await svc.album_recommendations("current-album")

    ids = [r["id"] for r in res["recommendations"]]
    assert "good-album" in ids
    assert "odd-year" in ids
    assert "broken-album" not in ids
    assert "RECOMMENDATION_SOURCE_FAILED" in caplog.text


@pytest.mark.asyncio
async def test_album_details_failure_returns_empty_payload(caplog):
    svc = _service([])
    svc.album_details = AsyncMock(side_effect=RuntimeError("provider down"))
    res = await svc.album_recommendations("current-album")
    assert res["recommendations"] == []
    assert "RECOMMENDATION_SOURCE_FAILED" in caplog.text


@pytest.mark.asyncio
async def test_empty_recommendations_are_not_cached_long():
    cache = RecordingCache()
    svc = _service([], cache)
    res = await svc.album_recommendations("current-album")
    assert res["recommendations"] == []
    ttl, stale_ttl = cache.ttls["music:album:recs:current-album:v3"]
    assert ttl <= 60
    assert not stale_ttl


@pytest.mark.asyncio
async def test_non_empty_recommendations_use_album_ttl():
    cache = RecordingCache()
    svc = _service([_album("good-album", "Good Album")], cache)
    await svc.album_recommendations("current-album")
    ttl, _ = cache.ttls["music:album:recs:current-album:v3"]
    assert ttl > 60


@pytest.mark.asyncio
async def test_duplicate_candidates_are_merged_by_fingerprint():
    svc = _service([
        _album("tum-hi-ho-1", "Tum Hi Ho"),
        _album("tum-hi-ho-2", "Tum Hi Ho (Original Motion Picture Soundtrack)"),
        _album("tum-hi-ho-3", "TUM HI HO!"),
        _album("kesariya-hi", "केसरिया"),
        _album("kesariya-hi-2", "केसरिया (Deluxe)"),
    ])
    res = await svc.album_recommendations("current-album")
    ids = [r["id"] for r in res["recommendations"]]
    assert len([i for i in ids if i.startswith("tum-hi-ho")]) == 1
    assert len([i for i in ids if i.startswith("kesariya")]) == 1
