"""
PersonalizedMusicService – Orchestrates production-grade music recommendations.

Three-Stage Architecture:
  Stage 1: Multi-Source Candidate Retrieval (~650 candidates across 7 sources)
  Stage 2: Multi-Factor Feature Scoring with time decay, session intent, skip, repeat, & impression penalties
  Stage 3: Re-ranking with diversity caps, adaptive discovery balancing, canonical deduplication, and explanation tags
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from api.catalog.normalize import song
from api.personalization.candidate_generator import CandidateGenerator
from api.personalization.mix_generator import MixGenerator
from api.personalization.models import TrackSnapshot, UserTasteProfile
from api.personalization.profile_engine import ProfileEngine
from api.personalization.ranking_engine import RankingEngine

logger = logging.getLogger(__name__)


class MusicCatalog(Protocol):
    async def search_songs(self, search_query: str, limit: int) -> list[dict[str, Any]] | dict[str, Any]: ...
    async def get_trending(self, language: str, limit: int) -> list[dict[str, Any]] | dict[str, Any]: ...
    async def get_new_releases(self, language: str, limit: int) -> list[dict[str, Any]] | dict[str, Any]: ...


class PersonalizedMusicService:
    MAX_SEARCH_SEEDS      = 6    # top artist+genre seeds to search
    CANDIDATES_PER_SEED   = 10   # tracks fetched per seed
    CANDIDATES_TRENDING   = 20   # tracks fetched per language trending
    CANDIDATES_NEW        = 15   # tracks fetched for new releases
    MAX_PER_ARTIST        = 2    # diversity cap per artist in final list
    HISTORY_CONTEXT       = 100  # events to load for signal computation

    def __init__(self, repository: Any, cache: Any | None = None) -> None:
        self.repository = repository
        self.cache = cache
        self.profile_engine = ProfileEngine(cache=cache)
        self.ranking_engine = RankingEngine()
        self.mix_generator = MixGenerator(repository=repository)

    # ─── Public Recommendation API ────────────────────────────────────────────

    async def recommendations(
        self,
        uid: str,
        catalog: MusicCatalog,
        limit: int = 20,
        refresh_generation: int = 0,
        session_id: str | None = None,
        exclude_ids: list[str] | set[str] | None = None,
        cursor: int = 0,
    ) -> list[dict[str, Any]]:
        """Return up to *limit* personalized track recommendations using 3-stage pipeline."""

        # 1. Gather user data concurrently
        b_events_task = (
            self.repository.list_behavioral_events(uid, 100)
            if hasattr(self.repository, "list_behavioral_events")
            else asyncio.sleep(0, result=[])
        )

        profile_row, favorites, history, behavioral_events = await asyncio.gather(
            self.repository.get_profile(uid),
            self.repository.list_favorites(uid),
            self.repository.list_history(uid, self.HISTORY_CONTEXT),
            b_events_task,
        )

        # 2. Build 3-tier user taste profile (long-term, short-term, session intent)
        taste_profile = self.profile_engine.build_profile(
            user_id=uid,
            profile_row=profile_row,
            favorites=favorites,
            history_events=history,
            behavioral_events=behavioral_events,
            current_session_id=session_id,
        )

        # Asynchronously persist taste profile to database
        if hasattr(self.repository, "save_taste_profile"):
            try:
                asyncio.create_task(self.repository.save_taste_profile(uid, taste_profile.model_dump()))
            except Exception:
                pass

        # 3. Stage 1: Multi-Source Candidate Generation (~650 candidates)
        cand_gen = CandidateGenerator(catalog, repository=self.repository)
        candidates = await cand_gen.generate_candidates(
            taste_profile,
            recent_history=history,
            refresh_generation=refresh_generation,
        )

        if not candidates:
            return []

        # 4. Gather history map & impression penalties
        excluded_keys: set[str] = {
            str(item.get("seokey", "")).lower().strip() for item in favorites if item.get("seokey")
        }
        if exclude_ids:
            for ex in exclude_ids:
                if ex:
                    excluded_keys.add(str(ex).lower().strip())

        history_map: dict[str, dict[str, Any]] = {}
        for event in history:
            sk = str(event.get("seokey") or event.get("id") or "").lower().strip()
            if sk:
                history_map[sk] = event

        candidate_ids = [
            str(c.get("id") or c.get("seokey") or "").strip()
            for c in candidates if str(c.get("id") or c.get("seokey"))
        ]
        impression_counts: dict[str, int] = {}
        if hasattr(self.repository, "get_impression_counts"):
            try:
                impression_counts = await self.repository.get_impression_counts(uid, candidate_ids)
            except Exception as exc:
                logger.debug("Failed to fetch impression counts: %s", exc)

        # 5. Stage 2: Score Candidates
        filtered_candidates: list[dict[str, Any]] = []
        for track in candidates:
            tid = str(track.get("id") or track.get("seokey") or "").lower().strip()
            if not tid or tid in excluded_keys:
                continue

            score, reasons = self.ranking_engine.score_candidate(
                track,
                taste_profile,
                history_map,
                impression_counts,
            )
            track["_ranking_score"] = score
            track["_ranking_reasons"] = reasons
            filtered_candidates.append(track)

        # 6. Stage 3: Re-Ranking & Diversity
        diverse_page = self.ranking_engine.rerank_and_diversify(
            filtered_candidates,
            max_per_artist=self.MAX_PER_ARTIST,
            discovery_receptivity=taste_profile.discovery_receptivity,
            limit=limit,
            offset=cursor,
        )

        # Asynchronously record impressions for shown items
        if hasattr(self.repository, "record_impressions") and diverse_page:
            try:
                asyncio.create_task(self.repository.record_impressions(uid, diverse_page, "home_feed"))
            except Exception:
                pass

        return diverse_page

    async def get_taste_profile(self, uid: str, session_id: str | None = None) -> UserTasteProfile:
        """Return the current 3-tier taste profile for a user UUID."""
        b_events_task = (
            self.repository.list_behavioral_events(uid, 100)
            if hasattr(self.repository, "list_behavioral_events")
            else asyncio.sleep(0, result=[])
        )
        profile_row, favorites, history, behavioral_events = await asyncio.gather(
            self.repository.get_profile(uid),
            self.repository.list_favorites(uid),
            self.repository.list_history(uid, self.HISTORY_CONTEXT),
            b_events_task,
        )
        return self.profile_engine.build_profile(
            user_id=uid,
            profile_row=profile_row,
            favorites=favorites,
            history_events=history,
            behavioral_events=behavioral_events,
            current_session_id=session_id,
        )

    async def get_personalized_mixes(
        self,
        uid: str,
        catalog: MusicCatalog,
    ) -> list[dict[str, Any]]:
        """Generate virtual personalized mix snapshots (Daily Mix, On Repeat, Rediscover, Language Mixes)."""
        taste_profile = await self.get_taste_profile(uid)
        favorites, history = await asyncio.gather(
            self.repository.list_favorites(uid),
            self.repository.list_history(uid, 60),
        )
        # Fetch candidate songs to populate mixes
        cand_gen = CandidateGenerator(catalog, repository=self.repository)
        candidates = await cand_gen.generate_candidates(taste_profile, recent_history=history)

        mixes = self.mix_generator.generate_mixes(
            taste_profile,
            favorites=favorites,
            history=history,
            candidates=candidates,
        )
        return [m.model_dump(mode="json") for m in mixes]

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


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _history_event_to_track_snapshot(event: dict[str, Any]) -> Any | None:
    """Convert a raw history dict to a TrackSnapshot, or None on failure."""
    try:
        return TrackSnapshot.model_validate(event)
    except Exception:
        return None
