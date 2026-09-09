from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from api.catalog.labels import (
    LabelRegistry,
    LabelRecord,
    PostgresLabelRepository,
    calculate_label_confidence,
    canonicalize_label,
    is_verified_label,
    normalize_label,
)
from api.catalog.normalize import compute_metadata_confidence, song
from api.catalog.search.ranking import rank, score_item


def test_normalize_label():
    assert normalize_label("Sony Music & Entertainment") == "sony music and entertainment"
    assert normalize_label("Anirudh Official YouTube Channel") == "anirudh"
    assert normalize_label("Saina Official Channel") == "saina"
    assert normalize_label("  Tips   Industries  ") == "tips industries"
    assert normalize_label("Muzik247!") == "muzik247"
    assert normalize_label("") == ""


def test_canonicalize_label_and_aliases():
    assert canonicalize_label("Sony Music India") == "sony music"
    assert canonicalize_label("Sony Music Entertainment") == "sony music"
    assert canonicalize_label("Saregama India") == "saregama"
    assert canonicalize_label("Zee Music Company") == "zee music"
    assert canonicalize_label("Think Music India") == "think music"
    assert canonicalize_label("Muzik 247") == "muzik247"
    assert canonicalize_label("Music 247") == "muzik247"
    assert canonicalize_label("The Orchard") == "orchard"
    assert canonicalize_label("Satyam Audio") == "satyam audios"
    assert canonicalize_label("Goodwill Entertainment") == "goodwill entertainments"
    assert canonicalize_label("123 Musix") == "123musix"
    assert canonicalize_label("A P International") == "ap international"


def test_calculate_label_confidence():
    # Only in label field
    assert calculate_label_confidence(appears_in_label_field=True) == 40

    # Only in copyright
    assert calculate_label_confidence(appears_in_copyright=True) == 20

    # Label + copyright
    assert calculate_label_confidence(
        appears_in_label_field=True,
        appears_in_copyright=True,
    ) == 60

    # Label + copyright + multiple songs
    assert calculate_label_confidence(
        appears_in_label_field=True,
        appears_in_copyright=True,
        appears_across_multiple_songs=True,
    ) == 75

    # Full multi-source
    assert calculate_label_confidence(
        appears_in_label_field=True,
        appears_in_copyright=True,
        appears_across_multiple_songs=True,
        appears_across_multiple_albums=True,
        provider_marks_official=True,
    ) == 100


def test_garbage_label_rejection():
    registry = LabelRegistry()
    assert registry.register_candidate("ab") is None  # too short (<3)
    assert registry.register_candidate("a") is None
    assert registry.register_candidate("Unknown") is None
    assert registry.register_candidate("Various Artists") is None
    assert registry.register_candidate("Music") is None
    assert registry.register_candidate("Audio") is None
    assert registry.register_candidate("Official") is None
    assert registry.register_candidate("Songs") is None
    assert registry.register_candidate("Soundtrack") is None


def test_seed_labels_start_verified():
    assert is_verified_label("Sony Music") is True
    assert is_verified_label("T-Series") is True
    assert is_verified_label("Saregama") is True
    assert is_verified_label("Think Music") is True
    assert is_verified_label("Muzik247") is True
    assert is_verified_label("Sun Pictures") is True
    assert is_verified_label("Lahari Music") is True
    assert is_verified_label("Hombale Films") is True
    assert is_verified_label("Totally Random Unknown Entity 123") is False


def test_dynamic_discovery_candidate_to_verified_promotion():
    registry = LabelRegistry()
    test_label = "Neo Wave Sounds"

    # Initially unknown
    assert registry.is_verified_label(test_label) is False

    # 1. First observation on single song
    registry.observe_song({
        "id": "song_1",
        "title": "Track 1",
        "label": test_label,
        "album": {"id": "alb_1", "title": "Album 1"},
    })

    # Confidence is 40 (appears_in_label_field only) -> candidate, not yet verified
    assert registry.is_verified_label(test_label) is False
    canonical = canonicalize_label(test_label)
    assert registry._records[canonical].confidence == 40
    assert registry._records[canonical].verified is False

    # 2. Second observation on another song (same album)
    registry.observe_song({
        "id": "song_2",
        "title": "Track 2",
        "label": test_label,
        "album": {"id": "alb_1", "title": "Album 1"},
    })
    # Confidence becomes 40 + 15 (multiple songs) = 55 -> still candidate
    assert registry._records[canonical].confidence == 55
    assert registry.is_verified_label(test_label) is False

    # 3. Third observation on a song in a DIFFERENT album
    registry.observe_song({
        "id": "song_3",
        "title": "Track 3",
        "label": test_label,
        "album": {"id": "alb_2", "title": "Album 2"},
    })
    # Confidence becomes 40 + 15 + 15 = 70 -> Promoted to Verified!
    assert registry._records[canonical].confidence >= 70
    assert registry._records[canonical].verified is True
    assert registry.is_verified_label(test_label) is True


