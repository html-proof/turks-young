"""
Ranking & Re-Ranking Engine.

Two-stage machine learning / ranking scoring:
  Stage 2: Multi-feature score calculation with skip, repetition, cooldown, and impression penalties.
  Stage 3: Diversity enforcement (artist caps, no consecutive same artist),
           adaptive exploitation vs. discovery balance (70/20/10),
           canonical deduplication, and reason explanation generation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import math
import re
from typing import Any

from api.catalog.normalize import _clean_artist_id, _clean_artist_str
from api.catalog.search.ranking import normalize_query
from api.personalization.models import UserTasteProfile
from api.personalization.profile_engine import extract_era

logger = logging.getLogger(__name__)


def _canonical_key(track: dict[str, Any]) -> str:
    """Canonical deduplication key across title, primary artist, and approximate duration."""
    title = normalize_query(str(track.get("title") or track.get("name") or ""))
    artists = track.get("artists") or [track.get("artist")] or []
    first_art = ""
    if artists:
        first_art = _clean_artist_str(artists[0])
    art_norm = normalize_query(first_art)
    dur = int(track.get("duration") or track.get("duration_ms", 0) / 1000 or 0)
    # Group within 10-second duration buckets
    dur_bucket = dur // 10
    return f"{title}:{art_norm}:{dur_bucket}"


class RankingEngine:
    """Scores candidate tracks and performs diversity / freshness re-ranking."""

    def __init__(self, weights: dict[str, float] | None = None) -> None:
        self.weights = weights or {
            "artist_affinity": 0.25,
            "language_affinity": 0.15,
            "song_similarity": 0.12,
            "session_intent": 0.10,
            "collaborative_score": 0.10,
            "genre_affinity": 0.08,
            "freshness": 0.07,
            "popularity": 0.05,
            "new_release": 0.05,
            "discovery": 0.03,
        }

    def score_candidate(
        self,
        track: dict[str, Any],
        profile: UserTasteProfile,
        history_map: dict[str, dict[str, Any]],
        impression_counts: dict[str, int],
        now: datetime | None = None,
    ) -> tuple[float, list[str]]:
        """Calculate weighted score for a candidate track."""
        now = now or datetime.now(timezone.utc)
        reasons: list[str] = []

        # 1. Artist Affinity (0.0 to 1.0)
        track_artists = track.get("artists") or [track.get("artist")] or []
        artist_scores = []
        primary_artist = ""
        for a in track_artists:
            name = _clean_artist_str(a)
            aid = _clean_artist_id(a)
            if not primary_artist and name:
                primary_artist = name
            for key in (aid.lower(), name.lower()):
                if key and key in profile.artists:
                    artist_scores.append(profile.artists[key])
        artist_affinity = max(artist_scores) if artist_scores else 0.0
        if artist_affinity >= 0.7 and primary_artist:
            reasons.append(f"Because you like {primary_artist}")

        # 2. Language Affinity (0.0 to 1.0)
        lang = str(track.get("language") or "").lower().strip()
        lang_affinity = profile.languages.get(lang, 0.0)
        if lang_affinity >= 0.6 and lang:
            reasons.append(f"Popular in {lang.title()}")

        # 3. Genre Affinity
        genres = track.get("genres") or []
        genre_scores = [profile.genres.get(str(g).lower().strip(), 0.0) for g in genres if g]
        genre_affinity = max(genre_scores) if genre_scores else 0.0

        # 4. Era Affinity
        era = extract_era(track.get("release_date") or track.get("year"))
        era_affinity = profile.eras.get(era, 0.0)

        # 5. Session Intent Match
        session_artists = profile.current_session.get("artists", {})
        session_match = 0.0
        for a in track_artists:
            name = a.get("name") if isinstance(a, dict) else str(a or "")
            if name and name.lower().strip() in session_artists:
                session_match = 1.0
                reasons.append(f"Matching your current session interest in {name}")
                break

        # 6. Source-Specific Boosts
        src = track.get("_candidate_source", "")
        collab_score = 0.8 if src == "collaborative" else 0.0
        if collab_score > 0:
            reasons.append("Popular with listeners like you")

        new_rel_score = 0.9 if src == "new_release" else 0.0
        if new_rel_score > 0:
            reasons.append("New release from an artist you love")

        discovery_score = 0.7 if src == "discovery" else 0.0
        if discovery_score > 0:
            reasons.append("Discover something new")

        cand_reason = track.get("_candidate_reason")
        if cand_reason and cand_reason not in reasons:
            reasons.append(cand_reason)

        # 7. Popularity
        pop_raw = track.get("popularity") or track.get("favorite_count") or 0.5
        popularity = max(0.0, min(1.0, float(pop_raw) if float(pop_raw) <= 1.0 else float(pop_raw) / 10000.0))

        # Base Feature Score
        w = self.weights
        base_score = (
            w["artist_affinity"] * artist_affinity +
            w["language_affinity"] * lang_affinity +
            w["genre_affinity"] * genre_affinity +
            w["session_intent"] * session_match +
            w["collaborative_score"] * collab_score +
            w["new_release"] * new_rel_score +
            w["popularity"] * popularity +
            w["discovery"] * discovery_score +
            0.05 * era_affinity
        )

        # 8. Penalties & Cooldowns
        tid = str(track.get("id") or track.get("seokey") or "").lower().strip()

        # 8a. History Cooldown & Skip Penalties
        cooldown_penalty = 0.0
        skip_penalty = 0.0
        if tid in history_map:
            hist_item = history_map[tid]
            played_at_str = hist_item.get("played_at") or hist_item.get("started_at")
            if played_at_str:
                try:
                    p_time = datetime.fromisoformat(played_at_str.replace("Z", "+00:00"))
                    if not p_time.tzinfo:
                        p_time = p_time.replace(tzinfo=timezone.utc)
                    age = now - p_time
                    # Played within last 2 hours: strong cooldown
                    if age <= timedelta(hours=2):
                        cooldown_penalty = 0.60
                    elif age <= timedelta(days=1):
                        cooldown_penalty = 0.30
                    elif age <= timedelta(days=3):
                        cooldown_penalty = 0.15
                except ValueError:
                    pass

            # Skip penalty for this specific track
            ratio = float(hist_item.get("completion_ratio") or 0.0)
            played_sec = int(hist_item.get("played_seconds") or 0)
            if played_sec > 0 and played_sec < 15 and ratio < 0.20:
                skip_penalty = 0.40

        # 8b. Impression Fatigue Penalty (shown without engagement)
        impression_count = impression_counts.get(tid, 0)
        impression_penalty = 0.0
        if impression_count >= 5:
            impression_penalty = 0.50
        elif impression_count >= 3:
            impression_penalty = 0.25

        final_score = base_score - cooldown_penalty - skip_penalty - impression_penalty
        return round(max(0.01, final_score), 4), reasons[:3]

    def rerank_and_diversify(
        self,
        scored_candidates: list[dict[str, Any]],
        max_per_artist: int = 2,
        discovery_receptivity: float = 0.5,
        limit: int = 30,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Stage 3: Apply artist repetition caps, canonical deduplication, and discovery blend."""

        # Sort descending by ranking score
        sorted_cands = sorted(
            scored_candidates,
            key=lambda x: x.get("_ranking_score", 0.0),
            reverse=True,
        )

        # 1. Canonical deduplication
        deduped: list[dict[str, Any]] = []
        seen_canonical: set[str] = set()
        seen_ids: set[str] = set()

        for c in sorted_cands:
            cid = str(c.get("id") or c.get("seokey") or "").lower().strip()
            ckey = _canonical_key(c)
            if cid in seen_ids or ckey in seen_canonical:
                continue
            seen_ids.add(cid)
            seen_canonical.add(ckey)
            deduped.append(c)

        # 2. Separate into Core Personalized vs Discovery candidates
        core_pool: list[dict[str, Any]] = []
        discovery_pool: list[dict[str, Any]] = []

        for c in deduped:
            src = c.get("_candidate_source", "")
            if src in ("discovery", "collaborative", "new_release"):
                discovery_pool.append(c)
            else:
                core_pool.append(c)

        # 3. Interleave Core & Discovery based on discovery_receptivity
        # e.g., 0.5 receptivity -> 3 core, 1 discovery (75/25)
        # 0.8 receptivity -> 2 core, 1 discovery (66/33)
        # 0.2 receptivity -> 5 core, 1 discovery (83/17)
        interleave_step = max(2, min(6, int(1.0 / max(0.1, discovery_receptivity))))

        interleaved: list[dict[str, Any]] = []
        c_idx = 0
        d_idx = 0

        while c_idx < len(core_pool) or d_idx < len(discovery_pool):
            for _ in range(interleave_step):
                if c_idx < len(core_pool):
                    interleaved.append(core_pool[c_idx])
                    c_idx += 1
            if d_idx < len(discovery_pool):
                interleaved.append(discovery_pool[d_idx])
                d_idx += 1

        # 4. Enforce Artist Diversity Caps (max 2-3 per artist, no consecutive same artist)
        remaining = list(interleaved)
        final_list: list[dict[str, Any]] = []
        artist_counts: dict[str, int] = {}
        last_artist = ""

        def _primary_art(item: dict[str, Any]) -> str:
            arts = item.get("artists") or [item.get("artist")] or []
            if arts:
                return _clean_artist_str(arts[0]).lower().strip()
            return ""

        while remaining and len(final_list) < limit:
            # 1. Prefer candidate whose artist != last_artist and count < max_per_artist
            chosen_idx = None
            for idx, item in enumerate(remaining):
                art = _primary_art(item)
                count = artist_counts.get(art, 0)
                if count >= max_per_artist:
                    continue
                if art != last_artist or not art:
                    chosen_idx = idx
                    break

            # 2. If all available items have the same artist, pick next under cap
            if chosen_idx is None:
                for idx, item in enumerate(remaining):
                    art = _primary_art(item)
                    if artist_counts.get(art, 0) < max_per_artist:
                        chosen_idx = idx
                        break

            if chosen_idx is None:
                break

            chosen_item = remaining.pop(chosen_idx)
            final_list.append(chosen_item)
            art = _primary_art(chosen_item)
            if art:
                artist_counts[art] = artist_counts.get(art, 0) + 1
                last_artist = art

        # Apply pagination offset & limit
        page = final_list[offset : offset + limit]

        # Clean up internal candidate helper fields
        cleaned_page = []
        for item in page:
            record = dict(item)
            score = record.pop("_ranking_score", 0.0)
            reasons = record.pop("_ranking_reasons", [])
            record.pop("_candidate_source", None)
            record.pop("_candidate_reason", None)
            record["seokey"] = str(record.get("seokey") or record.get("id") or "")
            record["recommendation"] = {
                "score": round(score, 3),
                "reasons": reasons,
            }
            cleaned_page.append(record)

        return cleaned_page
