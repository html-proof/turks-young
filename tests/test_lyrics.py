from unittest.mock import AsyncMock

import pytest

from api.lyrics.fingerprint import (
    create_track_fingerprint,
    extract_version_type,
    normalize_artist_name,
)
from api.lyrics.lrc import parse_lrc
from api.lyrics.provider import (
    _candidate_score,
    clean_song_title,
    get_primary_artist,
    normalize_track_name,
)
from api.lyrics.service import LyricsService, duration_seconds
from api.lyrics.verifier import (
    MIN_CONFIDENCE_THRESHOLD,
    LyricsVerifier,
    validate_synced_timestamps,
)


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


def test_clean_song_title_and_primary_artist():
    assert clean_song_title('Tum Hi Ho (From "Aashiqui 2")') == "Tum Hi Ho"
    assert clean_song_title("Kesariya (From Brahmastra)") == "Kesariya"
    assert clean_song_title("Arabic Kuthu - Halamithi Habibo (From \"Beast\")") == "Arabic Kuthu - Halamithi Habibo"
    assert clean_song_title("Ente Khalbile - Reprise") == "Ente Khalbile"
    assert clean_song_title("Jimikki Kammal [Malayalam]") == "Jimikki Kammal"
    assert get_primary_artist("Arijit Singh, Mithoon, Shreya Ghoshal") == "Arijit Singh"
    assert get_primary_artist([{"name": "Anirudh Ravichander"}, {"name": "Jonita Gandhi"}]) == "Anirudh Ravichander"


def test_artist_normalization_handles_punctuation_and_initials():
    assert normalize_artist_name("A.R. Rahman") == normalize_artist_name("A R Rahman")
    assert normalize_artist_name("A. R. Rahman") == normalize_artist_name("A.R. Rahman")
    assert normalize_artist_name("Ed Sheeran") == "ed sheeran"


def test_version_extraction():
    assert extract_version_type("Song (Live at Wembley)") == "live"
    assert extract_version_type("Song - DJ Remix") == "remix"
    assert extract_version_type("Song (Acoustic Version)") == "acoustic"
    assert extract_version_type("Song (Karaoke Track)") == "karaoke"
    assert extract_version_type("Song (Cover by Artist)") == "cover"
    assert extract_version_type("Song - Reprise") == "reprise"
    assert extract_version_type("Song (Sped Up)") == "sped_up"
    assert extract_version_type("Original Studio Master") == "original"


def test_candidate_score_rejects_wrong_artist():
    # Playing: Perfect - Ed Sheeran
    target = create_track_fingerprint({
        "title": "Perfect",
        "artists": "Ed Sheeran",
        "album": "Divide",
        "duration_seconds": 263,
    })
    # Candidate: Perfect - One Direction
    candidate = {
        "trackName": "Perfect",
        "artistName": "One Direction",
        "albumName": "Made in the A.M.",
        "duration": 230,
    }
    is_verified, score, _ = LyricsVerifier.verify_candidate(candidate, target)
    assert not is_verified
    assert score < 0  # Severe penalty for wrong artist


def test_candidate_score_rejects_version_mismatch():
    # Playing studio track
    target = create_track_fingerprint({
        "title": "Song Title",
        "artists": "Popular Artist",
        "album": "Studio Album",
        "duration_seconds": 240,
    })
    # Candidate is remix
    remix_cand = {
        "trackName": "Song Title (Remix)",
        "artistName": "Popular Artist",
        "albumName": "Studio Album",
        "duration": 240,
    }
    is_verified, score, _ = LyricsVerifier.verify_candidate(remix_cand, target)
    assert not is_verified
    assert score < MIN_CONFIDENCE_THRESHOLD

    # Candidate is live
    live_cand = {
        "trackName": "Song Title (Live)",
        "artistName": "Popular Artist",
        "albumName": "Live Concert",
        "duration": 240,
    }
    is_verified, score, _ = LyricsVerifier.verify_candidate(live_cand, target)
    assert not is_verified
    assert score < MIN_CONFIDENCE_THRESHOLD

    # Candidate is cover
    cover_cand = {
        "trackName": "Song Title (Cover)",
        "artistName": "Popular Artist",
        "albumName": "Covers",
        "duration": 240,
    }
    is_verified, score, _ = LyricsVerifier.verify_candidate(cover_cand, target)
    assert not is_verified
    assert score < MIN_CONFIDENCE_THRESHOLD


