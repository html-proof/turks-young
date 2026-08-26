"""
Pure-function scoring primitives used by PersonalizedMusicService.

Keeping these in a separate module makes them unit-testable without any I/O.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _items(value: Any) -> list[str]:
    """Return a normalised list of non-empty strings from str, list, or None."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _safe_seed(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\s\-'.]", " ", value).strip()[:100]


def _parse_utc(ts: str) -> datetime | None:
    """Parse an ISO-8601 timestamp string; return None on failure."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Temporal decay
# ---------------------------------------------------------------------------

_DECAY_BRACKETS: list[tuple[timedelta, float]] = [
    (timedelta(hours=6),  0.05),   # played in the last 6 h  → strong "skip" penalty
    (timedelta(days=1),   0.15),
    (timedelta(days=3),   0.40),
    (timedelta(days=7),   0.60),
    (timedelta(days=14),  0.80),
    (timedelta(days=30),  0.90),
]


def recency_penalty(played_at: str) -> float:
    """
    Return a multiplier (0–1) that penalises a track the more recently it was
    played.  Tracks not in history return 1.0 (no penalty).
    """
    ts = _parse_utc(played_at)
    if ts is None:
        return 1.0
    age = datetime.now(timezone.utc) - ts
    for threshold, penalty in _DECAY_BRACKETS:
        if age <= threshold:
            return penalty
    return 1.0   # > 30 days old → no penalty


# ---------------------------------------------------------------------------
# Preference scoring
# ---------------------------------------------------------------------------

def build_preference_scores(
    profile: dict[str, Any],
    favorites: list[dict[str, Any]],
    history: list[dict[str, Any]],
    signals: dict[str, Any],
) -> dict[str, dict[str, float]]:
    """
    Merge all taste signals into a single preference map:
    ``{"artists": {name: score}, "genres": {name: score}, "languages": {name: score}}``

    Sources and weights
    -------------------
    Explicit (profile)
        language      +3.0   favorite_genres  +5.0   favorite_artists +6.0

    Behavioral (favorites)
        language      +2.0   genre            +4.0   artist           +5.0

    Behavioral (history) – weighted by completion and recency
        completed (≥80 %)  language +1.5  genre +3.0  artist +4.0
        partial            language +0.5  genre +1.0  artist +1.5
        temporal decay applied to each history event

    Implicit signals (accumulated in Firebase)
        passed through at face value
    """
    from collections import defaultdict

    scores: dict[str, dict[str, float]] = {
        "artists":   defaultdict(float),
        "genres":    defaultdict(float),
        "languages": defaultdict(float),
    }

    # ── Explicit profile ──────────────────────────────────────────────────
    for artist in _items(profile.get("favorite_artists")):
        scores["artists"][artist.casefold()] += 6.0
    for genre in _items(profile.get("favorite_genres")):
        scores["genres"][genre.casefold()] += 5.0
    for lang in _items(profile.get("languages")):
        scores["languages"][lang.casefold()] += 3.0

    # ── Saved favorites ──────────────────────────────────────────────────
    for track in favorites:
        for artist in _items(track.get("artists")):
            scores["artists"][artist.casefold()] += 5.0
        for genre in _items(track.get("genres")):
            scores["genres"][genre.casefold()] += 4.0
        lang = str(track.get("language") or "").strip()
        if lang:
            scores["languages"][lang.casefold()] += 2.0

    # ── Listening history ────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    for event in history:
        played_at = str(event.get("played_at") or "")
        ts = _parse_utc(played_at)
        # Age-based weight: events from the last 7 days get full weight,
        # decaying linearly to 0.2 at 90 days.
        if ts:
            age_days = max(0, (now - ts).days)
            age_weight = max(0.2, 1.0 - age_days / 90)
        else:
            age_weight = 0.5

        completion = int(event.get("played_seconds", 0))
        completed = bool(event.get("completed", False))
        # Treat "completed" flag or >80 s of play as a strong signal.
        quality = 3.0 if (completed or completion >= 80) else 1.0
        weight = age_weight * quality

        for artist in _items(event.get("artists")):
            scores["artists"][artist.casefold()] += 4.0 * weight
        for genre in _items(event.get("genres")):
            scores["genres"][genre.casefold()] += 3.0 * weight
        lang = str(event.get("language") or "").strip()
        if lang:
            scores["languages"][lang.casefold()] += 1.5 * weight

    # ── Accumulated implicit signals ─────────────────────────────────────
    for bucket in ("artists", "genres", "languages"):
        bucket_signals = signals.get(bucket) or {}
        if not isinstance(bucket_signals, dict):
            continue
        for item in bucket_signals.values():
            if not isinstance(item, dict) or not item.get("value"):
                continue
            scores[bucket][str(item["value"]).casefold()] += float(item.get("score", 0))

    return {bucket: dict(values) for bucket, values in scores.items()}


# ---------------------------------------------------------------------------
# Candidate generation seeds
# ---------------------------------------------------------------------------

def build_search_seeds(
    preferences: dict[str, dict[str, float]],
    max_seeds: int = 6,
) -> list[str]:
    """
    Return up to *max_seeds* sanitised search strings drawn from the
    top-scoring artists and genres, interleaved so genres appear in the mix.
    """
    artists = sorted(preferences["artists"].items(), key=lambda x: x[1], reverse=True)
    genres  = sorted(preferences["genres"].items(),  key=lambda x: x[1], reverse=True)

    # Interleave artist / genre seeds so results are diverse.
    interleaved: list[str] = []
    for a, g in zip(artists, genres):
        interleaved.append(a[0])
        interleaved.append(g[0])
    # Append remaining artists if genres run out, and vice-versa.
    for a, _ in artists[len(genres):]:
        interleaved.append(a)
    for g, _ in genres[len(artists):]:
        interleaved.append(g)

    seeds: list[str] = []
    seen: set[str] = set()
    for raw in interleaved:
        seed = _safe_seed(raw)
        if seed and seed.casefold() not in seen:
            seeds.append(seed)
            seen.add(seed.casefold())
        if len(seeds) >= max_seeds:
            break
    return seeds


def preferred_languages(
    profile: dict[str, Any],
    preferences: dict[str, dict[str, float]],
    max_languages: int = 3,
) -> list[str]:
    """Return up to *max_languages* languages sorted by preference score."""
    lang_scores = preferences["languages"]
    if lang_scores:
        ranked = sorted(lang_scores.items(), key=lambda x: x[1], reverse=True)
        return [lang.title() for lang, _ in ranked[:max_languages]]
    profile_langs = _items(profile.get("languages"))
    return profile_langs[:max_languages]


# ---------------------------------------------------------------------------
# Track scoring
# ---------------------------------------------------------------------------

def score_track(
    track: dict[str, Any],
    preferences: dict[str, dict[str, float]],
    source: str,
    history_map: dict[str, str],          # seokey → most-recent played_at
) -> tuple[float, list[str]]:
    """
    Score a single candidate track and return ``(score, reasons)``.

    Score components
    ----------------
    Base                                           1.0
    Artist match  × preference_score             up to ~11
    Genre match   × preference_score × 0.8      up to ~8
    Language match × preference_score × 0.5     up to ~4.5
    Source bonus  (trending = 1.5, search = 1.0)
    Recency penalty (multiplier applied at the end)
    """
    score = 1.0
    reasons: list[str] = [source]

    # ── Artist ──────────────────────────────────────────────────────────
    artist_matches = [
        a for a in _items(track.get("artists"))
        if a.casefold() in preferences["artists"]
    ]
    for a in artist_matches:
        score += preferences["artists"][a.casefold()]
    if artist_matches:
        reasons.append(f"Matches artist {artist_matches[0]}")

    # ── Genre ────────────────────────────────────────────────────────────
    genre_matches = [
        g for g in _items(track.get("genres"))
        if g.casefold() in preferences["genres"]
    ]
    for g in genre_matches:
        score += preferences["genres"][g.casefold()] * 0.8
    if genre_matches:
        reasons.append(f"Matches genre {genre_matches[0]}")

    # ── Language ─────────────────────────────────────────────────────────
    lang = str(track.get("language") or "").casefold()
    if lang and lang in preferences["languages"]:
        score += preferences["languages"][lang] * 0.5
        reasons.append(f"In {track.get('language')}")

    # ── Source bonus ─────────────────────────────────────────────────────
    if "trending" in source.lower():
        score += 1.5
    else:
        score += 1.0

    # ── Recency penalty ──────────────────────────────────────────────────
    seokey = track.get("seokey", "")
    if seokey in history_map:
        penalty = recency_penalty(history_map[seokey])
        score *= penalty
        if penalty < 0.5:
            reasons.append("Played very recently")

    return score, reasons


# ---------------------------------------------------------------------------
# Diversity filter
# ---------------------------------------------------------------------------

def apply_diversity(
    ranked: list[dict[str, Any]],
    max_per_artist: int = 3,
) -> list[dict[str, Any]]:
    """
    Limit the number of consecutive / total tracks from the same artist
    so the final list feels varied.
    """
    artist_counts: dict[str, int] = {}
    result: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []

    for item in ranked:
        artists = _items(item.get("artists"))
        key = artists[0].casefold() if artists else "__unknown__"
        count = artist_counts.get(key, 0)
        if count < max_per_artist:
            artist_counts[key] = count + 1
            result.append(item)
        else:
            deferred.append(item)

    # Append deferred tracks (beyond per-artist cap) at the end.
    result.extend(deferred)
    return result
