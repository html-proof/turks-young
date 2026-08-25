from typing import Any

import pytest

from api.personalization.models import ListeningEvent, TrackSnapshot
from api.personalization.service import PersonalizedMusicService


class FakeRepository:
    def __init__(
        self,
        profile: dict[str, Any] | None = None,
        favorites: list[dict[str, Any]] | None = None,
        history: list[dict[str, Any]] | None = None,
        signals: dict[str, Any] | None = None,
    ) -> None:
        self.profile = profile or {}
        self.favorites = favorites or []
        self.history = history or []
        self.signals = signals or {}

    async def get_profile(self, uid: str) -> dict[str, Any]:
        return self.profile

    async def list_favorites(self, uid: str) -> list[dict[str, Any]]:
        return self.favorites

    async def list_history(self, uid: str, limit: int) -> list[dict[str, Any]]:
        return self.history[:limit]

    async def get_signals(self, uid: str) -> dict[str, Any]:
        return self.signals


class FakeCatalog:
    def __init__(self, search_results: list[dict[str, Any]], trending: list[dict[str, Any]]) -> None:
        self.search_results = search_results
        self.trending = trending
        self.searches: list[str] = []
        self.languages: list[str] = []

    async def search_songs(self, query: str, limit: int) -> list[dict[str, Any]]:
        self.searches.append(query)
        return self.search_results[:limit]

    async def get_trending(self, language: str, limit: int) -> list[dict[str, Any]]:
        self.languages.append(language)
        return self.trending[:limit]


def test_track_models_normalize_existing_song_response_fields():
    track = TrackSnapshot(
        seokey="yellow",
        artists="Coldplay, Chris Martin",
        artist_ids="1, 2",
        genres="Rock, Alternative",
    )
    event = ListeningEvent(seokey="yellow", artists="Coldplay", completed=True)

    assert track.artists == ["Coldplay", "Chris Martin"]
    assert track.artist_ids == ["1", "2"]
    assert track.genres == ["Rock", "Alternative"]
    assert event.completed is True


@pytest.mark.asyncio
async def test_recommendations_rank_preferences_and_exclude_favorites():
    repository = FakeRepository(
        profile={
            "favorite_artists": ["Coldplay"],
            "favorite_genres": ["Rock"],
            "languages": ["English"],
        },
        favorites=[{"seokey": "already-liked", "artists": ["Coldplay"]}],
    )
    catalog = FakeCatalog(
        search_results=[
            {
                "seokey": "best-match",
                "title": "Best Match",
                "artists": "Coldplay",
                "genres": "Rock",
                "language": "English",
            },
            {
                "seokey": "already-liked",
                "title": "Favorite",
                "artists": "Coldplay",
                "genres": "Rock",
                "language": "English",
            },
        ],
        trending=[
            {
                "seokey": "weak-match",
                "title": "Weak Match",
                "artists": "Another Artist",
                "genres": "Pop",
                "language": "English",
            }
        ],
    )

    service = PersonalizedMusicService(repository)  # type: ignore[arg-type]
    results = await service.recommendations("user-1", catalog, 10)

    assert results[0]["seokey"] == "best-match"
    assert "already-liked" not in {track["seokey"] for track in results}
    assert any("Coldplay" in reason for reason in results[0]["recommendation"]["reasons"])
    assert catalog.languages == ["English"]


@pytest.mark.asyncio
async def test_cold_start_uses_english_trending():
    repository = FakeRepository()
    catalog = FakeCatalog(
        search_results=[],
        trending=[{"seokey": "trending-song", "title": "Trending"}],
    )

    service = PersonalizedMusicService(repository)  # type: ignore[arg-type]
    results = await service.recommendations("new-user", catalog, 5)

    assert catalog.searches == []
    assert catalog.languages == ["English"]
    assert results[0]["seokey"] == "trending-song"


@pytest.mark.asyncio
async def test_recent_tracks_are_demoted():
    repository = FakeRepository(
        profile={"favorite_artists": ["Coldplay"], "languages": ["English"]},
        history=[{"seokey": "recent"}],
    )
    catalog = FakeCatalog(
        search_results=[
            {"seokey": "recent", "artists": "Coldplay", "language": "English"},
            {"seokey": "fresh", "artists": "Coldplay", "language": "English"},
        ],
        trending=[],
    )

    service = PersonalizedMusicService(repository)  # type: ignore[arg-type]
    results = await service.recommendations("user-1", catalog, 5)

    assert results[0]["seokey"] == "fresh"