def test_candidate_score_accepts_exact_metadata_match():
    target = create_track_fingerprint({
        "title": "Tum Hi Ho",
        "artists": "Arijit Singh",
        "album": "Aashiqui 2",
        "duration_seconds": 262,
    })
    candidate = {
        "trackName": "Tum Hi Ho",
        "artistName": "Arijit Singh",
        "albumName": "Aashiqui 2",
        "duration": 261,
    }
    is_verified, score, _ = LyricsVerifier.verify_candidate(candidate, target)
    assert is_verified
    assert score >= MIN_CONFIDENCE_THRESHOLD


def test_candidate_score_rejects_wrong_movie_album():
    # Indian song where same title exists for different movies
    target = create_track_fingerprint({
        "title": "Aradhya",
        "artists": "Sid Sriram",
        "album": "Kushi",
        "duration_seconds": 280,
    })
    candidate = {
        "trackName": "Aradhya",
        "artistName": "Sid Sriram",
        "albumName": "Different Movie Soundtrack",
        "duration": 280,
    }
    # Album mismatch reduces confidence
    _, score, _ = LyricsVerifier.verify_candidate(candidate, target)
    # Exact match with matching album
    exact_cand = {
        "trackName": "Aradhya",
        "artistName": "Sid Sriram",
        "albumName": "Kushi",
        "duration": 280,
    }
    _, exact_score, _ = LyricsVerifier.verify_candidate(exact_cand, target)
    assert exact_score > score


def test_timestamp_validation_rejects_out_of_bounds():
    track_duration = 180  # 3 minutes

    # Valid lines within track
    valid_lines = [
        {"startMs": 5000, "text": "Line 1"},
        {"startMs": 60000, "text": "Line 2"},
        {"startMs": 175000, "text": "Line 3"},
    ]
    assert validate_synced_timestamps(valid_lines, track_duration) is True

    # Invalid: line extends to 430 seconds (over 7 minutes) on a 3-minute song
    out_of_bounds = [
        {"startMs": 5000, "text": "Line 1"},
        {"startMs": 60000, "text": "Line 2"},
        {"startMs": 430000, "text": "Line from another extended version"},
    ]
    assert validate_synced_timestamps(out_of_bounds, track_duration) is False

    # Invalid: non-monotonic timestamps
    non_monotonic = [
        {"startMs": 10000, "text": "Line 1"},
        {"startMs": 5000, "text": "Line 2"},
    ]
    assert validate_synced_timestamps(non_monotonic, track_duration) is False

    # Invalid: negative timestamp
    negative = [{"startMs": -500, "text": "Line 1"}]
    assert validate_synced_timestamps(negative, track_duration) is False


@pytest.mark.parametrize("value, expected", [(200, 200), ("200.4", 200), ("03:20", 200), ("", 0)])
def test_duration_seconds(value, expected):
    assert duration_seconds(value) == expected


