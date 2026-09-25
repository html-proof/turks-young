import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient

from api.catalog.models import (
    CanonicalTrack,
    CanonicalArtwork,
    CanonicalPlayback,
    AlternateSource,
)
from api.catalog.artwork import ArtworkResolver, normalize_url, artwork_cache_key
from api.songs.playback import PlaybackResolver, validate_stream, identity_score, playback_cache_key
from app import app


def test_canonical_track_parsing_and_legacy_export():
    raw_data = {
        "track_id": "test-uuid-1",
        "title": "Kannum Kannum Nokia",
        "artists": ["Leslie Lewis", "Vasundhara Das"],
        "album": "Anniyan",
        "album_id": "anniyan-123",
        "duration_ms": 248000,
        "source": "gaana",
        "source_track_id": "gaana-track-1",
        "alternate_sources": [
            {"source": "jiosaavn", "source_track_id": "saavn-track-1"}
        ],
        "artwork": {
            "original": "https://cdn.example.com/orig.jpg",
            "high": "https://cdn.example.com/high.jpg",
            "medium": "https://cdn.example.com/med.jpg",
            "thumbnail": "https://cdn.example.com/thumb.jpg",
        },
        "playback": {
            "resolved": True,
            "stream_url": "https://cdn.example.com/stream.mp4?exp=1800000000",
            "expires_at": "1800000000",
            "quality": "96kbps",
        },
    }

    track = CanonicalTrack.from_dict(raw_data)
    assert track.track_id == "test-uuid-1"
    assert track.title == "Kannum Kannum Nokia"
    assert track.artists == ["Leslie Lewis", "Vasundhara Das"]
    assert track.album == "Anniyan"
    assert track.duration_ms == 248000
    assert track.source == "gaana"
    assert track.playback.resolved is True
    assert track.artwork.high == "https://cdn.example.com/high.jpg"

    legacy = track.to_legacy_dict()
    assert legacy["id"] == "test-uuid-1"
    assert legacy["seokey"] == "test-uuid-1"
    assert legacy["artist"] == "Leslie Lewis, Vasundhara Das"
    assert legacy["imageUrl"] == "https://cdn.example.com/high.jpg"
    assert legacy["stream_url"] == "https://cdn.example.com/stream.mp4?exp=1800000000"
    assert legacy["playable"] is True


def test_canonical_track_from_legacy_format():
    legacy_data = {
        "seokey": "kannum-kannum-nokia",
        "song_name": "Kannum Kannum Nokia",
        "artist": "Leslie Lewis, Vasundhara Das",
        "album": "Anniyan",
        "duration": "248",
        "imageUrl": "http://img.example.com/cover.jpg",
        "stream_url": "https://stream.example.com/audio.mp4",
    }
    track = CanonicalTrack.from_dict(legacy_data)
    assert track.track_id == "kannum-kannum-nokia"
    assert track.title == "Kannum Kannum Nokia"
    assert "Leslie Lewis" in track.artists
    assert track.duration_ms == 248000
    assert track.playback.stream_url == "https://stream.example.com/audio.mp4"
    assert track.playback.resolved is True


def test_artwork_resolver_priority():
    resolver = ArtworkResolver()
    
    # Priority 1: Track high-resolution
    track_with_high = {
        "track_id": "t1",
        "artwork": {"high": "https://cdn.example.com/t1-high.jpg"},
        "album": {"artwork": {"high": "https://cdn.example.com/album-high.jpg"}},
        "artist_image": "https://cdn.example.com/artist.jpg",
    }
    # Direct candidate extraction
    import asyncio
    res1 = asyncio.run(resolver.resolve(track_with_high))
    assert res1["artwork_url"] == "https://cdn.example.com/t1-high.jpg"
    assert res1["source"] == "track_high"

    # Priority 2: Album when track artwork missing
    track_with_album = {
        "track_id": "t2",
        "album": {"artwork": "https://cdn.example.com/album.jpg"},
        "artist_image": "https://cdn.example.com/artist.jpg",
    }
    res2 = asyncio.run(resolver.resolve(track_with_album))
    assert res2["artwork_url"] == "https://cdn.example.com/album.jpg"
    assert res2["source"] == "album"

    # Priority 5: Artist when album missing
    track_with_artist = {
        "track_id": "t3",
        "artist_image": "https://cdn.example.com/artist.jpg",
    }
    res3 = asyncio.run(resolver.resolve(track_with_artist))
    assert res3["artwork_url"] == "https://cdn.example.com/artist.jpg"
    assert res3["source"] == "artist"


def test_artwork_normalization_rules():
    assert normalize_url("//cdn.example.com/image.jpg") == "https://cdn.example.com/image.jpg"
    assert normalize_url("http://cdn.example.com/image.jpg") == "https://cdn.example.com/image.jpg"
    assert normalize_url("https://cdn.example.com/path with spaces.jpg") == "https://cdn.example.com/path%20with%20spaces.jpg"
    assert normalize_url("https://cdn.example.com/placeholder-album.png") is None


@pytest.mark.asyncio
async def test_playback_resolver_cached_and_fallback():
    cache = MagicMock()
    cache.get = AsyncMock(return_value={
        "track_id": "cached-song",
        "playable": True,
        "stream": {
            "url": "https://cdn.example.com/song.mp4?exp=9999999999",
            "expires_at": "9999999999",
            "quality": "96kbps",
        },
        "artwork": {"url": "https://cdn.example.com/cover.jpg", "source": "album"},
    })

    resolver = PlaybackResolver(cache=cache)
    res = await resolver.resolve("cached-song")
    assert res["playable"] is True
    assert res["stream"]["url"] == "https://cdn.example.com/song.mp4?exp=9999999999"


def test_tracks_api_endpoints():
    client = TestClient(app)
    
    # Test resolve-playback endpoint (returns 200 with playable or unplayable reason, not 500)
    response = client.get("/tracks/non-existent-track-xyz/resolve-playback")
    assert response.status_code == 200
    data = response.json()
    assert "playable" in data
    assert data["playable"] is False
    assert data["reason"] is not None

    # Test artwork endpoint
    response = client.get("/tracks/sample-track/artwork")
    assert response.status_code == 200
    data = response.json()
    assert "track_id" in data
    assert "artwork_url" in data