def test_compute_metadata_confidence():
    # Fully complete metadata
    conf = compute_metadata_confidence(
        title="Sample Song",
        artists=[{"id": "a1", "name": "Artist"}],
        album="Sample Album",
        duration_ms=210000,
        artwork_url="https://images.test/art.jpg",
        stream_url="https://audio.test/stream.mp3",
    )
    assert conf == 1.0

    # Incomplete metadata (missing album, stream, artwork)
    conf_sparse = compute_metadata_confidence(
        title="Sample Song",
        artists=[],
        album=None,
        duration_ms=10000,
        artwork_url=None,
        stream_url=None,
    )
    assert conf_sparse < 0.50


def test_search_ranking_verified_label_bonus_and_subordination():
    # Both songs match "Manam", but song A has a verified label while song B has no verified label
    song_verified = {
        "id": "verified-track",
        "title": "Manam Theme",
        "album": "Manam",
        "artists": "Anup Rubens",
        "label": "Aditya Music",  # known verified
        "official_label_verified": True,
        "metadata_confidence": 1.0,
    }
    song_unverified = {
        "id": "unverified-track",
        "title": "Manam Theme",
        "album": "Manam",
        "artists": "Anup Rubens",
        "label": "Some Random Channel",
        "official_label_verified": False,
        "metadata_confidence": 0.5,
    }

    tier_v, score_v, reasons_v = score_item("Manam Theme", song_verified, "song")
    tier_u, score_u, reasons_u = score_item("Manam Theme", song_unverified, "song")

    # Both are exact matches (Tier A)
    assert tier_v == tier_u
    # Verified track earns verified_official_label (+25) and metadata_confidence (+15)
    assert score_v > score_u
    assert any("verified_official_label +25" in r for r in reasons_v)
    assert any("metadata_confidence +15" in r for r in reasons_v)

    # CRITICAL: Subordination test
    # An unrelated song with verified official label must NEVER beat an exact query match
    exact_song_no_label = {
        "id": "exact-match",
        "title": "Katchi Sera",
        "artists": "Sai Abhyankkar",
        "label": "Indie Self Release",
        "official_label_verified": False,
    }
    unrelated_song_official_label = {
        "id": "unrelated-official",
        "title": "Random Viral Song",
        "artists": "Big Star",
        "label": "Sony Music",
        "official_label_verified": True,
        "metadata_confidence": 1.0,
        "popularity": 1.0,
    }

    ranked = rank("Katchi Sera", [unrelated_song_official_label, exact_song_no_label], "song", 10)
    assert ranked[0]["id"] == "exact-match"


@pytest.mark.asyncio
async def test_postgres_label_repository_mock():
    from unittest.mock import MagicMock
    mock_pool = MagicMock()
    mock_conn = AsyncMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mock_pool.acquire.return_value.__aexit__.return_value = None

    mock_conn.fetch.return_value = [
        {
            "canonical_name": "test label",
            "normalized_name": "test label",
            "aliases": ["test label alias"],
            "source": "dynamic",
            "verified": True,
            "confidence": 85,
            "song_count": 5,
            "album_count": 2,
            "first_seen_at": "2026-09-09T00:00:00Z",
            "last_seen_at": "2026-09-09T00:00:00Z",
        }
    ]

    repo = PostgresLabelRepository(mock_pool)
    records = await repo.load_all()
    assert len(records) == 1
    assert records[0].canonical_name == "test label"
    assert records[0].verified is True

    # Test batch upsert
    await repo.batch_upsert(records)
    mock_conn.execute.assert_awaited()
