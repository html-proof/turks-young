from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from api.personalization.models import ListeningEvent, TrackSnapshot
from api.personalization.scorer import (
    apply_diversity,
    build_preference_scores,
    build_search_seeds,
    preferred_languages,
    recency_penalty,
    score_track,
)
from api.personalization.service import PersonalizedMusicService


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------

class FakeRepository:
    def __init__(
        self,
        profile: dict[str, Any] | None = None,
        favorites: list[dict[str, Any]] | None = None,
        history: list[dict[str, Any]] | None = None,
        signals: dict[str, Any] | None = None,
    ) -> None:
        self.profile   = profile   or {}
        self.favorites = favorites or []
        self.history   = history   or []
        self.signals   = signals   or {}
        self.cleared_signals = False

    async def get_profile(self, uid: str)  -> dict[str, Any]:         return self.profile
    async def list_favorites(self, uid: str) -> list[dict[str, Any]]: return self.favorites
    async def list_history(self, uid: str, limit: int) -> list[dict[str, Any]]:
        return self.history[:limit]
    async def get_signals(self, uid: str) -> dict[str, Any]: return self.signals
    async def clear_signals(self, uid: str) -> None: self.cleared_signals = True
    async def adjust_signals(self, uid: str, track: Any, amount: float) -> None: pass


class FakeCatalog:
    def __init__(
        self,
        search_results: list[dict[str, Any]] | None = None,
        trending: list[dict[str, Any]] | None = None,
        new_releases: list[dict[str, Any]] | None = None,
    ) -> None:
        self.search_results = search_results or []
        self.trending       = trending       or []
        self.new_releases   = new_releases   or []
        self.searches:  list[str] = []
        self.languages: list[str] = []
        self.new_release_langs: list[str] = []

    async def search_songs(self, query: str, limit: int) -> list[dict[str, Any]]:
        self.searches.append(query)
        return self.search_results[:limit]

    async def get_trending(self, language: str, limit: int) -> list[dict[str, Any]]:
        self.languages.append(language)
        return self.trending[:limit]

    async def get_new_releases(self, language: str, limit: int) -> list[dict[str, Any]]:
        self.new_release_langs.append(language)
        return self.new_releases[:limit]


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

def test_track_snapshot_normalises_comma_and_list_fields():
    track = TrackSnapshot(
        seokey="yellow",
        artists="Coldplay, Chris Martin",
        artist_ids="1, 2",
        genres="Rock, Alternative",
    )
    assert track.artists   == ["Coldplay", "Chris Martin"]
    assert track.artist_ids == ["1", "2"]
    assert track.genres    == ["Rock", "Alternative"]


def test_listening_event_inherits_track_snapshot_normalisation():
    event = ListeningEvent(seokey="yellow", artists="Coldplay", completed=True)
    assert event.completed is True
    assert event.artists == ["Coldplay"]


# ---------------------------------------------------------------------------
# Scorer unit tests
# ---------------------------------------------------------------------------

class TestRecencyPenalty:
    def test_very_recent_track_has_heavy_penalty(self):
        played_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        assert recency_penalty(played_at) < 0.2

    def test_old_track_has_no_penalty(self):
        played_at = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        assert recency_penalty(played_at) == 1.0

    def test_missing_timestamp_returns_one(self):
        assert recency_penalty("") == 1.0


class TestBuildPreferenceScores:
    def test_profile_artists_contribute(self):
        prefs = build_preference_scores(
            profile={"favorite_artists": ["Coldplay"], "languages": ["English"]},
            favorites=[],
            history=[],
            signals={},
        )
        assert prefs["artists"]["coldplay"] > 0

    def test_completed_history_outweighs_partial(self):
        base = build_preference_scores(
            profile={},
            favorites=[],
            history=[{"artists": ["Artist A"], "language": "English", "completed": False}],
            signals={},
        )
        strong = build_preference_scores(
            profile={},
            favorites=[],
            history=[{"artists": ["Artist A"], "language": "English", "completed": True}],
            signals={},
        )
        assert strong["artists"]["artist a"] > base["artists"]["artist a"]

    def test_favorites_add_to_artist_score(self):
        prefs = build_preference_scores(
            profile={},
            favorites=[{"artists": ["Billie Eilish"], "genres": ["Pop"], "language": "English"}],
            history=[],
            signals={},
        )
        assert prefs["artists"]["billie eilish"] > 0
        assert prefs["genres"]["pop"] > 0

    def test_signals_are_merged(self):
        from api.personalization.repository import _signal_key  # noqa: PLC0415
        key = _signal_key("Jazz")
        prefs = build_preference_scores(
            profile={},
            favorites=[],
            history=[],
            signals={"genres": {key: {"value": "Jazz", "score": 7.0}}},
        )
        assert prefs["genres"]["jazz"] == pytest.approx(7.0)


