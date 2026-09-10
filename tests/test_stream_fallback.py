import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from api.core.circuit_breaker import CircuitBreaker, CBState, CircuitOpenError
from api.stream_fallback import (
    StreamFallbackResolver,
    decrypt_saavn_media_url,
    get_stream_fallback_resolver,
    set_stream_fallback_resolver,
)
from api.songs.songs import Songs
from api.functions import Functions
from api.errors import Errors


def test_decrypt_url_des():
    resolver = StreamFallbackResolver(cache=None)
    # Encrypt a test URL using DES-ECB with key "38343638"
    from Crypto.Cipher import DES
    raw_url = "https://aac.saavncdn.com/123/sample_96.mp4"
    pad_len = 8 - (len(raw_url.encode("utf-8")) % 8)
    padded = raw_url.encode("utf-8") + bytes([pad_len] * pad_len)
    cipher = DES.new(b"38343638", DES.MODE_ECB)
    import base64
    encrypted_b64 = base64.b64encode(cipher.encrypt(padded)).decode("utf-8")

    decrypted = resolver.decrypt_url(encrypted_b64)
    assert decrypted == raw_url
    assert decrypt_saavn_media_url(encrypted_b64) == raw_url


def test_build_fallback_stream_urls():
    resolver = StreamFallbackResolver(cache=None)
    url_96 = "https://aac.saavncdn.com/999/test_96.mp4"
    streams = resolver.build_fallback_stream_urls(decrypted=url_96)
    
    assert streams["high_quality"] == "https://aac.saavncdn.com/999/test_320.mp4"
    assert streams["medium_quality"] == "https://aac.saavncdn.com/999/test_160.mp4"
    assert streams["low_quality"] == "https://aac.saavncdn.com/999/test_96.mp4"
    assert streams["default"] == "https://aac.saavncdn.com/999/test_320.mp4"
    assert streams["raw"] == url_96


@pytest.mark.asyncio
async def test_resolve_stream_mocked():
    resolver = StreamFallbackResolver(cache=None)
    
    # Mock aiohttp session response
    mock_payload = {
        "results": [
            {
                "title": "Wake Up",
                "primary_artists": "Ramqa Fifa",
                "media_preview_url": "https://preview.saavncdn.com/sample_preview.mp4",
            }
        ]
    }
    
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=mock_payload)
    mock_resp.__aenter__.return_value = mock_resp
    mock_resp.__aexit__.return_value = None

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp
    
    async def mock_get_session():
        return mock_session
    
    with patch.object(resolver, "_get_session", side_effect=mock_get_session):
        result = await resolver.resolve_stream("Wake Up", "Ramqa Fifa")
        assert result is not None
        assert "stream_url" in result
        assert result["stream_url"] == "https://preview.saavncdn.com/sample_preview.mp4"
        assert result["stream_urls"]["urls"]["default"] == "https://preview.saavncdn.com/sample_preview.mp4"


@pytest.mark.asyncio
async def test_circuit_breaker_does_not_reset_opened_at_on_trailing_failures():
    cb = CircuitBreaker("test_cb", failure_threshold=2, recovery_timeout=5.0, window=60.0)
    
    # Trigger 2 failures to open circuit
    await cb._on_failure()
    await cb._on_failure()
    assert cb.state == CBState.OPEN
    opened_at_first = cb._opened_at
    assert opened_at_first > 0
    
    # Trailing concurrent failures finish after circuit is OPEN
    time.sleep(0.05)
    await cb._on_failure()
    await cb._on_failure()
    
    # Opened_at MUST NOT be pushed forward
    assert cb._opened_at == opened_at_first
    # Failure count must not be incremented past threshold while already OPEN
    assert len(cb._failure_times) == 2


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_single_probe():
    cb = CircuitBreaker("test_cb", failure_threshold=1, recovery_timeout=0.1, window=60.0)
    await cb._on_failure()
    assert cb.state == CBState.OPEN
    
    # Wait for recovery timeout to elapse
    await asyncio.sleep(0.15)
    
    # First call enters HALF_OPEN and acquires probe guard
    called = []
    async def sample_coro():
        called.append(1)
        return "ok"

    # In call(): when elapsed, transitions to HALF_OPEN and allows single probe
    res = await cb.call(sample_coro())
    assert res == "ok"
    assert cb.state == CBState.CLOSED
    assert cb._half_open_probing is False


