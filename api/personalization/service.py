"""
PersonalizedMusicService – orchestrates recommendation generation.

Algorithm overview
------------------
1. Gather user data in parallel (profile, favorites, history, signals).
2. Build a unified preference-score map from all sources via scorer.py.
3. Generate candidate tracks from multiple parallel fetches:
   - Search the catalog with top artist/genre seeds (interleaved for variety).
   - Fetch trending tracks for every preferred language (not just the top one).
   - Fetch new releases for the top preferred language.
4. Score every candidate track against preferences; apply recency penalty.
5. Deduplicate, apply a per-artist diversity filter, and return top-N.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from api.personalization.repository import FirebaseUserRepository
from api.personalization.scorer import (
    apply_diversity,
    build_preference_scores,
    build_search_seeds,
    preferred_languages,
    score_track,
)

logger = logging.getLogger(__name__)


class MusicCatalog(Protocol):
    async def search_songs(self, search_query: str, limit: int) -> list[dict[str, Any]] | dict[str, Any]: ...
    async def get_trending(self, language: str, limit: int) -> list[dict[str, Any]] | dict[str, Any]: ...
    async def get_new_releases(self, language: str, limit: int) -> list[dict[str, Any]] | dict[str, Any]: ...


class PersonalizedMusicService:
    MAX_SEARCH_SEEDS      = 6    # top artist+genre seeds to search
    CANDIDATES_PER_SEED   = 8    # tracks fetched per seed
    CANDIDATES_TRENDING   = 12   # tracks fetched per language trending
    CANDIDATES_NEW        = 8    # tracks fetched for new releases
    MAX_PER_ARTIST        = 3    # diversity cap per artist in final list
    HISTORY_CONTEXT       = 100  # events to load for signal computation

    def __init__(self, repository: FirebaseUserRepository) -> None:
        self.repository = repository

    # ─── Public API ───────────────────────────────────────────────────────────

    async def recommendations(
        self,
        uid: str,
        catalog: MusicCatalog,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return up to *limit* personalised track recommendations."""

        # 1. Gather user data in parallel.
        profile, favorites, history, signals = await asyncio.gather(
            self.repository.get_profile(uid),
            self.repository.list_favorites(uid),
            self.repository.list_history(uid, self.HISTORY_CONTEXT),
            self.repository.get_signals(uid),
        )

        # 2. Build unified preference scores.
        preferences = build_preference_scores(profile, favorites, history, signals)
        is_cold_start = self._is_cold_start(preferences)
        if is_cold_start:
            logger.info("uid=%s cold-start — broadening candidate fetch", uid)

        seeds     = build_search_seeds(preferences, self.MAX_SEARCH_SEEDS)
        languages = preferred_languages(profile, preferences, max_languages=3)
        top_lang  = languages[0]

        # 3. Build candidate fetch jobs.
        jobs: list[tuple[str, Any]] = []

        # 3a. Catalog search seeds (artist + genre interleaved).
        per_seed = max(4, min(self.CANDIDATES_PER_SEED, limit))
        for seed in seeds:
            jobs.append((
                f"Because you like {seed}",
                catalog.search_songs(seed, per_seed),
            ))

        # 3b. Trending for ALL preferred languages (not just the top one).
        for lang in languages:
            jobs.append((
                f"Trending in {lang}",
                catalog.get_trending(lang, self.CANDIDATES_TRENDING),
            ))

        # 3c. New releases in the top language.
        if hasattr(catalog, "get_new_releases"):
            jobs.append((
                f"New in {top_lang}",
                catalog.get_new_releases(top_lang, self.CANDIDATES_NEW),
            ))

        # 3d. Cold-start fallback: broaden to generic "pop" / "hits" search.
        if is_cold_start:
            jobs.append(("Popular hits", catalog.search_songs("top hits", per_seed)))
            jobs.append((f"Popular in {top_lang}", catalog.search_songs(top_lang, per_seed)))

        # 4. Fetch all candidates concurrently.
        raw_results = await asyncio.gather(
            *[coro for _, coro in jobs],
            return_exceptions=True,
        )

        # 5. Flatten and filter.
        favorite_keys: set[str] = {item.get("seokey", "") for item in favorites}

        # Build a recency map: seokey → most-recent played_at string.
        history_map: dict[str, str] = {}
        for event in history:
            sk = event.get("seokey", "")
            if sk and (sk not in history_map or event.get("played_at", "") > history_map[sk]):
                history_map[sk] = event.get("played_at", "")

        ranked: dict[str, dict[str, Any]] = {}
        for (source, _), result in zip(jobs, raw_results):
            if isinstance(result, Exception):
                logger.warning("Candidate fetch failed for '%s': %s", source, result)
                continue
            if not isinstance(result, list):
                continue
            for track in result:
                if not isinstance(track, dict):
                    continue
                seokey = track.get("seokey")
                if not seokey or seokey in favorite_keys:
                    continue

                tr_score, reasons = score_track(track, preferences, source, history_map)

                existing = ranked.get(seokey)
                if existing is None or existing["recommendation"]["score"] < tr_score:
                    ranked[seokey] = {
                        **track,
                        "recommendation": {
                            "score": round(tr_score, 2),
                            "reasons": reasons[:3],
                        },
                    }

        # 6. Sort → diversity filter → return top-N.
        sorted_tracks = sorted(
            ranked.values(),
            key=lambda item: item["recommendation"]["score"],
            reverse=True,
        )
        diverse_tracks = apply_diversity(sorted_tracks, self.MAX_PER_ARTIST)
        return diverse_tracks[:limit]

    async def rebuild_signals_from_history(self, uid: str) -> int:
        """
        Recompute implicit signals by replaying the full listening history.
        Clears existing signals first so stale data is removed.
        Returns the number of history events processed.
        """
        history = await self.repository.list_history(uid, 500)
        await self.repository.clear_signals(uid)

        for event in history:
            try:
                track = _history_event_to_track_snapshot(event)
                if track is None:
                    continue
                completed = bool(event.get("completed", False))
                weight = 2.0 if completed else 1.0
                await self.repository.adjust_signals(uid, track, weight)
            except Exception as exc:
                logger.warning("Signal rebuild error for event: %s", exc)

        return len(history)

    # ─── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _is_cold_start(preferences: dict[str, dict[str, float]]) -> bool:
        """True when the user has very few accumulated preference signals."""
        total = sum(
            sum(v for v in bucket.values())
            for bucket in preferences.values()
        )
        return total < 10.0


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _history_event_to_track_snapshot(event: dict[str, Any]) -> Any | None:
    """Convert a raw history dict to a TrackSnapshot, or None on failure."""
    from api.personalization.models import TrackSnapshot
    try:
        return TrackSnapshot.model_validate(event)
    except Exception:
        return None