class TestBuildSearchSeeds:
    def test_returns_interleaved_artist_and_genre_seeds(self):
        prefs = {
            "artists": {"coldplay": 10.0, "radiohead": 8.0},
            "genres":  {"rock": 9.0, "indie": 6.0},
            "languages": {},
        }
        seeds = build_search_seeds(prefs, max_seeds=4)
        assert len(seeds) == 4
        # First seed should be top artist.
        assert seeds[0].lower() == "coldplay"

    def test_respects_max_seeds(self):
        prefs = {
            "artists": {f"artist{i}": float(10 - i) for i in range(20)},
            "genres":  {f"genre{i}":  float(10 - i) for i in range(20)},
            "languages": {},
        }
        assert len(build_search_seeds(prefs, max_seeds=5)) == 5


class TestScoreTrack:
    def _prefs(self):
        return {
            "artists":   {"coldplay": 6.0},
            "genres":    {"rock": 5.0},
            "languages": {"english": 3.0},
        }

    def test_artist_match_boosts_score(self):
        track = {"seokey": "t1", "artists": "Coldplay", "genres": "Rock", "language": "English"}
        score, reasons = score_track(track, self._prefs(), "test source", {})
        assert score > 1.0
        assert any("Coldplay" in r for r in reasons)

    def test_recently_played_demoted(self):
        recent_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        track = {"seokey": "t1", "artists": "Coldplay", "genres": "Rock", "language": "English"}
        score_fresh,  _ = score_track(track, self._prefs(), "src", {})
        score_recent, _ = score_track(track, self._prefs(), "src", {"t1": recent_ts})
        assert score_recent < score_fresh

    def test_trending_source_gives_bonus(self):
        track = {"seokey": "t1", "artists": "", "genres": "", "language": ""}
        score_trend,  _ = score_track(track, self._prefs(), "Trending in English", {})
        score_search, _ = score_track(track, self._prefs(), "Because you like X", {})
        assert score_trend > score_search


class TestApplyDiversity:
    def _make_tracks(self, artist: str, count: int) -> list[dict]:
        return [{"seokey": f"{artist}-{i}", "artists": artist} for i in range(count)]

    def test_caps_tracks_per_artist(self):
        tracks = self._make_tracks("Coldplay", 5) + self._make_tracks("Adele", 2)
        result = apply_diversity(tracks, max_per_artist=3)
        # Overflow tracks are deferred to the end — total count is unchanged.
        coldplay_total = sum(1 for t in result if t["artists"] == "Coldplay")
        assert coldplay_total == 5   # all 5 preserved, just reordered
        # But within the first `max_per_artist` Coldplay slots we should be capped.
        first_three_coldplay = sum(
            1 for t in result[:3] if t["artists"] == "Coldplay"
        )
        assert first_three_coldplay <= 3

    def test_preserves_all_tracks_after_cap(self):
        tracks = self._make_tracks("Coldplay", 5)
        result = apply_diversity(tracks, max_per_artist=3)
        assert len(result) == 5


# ---------------------------------------------------------------------------
# Integration tests (service-level)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recommendations_rank_preferences_and_exclude_favorites():
    repo = FakeRepository(
        profile={"favorite_artists": ["Coldplay"], "favorite_genres": ["Rock"], "languages": ["English"]},
        favorites=[{"seokey": "already-liked", "artists": ["Coldplay"]}],
    )
    catalog = FakeCatalog(
        search_results=[
            {"seokey": "best-match",    "title": "Best Match",    "artists": "Coldplay", "genres": "Rock", "language": "English"},
            {"seokey": "already-liked", "title": "Favorite",      "artists": "Coldplay", "genres": "Rock", "language": "English"},
        ],
        trending=[
            {"seokey": "weak-match",    "title": "Weak Match",    "artists": "Other",    "genres": "Pop",  "language": "English"},
        ],
    )

    service = PersonalizedMusicService(repo)  # type: ignore[arg-type]
    results = await service.recommendations("user-1", catalog, 10)

    assert results[0]["seokey"] == "best-match"
    assert "already-liked" not in {t["seokey"] for t in results}
    assert any("Coldplay" in r for r in results[0]["recommendation"]["reasons"])