@pytest.mark.asyncio
async def test_songs_format_json_songs_fallback_integration():
    songs_module = Songs()
    songs_module.functions = Functions()
    songs_module.errors = Errors()
    
    # Track with missing stream URLs
    raw_track = {
        "track_id": "12345",
        "title": "Wake Up",
        "artist": "Ramqa Fifa",
        "seokey": "wake-up-ramqa-fifa",
        "albumseokey": "wake-up-album",
        "duration": "180",
        "has_lyrics": "0",
    }
    
    fake_fallback_result = {
        "stream_url": "https://aac.saavncdn.com/fallback_320.mp4",
        "stream_urls": {
            "urls": {
                "very_high_quality": "https://aac.saavncdn.com/fallback_320.mp4",
                "high_quality": "https://aac.saavncdn.com/fallback_320.mp4",
                "medium_quality": "https://aac.saavncdn.com/fallback_160.mp4",
                "low_quality": "https://aac.saavncdn.com/fallback_96.mp4",
                "default": "https://aac.saavncdn.com/fallback_320.mp4",
                "raw": "https://aac.saavncdn.com/fallback_96.mp4",
            }
        },
        "fallback_source": "jiosaavn",
    }
    
    mock_resolver = AsyncMock()
    mock_resolver.resolve_stream.return_value = fake_fallback_result
    
    songs_module._fallback_resolver = mock_resolver
    track = await songs_module.format_json_songs(raw_track)
    assert track["stream_url"] == "https://aac.saavncdn.com/fallback_320.mp4"
    assert track["stream_urls"]["urls"]["default"] == "https://aac.saavncdn.com/fallback_320.mp4"


@pytest.mark.asyncio
async def test_search_tracks_mocked():
    resolver = StreamFallbackResolver(cache=None)
    mock_payload = {
        "results": [
            {
                "id": "saavn123",
                "song": "Waka Waka (This Time for Africa)",
                "primary_artists": "Shakira",
                "album": "Mundial 2010",
                "duration": "203",
                "image": "https://c.saavncdn.com/798/waka-150x150.webp",
                "media_preview_url": "https://preview.saavncdn.com/waka_preview.mp4",
            }
        ]
    }

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=mock_payload)
    mock_resp.__aenter__.return_value = mock_resp
    mock_resp.__aexit__.return_value = None

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    async def mock_get_session():
        return mock_session

    with patch.object(resolver, "_get_session", side_effect=mock_get_session):
        tracks = await resolver.search_tracks("waka waka fifa", limit=5)
        assert len(tracks) == 1
        t = tracks[0]
        assert t["title"] == "Waka Waka (This Time for Africa)"
        assert t["artist"] == "Shakira"
        assert t["stream_url"] == "https://preview.saavncdn.com/waka_preview.mp4"
        assert t["images"]["urls"]["large_artwork"] == "https://c.saavncdn.com/798/waka-500x500.webp"


@pytest.mark.asyncio
async def test_songs_search_songs_fallback_integration():
    songs_module = Songs()
    songs_module.api_endpoints = MagicMock()
    songs_module.errors = Errors()
    songs_module._safe_request = AsyncMock(return_value={"error": "circuit open"})
    songs_module._circuit_breaker = MagicMock()
    songs_module._circuit_breaker.state = CBState.OPEN

    fallback_tracks = [
        {
            "id": "saavn123",
            "title": "Waka Waka",
            "artist": "Shakira",
            "stream_url": "https://preview.saavncdn.com/waka.mp4",
        }
    ]

    mock_resolver = AsyncMock()
    mock_resolver.search_tracks.return_value = fallback_tracks
    songs_module._fallback_resolver = mock_resolver

    results = await songs_module.search_songs("waka waka fifa songs song", limit=10)
    assert results == fallback_tracks

