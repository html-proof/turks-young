from unittest.mock import AsyncMock

import pytest

from api.lyrics.lrc import parse_lrc
from api.lyrics.provider import _candidate_score, normalize_track_name
from api.lyrics.service import LyricsService, duration_seconds


class FakeRepository:
    def __init__(self, cached=None):
        self.cached = cached
        self.put = AsyncMock()

    async def get(self, track_id, max_age_seconds, status=None):
        if not self.cached:
            return None
        if status is not None and self.cached.get("status") != status:
            return None
        return self.cached


def test_parse_lrc_supports_fractions_multiple_timestamps_and_duration():
    result = parse_lrc(
        "[ar:Artist]\n[00:01.2][00:03.250] Hello \n[00:05]World",
        duration_ms=8_000,
    )

    assert result == [
        {"startMs": 1200, "endMs": 3250, "text": "Hello"},
        {"startMs": 3250, "endMs": 5000, "text": "Hello"},
        {"startMs": 5000, "endMs": 8000, "text": "World"},
    ]


def test_track_name_cleanup_preserves_version_markers():
    assert normalize_track_name("Song (Official Audio)") == "Song"
    assert normalize_track_name("Song (Live)") == "Song (Live)"
    assert normalize_track_name("Song [Remix]") == "Song [Remix]"


def test_candidate_score_rejects_wrong_version_duration():
    track = {
        "title": "Song",
        "artists": "Artist",
        "album": "Album",
        "duration_seconds": 200,
    }
    exact = {"trackName": "Song", "artistName": "Artist", "albumName": "Album", "duration": 201}
    wrong = {"trackName": "Song Remix", "artistName": "Artist", "albumName": "Album", "duration": 240}

    assert _candidate_score(exact, track) == 100
    assert _candidate_score(wrong, track) < 80


@pytest.mark.parametrize("value, expected", [(200, 200), ("200.4", 200), ("03:20", 200), ("", 0)])
def test_duration_seconds(value, expected):
    assert duration_seconds(value) == expected


@pytest.mark.asyncio
async def test_service_fetches_and_normalizes_synced_lyrics():
    provider = AsyncMock()
    provider.get_lyrics.return_value = {
        "id": 42,
        "instrumental": False,
        "plainLyrics": "Hello\nWorld",
        "syncedLyrics": "[00:01.00]Hello\n[00:02.50]World",
    }
    repository = FakeRepository()
    service = LyricsService(provider, repository, success_ttl=100, not_found_ttl=10)

    result = await service.get_lyrics({
        "seokey": "hello-world",
        "title": "Hello World",
        "artists": "Artist",
        "album": "Album",
        "duration": "00:05",
    })

    assert result["status"] == "available"
    assert result["providerId"] == "42"
    assert result["synced"] is True
    assert result["lines"][-1]["endMs"] == 5000
    repository.put.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_distinguishes_instrumental_and_not_found():
    provider = AsyncMock()
    repository = FakeRepository()
    service = LyricsService(provider, repository, success_ttl=100, not_found_ttl=10)
    track = {"seokey": "track", "title": "Track", "artists": "Artist", "duration": 100}

    provider.get_lyrics.return_value = {"id": 1, "instrumental": True}
    instrumental = await service.get_lyrics(track)
    assert instrumental["status"] == "instrumental"
    assert instrumental["instrumental"] is True

    provider.get_lyrics.return_value = None
    missing = await service.get_lyrics(track)
    assert missing["status"] == "not_found"
    assert missing["provider"] is None


@pytest.mark.asyncio
async def test_service_uses_cached_lyrics_without_provider_call():
    provider = AsyncMock()
    repository = FakeRepository({
        "provider": "lrclib",
        "provider_lyrics_id": "7",
        "status": "available",
        "instrumental": False,
        "synced_lyrics": "[00:01]Cached",
        "plain_lyrics": "Cached",
    })
    service = LyricsService(provider, repository, success_ttl=100, not_found_ttl=10)

    result = await service.get_lyrics({
        "seokey": "cached", "title": "Cached", "artists": "Artist", "duration": 5
    })

    assert result["lines"][0]["text"] == "Cached"
    provider.get_lyrics.assert_not_awaited()
