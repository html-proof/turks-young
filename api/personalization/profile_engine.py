"""
User Taste & Preference Profile Engine.

Generates and continuously updates 3-tier preference vectors for each user UUID:
  1. Long-Term Profile (exponential time-decay across historical interactions)
  2. Short-Term Profile (rolling 7-day high-weight window)
  3. Current-Session Profile (real-time session intent vector)

Blends them dynamically:
  Final = 0.50 * LongTerm + 0.30 * ShortTerm + 0.20 * CurrentSession
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import math
import re
from typing import Any

from api.personalization.models import BehavioralEvent, ListeningEvent, UserTasteProfile

logger = logging.getLogger(__name__)

DEFAULT_SIGNAL_WEIGHTS = {
    "liked": 8.0,
    "playlist_add": 7.0,
    "replay": 6.0,
    "download": 6.0,
    "artist_follow": 7.0,
    "completion_90": 5.0,
    "completion_60": 3.0,
    "search_and_play": 4.0,
    "completion_20_60": 1.0,
    "skip_under_10": -7.0,
    "skip_under_30": -4.0,
    "dislike": -10.0,
    "unliked": -6.0,
    "playlist_remove": -5.0,
}

SESSION_WINDOW_HOURS = 2


def _parse_utc(ts: Any) -> datetime:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, str) and ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def compute_time_decay(event_time: datetime, now: datetime | None = None) -> float:
    """Return exponential / stepped temporal decay multiplier (0.20 to 1.00)."""
    now = now or datetime.now(timezone.utc)
    age = now - event_time
    if age <= timedelta(days=1):
        return 1.00
    if age <= timedelta(days=7):
        return 0.90
    if age <= timedelta(days=30):
        return 0.70
    if age <= timedelta(days=90):
        return 0.45
    return 0.20


def extract_era(year_or_date: Any) -> str:
    """Classify release year into an era category."""
    if not year_or_date:
        return ""
    try:
        yr = int(str(year_or_date)[:4])
    except (ValueError, TypeError):
        return ""
    if yr >= 2020:
        return "2020s"
    if yr >= 2010:
        return "2010s"
    if yr >= 2000:
        return "2000s"
    if yr >= 1990:
        return "1990s"
    if yr > 1900:
        return "classic"
    return ""


def evaluate_event_weight(
    event_type: str,
    completion_ratio: float = 0.0,
    played_seconds: int = 0,
    source: str = "app",
    weights: dict[str, float] | None = None,
) -> tuple[float, str]:
    """Compute score impact and signal category from an event."""
    w = weights or DEFAULT_SIGNAL_WEIGHTS

    etype = (event_type or "").lower().strip()
    if etype in ("song_liked", "like", "favorite_add"):
        return w["liked"], "liked"
    if etype in ("song_unliked", "unlike", "favorite_remove"):
        return w["unliked"], "unliked"
    if etype in ("dislike", "explicit_dislike"):
        return w["dislike"], "dislike"
    if etype in ("playlist_song_added", "playlist_add"):
        return w["playlist_add"], "playlist_add"
    if etype in ("playlist_song_removed", "playlist_remove"):
        return w["playlist_remove"], "playlist_remove"
    if etype in ("song_downloaded", "download"):
        return w["download"], "download"
    if etype in ("artist_followed", "follow_artist"):
        return w["artist_follow"], "artist_follow"
    if etype in ("song_repeated", "replay"):
        return w["replay"], "replay"

    # Playback duration and completion ratios
    if played_seconds > 0 and played_seconds < 10 and completion_ratio < 0.15:
        return w["skip_under_10"], "skip_under_10"
    if played_seconds > 0 and played_seconds < 30 and completion_ratio < 0.30:
        return w["skip_under_30"], "skip_under_30"

    if completion_ratio >= 0.90 or etype == "song_completed":
        base = w["completion_90"]
        if "search" in source.lower():
            base += w["search_and_play"]
        return base, "completion_90"

    if completion_ratio >= 0.60:
        base = w["completion_60"]
        if "search" in source.lower():
            base += w["search_and_play"]
        return base, "completion_60"

    if completion_ratio >= 0.20:
        return w["completion_20_60"], "completion_20_60"

    # Search and play click
    if "search" in source.lower() and (etype in ("song_play", "search_result_clicked")):
        return w["search_and_play"], "search_and_play"

    # Neutral or weak baseline play
    return 1.0, "neutral_play"


def normalize_affinity_map(raw_map: dict[str, float], top_n: int = 50) -> dict[str, float]:
    """Normalize score values into 0.0 – 1.0 relative affinity scores."""
    if not raw_map:
        return {}
    # Filter non-positive values
    positive = {k: v for k, v in raw_map.items() if v > 0}
    if not positive:
        return {}
    max_val = max(positive.values())
    if max_val <= 0:
        return {}
    sorted_items = sorted(positive.items(), key=lambda x: x[1], reverse=True)[:top_n]
    return {k: round(v / max_val, 3) for k, v in sorted_items}


class ProfileEngine:
    """Maintains, blends, and evolves user music preference profiles."""

    def __init__(self, cache: Any | None = None) -> None:
        self.cache = cache

    def build_profile(
        self,
        user_id: str,
        profile_row: dict[str, Any],
        favorites: list[dict[str, Any]],
        history_events: list[dict[str, Any]],
        behavioral_events: list[dict[str, Any]],
        current_session_id: str | None = None,
        now: datetime | None = None,
    ) -> UserTasteProfile:
        """Construct full 3-tier taste profile from all available data under the user_uuid."""
        now = now or datetime.now(timezone.utc)

        long_term: dict[str, dict[str, float]] = {
            "languages": {}, "artists": {}, "genres": {}, "eras": {}
        }
        short_term: dict[str, dict[str, float]] = {
            "languages": {}, "artists": {}, "genres": {}, "eras": {}
        }
        current_session: dict[str, dict[str, float]] = {
            "languages": {}, "artists": {}, "genres": {}, "eras": {}
        }

        # 1. Seed Onboarding preferences (explicit baseline)
        onboarding_langs = profile_row.get("language_ids") or profile_row.get("languages") or []
        for lang in onboarding_langs:
            if lang:
                long_term["languages"][lang.lower().strip()] = 6.0

        onboarding_artists = profile_row.get("favorite_artist_ids") or profile_row.get("favorite_artists") or []
        for art in onboarding_artists:
            if art:
                long_term["artists"][art.lower().strip()] = 8.0

        onboarding_genres = profile_row.get("favorite_genres") or []
        for g in onboarding_genres:
            if g:
                long_term["genres"][g.lower().strip()] = 5.0

        # 2. Ingest explicit library favorites
        for fav in favorites:
            fav_time = _parse_utc(fav.get("favorited_at"))
            decay = compute_time_decay(fav_time, now)
            lang = str(fav.get("language") or "").lower().strip()
            if lang:
                long_term["languages"][lang] = long_term["languages"].get(lang, 0.0) + (8.0 * decay)
            for art in fav.get("artists") or [fav.get("artist")] or []:
                if isinstance(art, dict):
                    art = art.get("id") or art.get("name") or ""
                art_str = str(art or "").lower().strip()
                if art_str:
                    long_term["artists"][art_str] = long_term["artists"].get(art_str, 0.0) + (8.0 * decay)
            for g in fav.get("genres") or []:
                g_str = str(g or "").lower().strip()
                if g_str:
                    long_term["genres"][g_str] = long_term["genres"].get(g_str, 0.0) + (6.0 * decay)

        # 3. Ingest playback history and events
        combined_events = []
        for h in history_events:
            combined_events.append({
                "type": h.get("event_type") or ("song_completed" if h.get("completed") else "song_play"),
                "data": h,
                "time": _parse_utc(h.get("played_at") or h.get("started_at")),
                "session_id": h.get("session_id"),
                "source": h.get("source") or "history",
            })
        for b in behavioral_events:
            combined_events.append({
                "type": b.get("event_type") or "behavioral",
                "data": b,
                "time": _parse_utc(b.get("created_at") or b.get("timestamp")),
                "session_id": b.get("session_id"),
                "source": b.get("source") or "app",
            })

        interaction_count = len(combined_events)
        skip_count = 0
        play_count = 0
        discovery_skips = 0
        discovery_plays = 0

        for item in combined_events:
            ev_type = item["type"]
            ev_data = item["data"]
            ev_time = item["time"]
            ev_session = item["session_id"]
            ev_source = item["source"]

            ratio = float(ev_data.get("completion_ratio") or (1.0 if ev_data.get("completed") else 0.0))
            played_sec = int(ev_data.get("played_seconds") or 0)
            if played_sec == 0 and ev_data.get("duration_ms") and ev_data.get("position_ms"):
                played_sec = int(ev_data["position_ms"] / 1000)
                if ratio == 0.0:
                    ratio = min(1.0, played_sec / max(1, ev_data["duration_ms"] / 1000))

            weight, category = evaluate_event_weight(ev_type, ratio, played_sec, ev_source)
            if "skip" in category or weight < 0:
                skip_count += 1
                if "discovery" in ev_source.lower():
                    discovery_skips += 1
            elif weight > 1.0:
                play_count += 1
                if "discovery" in ev_source.lower():
                    discovery_plays += 1

            decay = compute_time_decay(ev_time, now)
            effective_long_weight = weight * decay

            is_short_term = (now - ev_time) <= timedelta(days=7)
            is_current_session = bool(
                current_session_id and ev_session == current_session_id and
                (now - ev_time) <= timedelta(hours=SESSION_WINDOW_HOURS)
            )

            # Metadata extraction
            lang = str(ev_data.get("language") or "").lower().strip()
            artists = ev_data.get("artists") or ([ev_data.get("artist")] if ev_data.get("artist") else [])
            genres = ev_data.get("genres") or []
            era = extract_era(ev_data.get("release_date") or ev_data.get("release_year") or ev_data.get("year"))

            def _apply(target_dict: dict[str, dict[str, float]], w: float):
                if lang:
                    target_dict["languages"][lang] = target_dict["languages"].get(lang, 0.0) + (w * 0.8)
                for a in artists:
                    if isinstance(a, dict):
                        a = a.get("id") or a.get("name") or ""
                    a_str = str(a or "").lower().strip()
                    if a_str:
                        target_dict["artists"][a_str] = target_dict["artists"].get(a_str, 0.0) + w
                for g in genres:
                    g_str = str(g or "").lower().strip()
                    if g_str:
                        target_dict["genres"][g_str] = target_dict["genres"].get(g_str, 0.0) + (w * 0.7)
                if era:
                    target_dict["eras"][era] = target_dict["eras"].get(era, 0.0) + (w * 0.5)

            _apply(long_term, effective_long_weight)
            if is_short_term:
                _apply(short_term, weight)
            if is_current_session:
                _apply(current_session, weight * 1.5)

        # 4. Compute Blended Vector: Final = 0.50 * LongTerm + 0.30 * ShortTerm + 0.20 * CurrentSession
        blended_languages: dict[str, float] = {}
        all_langs = set(long_term["languages"]) | set(short_term["languages"]) | set(current_session["languages"])
        for l in all_langs:
            val = (
                long_term["languages"].get(l, 0.0) * 0.50 +
                short_term["languages"].get(l, 0.0) * 0.30 +
                current_session["languages"].get(l, 0.0) * 0.20
            )
            blended_languages[l] = val

        blended_artists: dict[str, float] = {}
        all_arts = set(long_term["artists"]) | set(short_term["artists"]) | set(current_session["artists"])
        for a in all_arts:
            val = (
                long_term["artists"].get(a, 0.0) * 0.50 +
                short_term["artists"].get(a, 0.0) * 0.30 +
                current_session["artists"].get(a, 0.0) * 0.20
            )
            blended_artists[a] = val

        blended_genres: dict[str, float] = {}
        all_genres = set(long_term["genres"]) | set(short_term["genres"]) | set(current_session["genres"])
        for g in all_genres:
            val = (
                long_term["genres"].get(g, 0.0) * 0.50 +
                short_term["genres"].get(g, 0.0) * 0.30 +
                current_session["genres"].get(g, 0.0) * 0.20
            )
            blended_genres[g] = val

        blended_eras: dict[str, float] = {}
        all_eras = set(long_term["eras"]) | set(short_term["eras"]) | set(current_session["eras"])
        for e in all_eras:
            val = (
                long_term["eras"].get(e, 0.0) * 0.50 +
                short_term["eras"].get(e, 0.0) * 0.30 +
                current_session["eras"].get(e, 0.0) * 0.20
            )
            blended_eras[e] = val

        # 5. Cold-start to experienced user factor
        # If user has few interactions (< 10), onboarding stays prominent.
        # Once interactions reach 30, actual behavior dominates.
        if interaction_count < 10:
            for l in onboarding_langs:
                l_key = str(l or "").lower().strip()
                if l_key:
                    blended_languages[l_key] = max(blended_languages.get(l_key, 0.0), 5.0)
            for a in onboarding_artists:
                a_key = str(a or "").lower().strip()
                if a_key:
                    blended_artists[a_key] = max(blended_artists.get(a_key, 0.0), 8.0)

        # Discovery receptivity: increases when user plays discovery tracks; decreases on skips
        disc_total = discovery_plays + discovery_skips
        discovery_receptivity = 0.5
        if disc_total >= 3:
            discovery_receptivity = max(0.2, min(0.8, discovery_plays / disc_total))

        norm_languages = normalize_affinity_map(blended_languages, top_n=20)
        norm_artists = normalize_affinity_map(blended_artists, top_n=60)
        norm_genres = normalize_affinity_map(blended_genres, top_n=30)
        norm_eras = normalize_affinity_map(blended_eras, top_n=10)

        return UserTasteProfile(
            user_id=user_id,
            languages=norm_languages,
            artists=norm_artists,
            genres=norm_genres,
            eras=norm_eras,
            long_term=long_term,
            short_term=short_term,
            current_session=current_session,
            metrics={
                "interaction_count": interaction_count,
                "play_count": play_count,
                "skip_count": skip_count,
                "discovery_plays": discovery_plays,
                "discovery_skips": discovery_skips,
            },
            interaction_count=interaction_count,
            discovery_receptivity=round(discovery_receptivity, 2),
            algorithm_version="rec_v2",
            updated_at=now.isoformat(),
        )

    async def get_or_load_profile(
        self,
        user_id: str,
        loader_fn,
        ttl: int = 300,
    ) -> UserTasteProfile:
        """Fetch cached profile or compute via loader."""
        cache_key = f"user_taste:profile:{user_id}:v2"
        if self.cache and hasattr(self.cache, "get"):
            try:
                cached = await self.cache.get(cache_key)
                if cached and isinstance(cached, dict):
                    return UserTasteProfile.model_validate(cached)
            except Exception as exc:
                logger.debug("Profile cache read failed: %s", exc)

        profile = await loader_fn()
        if self.cache and hasattr(self.cache, "set") and profile:
            try:
                await self.cache.set(cache_key, profile.model_dump(mode="json"), ttl=ttl)
            except Exception as exc:
                logger.debug("Profile cache write failed: %s", exc)

        return profile