@pytest.mark.asyncio
async def test_service_fetches_and_normalizes_synced_lyrics():
    provider = AsyncMock()
    provider.get_lyrics.return_value = {
        "id": 42,
        "trackName": "Hello World",
        "artistName": "Artist",
        "albumName": "Album",
        "duration": 5,
        "instrumental": False,
        "plainLyrics": "Hello\nWorld",
        "syncedLyrics": "[00:01.00]Hello\n[00:02.50]World",
        "_verified": True,
        "_verification_score": 1450,
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
    assert result["verified"] is True
    assert result["verificationScore"] == 1450
    assert result["lines"][-1]["endMs"] == 5000
    repository.put.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_rejects_unverified_lyrics_candidate():
    provider = AsyncMock()
    # Provider returns completely mismatched candidate (e.g. wrong song/artist)
    provider.get_lyrics.return_value = {
        "id": 99,
        "trackName": "Different Song",
        "artistName": "Unrelated Singer",
        "plainLyrics": "Wrong lyrics text",
    }
    repository = FakeRepository()
    service = LyricsService(provider, repository, success_ttl=100, not_found_ttl=10)

    result = await service.get_lyrics({
        "seokey": "target-song",
        "title": "Target Song",
        "artists": "Real Singer",
        "duration": 200,
    })

    # Must be marked not_found and unverified
    assert result["status"] == "not_found"
    assert result["verified"] is False
    assert result["lines"] == []
    assert result["plainLyrics"] is None


@pytest.mark.asyncio
async def test_service_distinguishes_instrumental_and_not_found():
    provider = AsyncMock()
    repository = FakeRepository()
    service = LyricsService(provider, repository, success_ttl=100, not_found_ttl=10)
    track = {"seokey": "track", "title": "Track", "artists": "Artist", "duration": 100}

    provider.get_lyrics.return_value = {
        "id": 1,
        "trackName": "Track",
        "artistName": "Artist",
        "instrumental": True,
        "_verified": True,
        "_verification_score": 1000,
    }
    instrumental = await service.get_lyrics(track)
    assert instrumental["status"] == "instrumental"
    assert instrumental["instrumental"] is True
    assert instrumental["verified"] is True

    provider.get_lyrics.return_value = None
    missing = await service.get_lyrics(track)
    assert missing["status"] == "not_found"
    assert missing["provider"] is None
    assert missing["verified"] is False


@pytest.mark.asyncio
async def test_service_uses_verified_cached_lyrics_without_provider_call():
    provider = AsyncMock()
    repository = FakeRepository({
        "provider": "lrclib",
        "provider_lyrics_id": "7",
        "title": "Cached",
        "artist": "Artist",
        "status": "available",
        "instrumental": False,
        "synced_lyrics": "[00:01]Cached",
        "plain_lyrics": "Cached",
        "verified": True,
        "verification_score": 1000,
    })
    service = LyricsService(provider, repository, success_ttl=100, not_found_ttl=10)

    result = await service.get_lyrics({
        "seokey": "cached", "title": "Cached", "artists": "Artist", "duration": 5
    })

    assert result["lines"][0]["text"] == "Cached"
    assert result["verified"] is True
    provider.get_lyrics.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_invalidates_stale_mismatched_cached_lyrics():
    provider = AsyncMock()
    # Repository has cached data for "Old Wrong Song" mapped to this ID
    repository = FakeRepository({
        "provider": "lrclib",
        "provider_lyrics_id": "7",
        "title": "Old Wrong Song",
        "artist": "Old Wrong Artist",
        "status": "available",
        "instrumental": False,
        "synced_lyrics": "[00:01]Cached",
        "plain_lyrics": "Cached",
        "verified": False,
        "verification_score": 0,
    })
    provider.get_lyrics.return_value = {
        "id": 88,
        "trackName": "Target Song",
        "artistName": "Target Artist",
        "plainLyrics": "Correct verified text",
        "_verified": True,
        "_verification_score": 1000,
    }
    service = LyricsService(provider, repository, success_ttl=100, not_found_ttl=10)

    result = await service.get_lyrics({
        "seokey": "target", "title": "Target Song", "artists": "Target Artist", "duration": 5
    })

    # The stale unverified entry was bypassed, provider was called!
    assert result["plainLyrics"] == "Correct verified text"
    assert result["verified"] is True
    provider.get_lyrics.assert_awaited_once()
