"""
Multi-Source Candidate Generation Layer.

Retrieves a diverse candidate pool of ~600-700 candidate songs across 7 independent streams:
  1. Favorite & Similar Artists (~150 tracks)
  2. Language-Based Pools (~100 tracks)
  3. Recent History & Song Similarity (~100 tracks)
  4. Anonymous Collaborative Filtering (~100 tracks)
  5. Trending in Preferred Languages (~75 tracks)
  6. New Releases (~75 tracks)
  7. Discovery & Exploration (~50 tracks)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from api.catalog.normalize import items, song
from api.personalization.models import UserTasteProfile

logger = logging.getLogger(__name__)


class CatalogProvider(Protocol):
    async def search_songs(self, query: str, limit: int) -> Any: ...
    async def get_trending(self, language: str, limit: int) -> Any: ...
    async def get_new_releases(self, language: str, limit: int) -> Any: ...


def _clean_candidates(raw_result: Any) -> list[dict[str, Any]]:
    if isinstance(raw_result, Exception) or not raw_result:
        return []
    if isinstance(raw_result, list):
        out = []
        for r in raw_result:
            if isinstance(r, dict):
                norm = song(r)
                if norm.get("id") or norm.get("seokey"):
                    out.append(norm)
        return out
    if isinstance(raw_result, dict):
        return items(raw_result, "song")
    return []


class CandidateGenerator:
    """Orchestrates multi-source candidate track generation."""

    def __init__(self, catalog: CatalogProvider, repository: Any | None = None) -> None:
        self.catalog = catalog
        self.repository = repository

    async def generate_candidates(
        self,
        profile: UserTasteProfile,
        recent_history: list[dict[str, Any]],
        refresh_generation: int = 0,
        session_query: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch ~650 candidate tracks across all 7 sources concurrently."""

        jobs: list[tuple[str, str, Any]] = []

        search_fn = getattr(self.catalog, "search_candidates", self.catalog.search_songs)

        # 1. Favorite & Similar Artists (~150 candidates)
        # Sort artists by affinity, pick top 6 rotating with refresh_generation
        sorted_artists = sorted(profile.artists.items(), key=lambda x: x[1], reverse=True)
        if sorted_artists:
            offset = (refresh_generation * 2) % max(1, len(sorted_artists))
            chosen_artists = (sorted_artists[offset:] + sorted_artists[:offset])[:6]
            for artist_name, affinity in chosen_artists:
                jobs.append((
                    "favorite_artist",
                    f"Because you like {artist_name.title()}",
                    search_fn(artist_name, 25),
                ))

        # 2. Preferred Languages (~100 candidates)
        sorted_langs = sorted(profile.languages.items(), key=lambda x: x[1], reverse=True)
        top_langs = [l for l, _ in sorted_langs[:3]]
        for lang in top_langs:
            jobs.append((
                "language_catalog",
                f"Popular in {lang.title()}",
                search_fn(f"{lang} hits", 30),
            ))

        # 3. Recent History & Song Similarity (~100 candidates)
        # Use recent tracks completed or replayed to find similar sound
        recent_seeds = []
        for h in recent_history[:5]:
            title = h.get("title") or ""
            artist = h.get("artist") or (h.get("artists")[0] if h.get("artists") else "")
            if isinstance(artist, dict):
                artist = artist.get("name") or ""
            seed = f"{title} {artist}".strip()
            if seed and seed not in recent_seeds:
                recent_seeds.append((title, seed))

        for title, seed in recent_seeds[:4]:
            jobs.append((
                "song_similarity",
                f"Because you played {title}",
                search_fn(seed, 25),
            ))

        # 4. Session Query Intent (if active session searched something)
        if session_query:
            jobs.append((
                "session_intent",
                f"Related to your recent search '{session_query}'",
                search_fn(session_query, 30),
            ))

        # 5. Trending in Preferred Languages (~75 candidates)
        for lang in top_langs[:3]:
            jobs.append((
                "trending",
                f"Trending in {lang.title()}",
                self.catalog.get_trending(lang.title(), 35),
            ))

        # 6. New Releases (~75 candidates)
        if hasattr(self.catalog, "get_new_releases") and top_langs:
            target_lang = top_langs[refresh_generation % len(top_langs)]
            jobs.append((
                "new_release",
                f"New releases in {target_lang.title()}",
                self.catalog.get_new_releases(target_lang.title(), 35),
            ))

        # 7. Collaborative & Discovery candidates from DB repository (~100 candidates)
        if self.repository and hasattr(self.repository, "get_collaborative_candidates"):
            jobs.append((
                "collaborative",
                "Popular with listeners like you",
                self.repository.get_collaborative_candidates(profile.user_id, limit=40),
            ))

        # Execute all retrieval jobs concurrently
        raw_results = await asyncio.gather(*[coro for _, _, coro in jobs], return_exceptions=True)

        candidates: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for (source, reason, _), result in zip(jobs, raw_results):
            tracks = _clean_candidates(result)
            for t in tracks:
                tid = str(t.get("id") or t.get("seokey") or "").lower().strip()
                if not tid or tid in seen_ids:
                    continue
                seen_ids.add(tid)
                cand = dict(t)
                cand["_candidate_source"] = source
                cand["_candidate_reason"] = reason
                candidates.append(cand)

        logger.info(
            "Candidate generation user=%s refresh=%d total_candidates=%d",
            profile.user_id, refresh_generation, len(candidates),
        )
        return candidates