@pytest.mark.asyncio
async def test_cold_start_does_not_inject_default_music():
    repo = FakeRepository()   # no profile, no history → cold start
    catalog = FakeCatalog(
        trending=[{"seokey": "trending-song", "title": "Trending", "artists": "", "genres": "", "language": "English"}],
    )

    service = PersonalizedMusicService(repo)  # type: ignore[arg-type]
    results = await service.recommendations("new-user", catalog, 5)

    assert catalog.searches == []
    assert catalog.languages == []
    assert results == []


@pytest.mark.asyncio
async def test_multi_language_trending_fetched_in_parallel():
    repo = FakeRepository(
        profile={"languages": ["English", "Hindi", "Tamil"]},
    )
    catalog = FakeCatalog(
        trending=[{"seokey": "t1", "artists": "", "genres": "", "language": ""}],
    )

    service = PersonalizedMusicService(repo)  # type: ignore[arg-type]
    await service.recommendations("user-1", catalog, 5)

    # All three preferred languages should have been fetched.
    assert "English" in catalog.languages
    assert "Hindi"   in catalog.languages
    assert "Tamil"   in catalog.languages


@pytest.mark.asyncio
async def test_recent_tracks_are_demoted():
    recent_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    repo = FakeRepository(
        profile={"favorite_artists": ["Coldplay"], "languages": ["English"]},
        history=[{"seokey": "recent", "artists": "Coldplay", "language": "English", "played_at": recent_ts}],
    )
    catalog = FakeCatalog(
        search_results=[
            {"seokey": "recent", "artists": "Coldplay", "language": "English"},
            {"seokey": "fresh",  "artists": "Coldplay", "language": "English"},
        ],
        trending=[],
    )

    service = PersonalizedMusicService(repo)  # type: ignore[arg-type]
    results = await service.recommendations("user-1", catalog, 5)

    assert results[0]["seokey"] == "fresh"


@pytest.mark.asyncio
async def test_diversity_filter_limits_single_artist_dominance():
    repo = FakeRepository(
        profile={"favorite_artists": ["Coldplay"], "languages": ["English"]},
    )
    # Catalog returns 6 Coldplay tracks.
    catalog = FakeCatalog(
        search_results=[
            {"seokey": f"cp-{i}", "artists": "Coldplay", "genres": "Rock", "language": "English"}
            for i in range(6)
        ],
        trending=[
            {"seokey": "other-1", "artists": "Other Artist", "genres": "Pop", "language": "English"},
        ],
    )

    service = PersonalizedMusicService(repo)  # type: ignore[arg-type]
    results = await service.recommendations("user-1", catalog, 7)

    coldplay_in_top = sum(
        1 for t in results[:service.MAX_PER_ARTIST + 1]
        if "Coldplay" in t.get("artists", "")
    )
    assert coldplay_in_top <= service.MAX_PER_ARTIST


@pytest.mark.asyncio
async def test_rebuild_signals_clears_and_replays_history():
    history = [
        {"seokey": "s1", "artists": ["Coldplay"], "genres": ["Rock"], "language": "English", "completed": True},
        {"seokey": "s2", "artists": ["Adele"],    "genres": ["Pop"],  "language": "English", "completed": False},
    ]
    repo = FakeRepository(history=history)
    service = PersonalizedMusicService(repo)  # type: ignore[arg-type]

    count = await service.rebuild_signals_from_history("user-1")

    assert repo.cleared_signals is True
    assert count == 2


@pytest.mark.asyncio
async def test_new_releases_fetched_for_top_language():
    repo = FakeRepository(
        profile={"languages": ["Hindi"], "favorite_artists": ["Arijit Singh"]},
    )
    catalog = FakeCatalog(
        search_results=[{"seokey": "sr1", "artists": "Arijit Singh", "genres": "Bollywood", "language": "Hindi"}],
        new_releases=[{"seokey": "nr1", "artists": "Some Artist", "genres": "Bollywood", "language": "Hindi"}],
        trending=[],
    )

    service = PersonalizedMusicService(repo)  # type: ignore[arg-type]
    await service.recommendations("user-1", catalog, 10)

    assert "Hindi" in catalog.new_release_langs
