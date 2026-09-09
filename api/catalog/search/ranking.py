from __future__ import annotations

import enum
import logging
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

logger = logging.getLogger(__name__)

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_SEPARATORS = re.compile(r"\b(?:feat\.?|ft\.?|featuring|with)\b|\s+-\s+", re.I)

KNOWN_LANGUAGES = {
    "malayalam", "tamil", "hindi", "telugu", "kannada", "punjabi",
    "english", "bengali", "marathi", "bhojpuri", "gujarati", "urdu",
    "odia", "assamese",
}

_UNOFFICIAL_NOISE = re.compile(
    r"\b(?:cover|karaoke|instrumental|reverb|lo-?fi|slowed|ringtone|status|dj remix|tribute|short|dialogue promo|whatsapp status)\b",
    re.I,
)

_OST_KEYWORDS = re.compile(
    r"\b(?:original motion picture soundtrack|original soundtrack|ost|soundtrack)\b",
    re.I,
)

from api.catalog.labels import _KNOWN_OFFICIAL_LABELS, is_verified_label

# Indian phonetic transliteration substitutions
_PHONETIC_REPLACEMENTS = [
    ("neeyae", "nee"),
    ("neeye", "nee"),
    ("zh", "l"),
    ("th", "t"),
    ("ee", "i"),
    ("oo", "u"),
    ("aa", "a"),
    ("dh", "d"),
    ("bh", "b"),
    ("kh", "k"),
    ("gh", "g"),
    ("ph", "p"),
    ("sh", "s"),
    ("ch", "c"),
    ("v", "w"),
]


class RankingTier(enum.IntEnum):
    """Deterministic ranking tiers where Tier A always beats Tier B, C, and D."""
    TIER_A = 1  # Exact intent (exact title, album, artist, or full multi-field match)
    TIER_B = 2  # Highly relevant (title prefix, exact phrase, all words, transliteration, typo)
    TIER_C = 3  # Related (partial words, album/artist contains query, soundtrack)
    TIER_D = 4  # Discovery / weak fuzzy match


def normalize_query(value: str) -> str:
    """Normalize query and metadata for matching without altering display titles.

    Handles lowercase, extra spaces, punctuation, apostrophes, Unicode normalization,
    accents/diacritics removal, and duplicate spaces.
    """
    if not value:
        return ""
    # Strip diacritics / accents
    value = unicodedata.normalize("NFKD", str(value))
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("’", "'").replace("‘", "'").replace("`", "'")
    value = _SEPARATORS.sub(" ", value)
    value = _PUNCTUATION.sub(" ", value.casefold())
    return " ".join(value.split())


def _tokens(value: str) -> list[str]:
    return normalize_query(value).split()


def _text(item: dict[str, Any], kind: str) -> tuple[str, str, str]:
    if kind == "artist":
        return str(item.get("name") or item.get("title") or ""), "", ""
    title = str(item.get("title") or item.get("name") or "")
    artists = item.get("artists") or []
    artist_text = ", ".join(str(a.get("name") or a.get("title") or "") for a in artists if isinstance(a, dict))
    if not artist_text:
        if isinstance(item.get("artist"), dict):
            artist_text = str(item["artist"].get("name") or item["artist"].get("title") or "")
        elif isinstance(item.get("artist"), str):
            artist_text = item["artist"]
        elif isinstance(item.get("artists"), str):
            artist_text = item["artists"]
    album_val = item.get("album")
    album_text = ""
    if isinstance(album_val, dict):
        album_text = str(album_val.get("title") or album_val.get("name") or "")
    elif isinstance(album_val, str):
        album_text = album_val
    return title, artist_text, album_text


def _levenshtein(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            ins = prev[j + 1] + 1
            dele = curr[j] + 1
            subs = prev[j] + (c1 != c2)
            curr.append(min(ins, dele, subs))
        prev = curr
    return prev[-1]


def _phonetic_normalize(text: str) -> str:
    t = normalize_query(text)
    for old, new in _PHONETIC_REPLACEMENTS:
        t = t.replace(old, new)
    return t


def _is_phonetic_match(query: str, target: str) -> bool:
    pq = _phonetic_normalize(query)
    pt = _phonetic_normalize(target)
    if not pq or not pt:
        return False
    if pq == pt or pt.startswith(pq) or pq in pt:
        return True
    pq_tokens = pq.split()
    pt_tokens = pt.split()
    if any(tok == pq or tok.startswith(pq) for tok in pt_tokens):
        return True
    if len(pq_tokens) > 1 and all(any(pt_t.startswith(pq_t) or pq_t in pt_t for pt_t in pt_tokens) for pq_t in pq_tokens):
        return True
    return False


def _is_typo_match(query: str, target: str) -> bool:
    """Typo tolerance with strict length-based thresholds:
    - length 1-3: avoid fuzzy correction
    - length 4-6: maximum edit distance 1
    - length 7+: maximum edit distance 2
    """
    q = normalize_query(query)
    t = normalize_query(target)
    if len(q) < 4 or not t:
        return False
    max_dist = 1 if len(q) <= 6 else 2
    if _levenshtein(q, t) <= max_dist:
        return True
    return any(_levenshtein(q, tok) <= max_dist for tok in t.split() if len(tok) >= 4)


def _similarity(query: str, value: str) -> float:
    if not value:
        return 0.0
    q_tokens, v_tokens = _tokens(query), _tokens(value)
    whole = SequenceMatcher(None, query, normalize_query(value)).ratio()
    token = sum(max(SequenceMatcher(None, token, candidate).ratio() for candidate in v_tokens) for token in q_tokens) if v_tokens else 0.0
    return max(whole, token / len(q_tokens) if q_tokens else 0.0)


def _matched_query_tokens(query_tokens: list[str], value: str) -> set[str]:
    value_tokens = _tokens(value)
    return {
        wanted for wanted in query_tokens
        if any(candidate == wanted or candidate.startswith(wanted) for candidate in value_tokens)
    }


def detect_intent(query: str, candidates: list[dict[str, Any]] | None = None) -> str:
    """Detects likely user intent: SONG, ARTIST, ALBUM, MOVIE, PLAYLIST, COMBINED, or GENERAL."""
    q = normalize_query(query)
    q_tokens = _tokens(q)
    if not q_tokens:
        return "GENERAL"
    
    if any(k in q_tokens for k in ["song", "songs", "track", "music", "audio"]):
        if any(k in q_tokens for k in ["movie", "soundtrack", "ost", "album", "film"]):
            return "ALBUM"
        return "SONG"
    if any(k in q_tokens for k in ["movie", "soundtrack", "ost", "album", "film"]):
        return "ALBUM"
    if any(k in q_tokens for k in ["singer", "artist", "composer", "director"]):
        return "ARTIST"
    if any(k in q_tokens for k in ["playlist", "hits", "top", "best", "collection", "party"]):
        return "PLAYLIST"

    # Multi-token combined search like "Vijay Ghilli" or "AR Rahman Vennilave"
    if len(q_tokens) >= 2:
        return "COMBINED"
    return "SONG"


def score_item(
    query: str,
    item: dict[str, Any],
    kind: str,
    user_languages: list[str] | None = None,
    user_artists: list[str] | None = None,
    history_tracks: list[dict[str, Any]] | None = None,
    previous_searches: list[str] | None = None,
) -> tuple[RankingTier, int, list[str]]:
    """Evaluates ranking tier, 1000-point score, and explanation reasons.

    The Current Search Query is always the strongest ranking signal.
    Tiers:
    - Tier A: Exact normalized title, exact album/movie, exact artist, complete title+artist combined match
    - Tier B: Title starts with query, exact phrase in title, all query words present, transliteration, typo
    - Tier C: Partial word match, album/artist contains query, soundtrack correlation
    - Tier D: Broad fuzzy match, discovery
    """
    q = normalize_query(query)
    if not q:
        return RankingTier.TIER_D, 0, ["empty_query"]

    q_tokens = _tokens(q)
    detected_lang = None
    core_tokens = []
    for token in q_tokens:
        if token in KNOWN_LANGUAGES and not detected_lang:
            detected_lang = token
        else:
            core_tokens.append(token)
    clean_q = " ".join(core_tokens) if core_tokens else q

    title, artists, album_name = _text(item, kind)
    norm_title = normalize_query(title)
    norm_artists = normalize_query(artists)
    norm_album = normalize_query(album_name)
    clean_album = _OST_KEYWORDS.sub("", norm_album).strip()
    item_lang = str(item.get("language") or item.get("lang") or "").lower().strip()

    reasons: list[str] = []
    tier = RankingTier.TIER_D
    total_score = 0

    title_tokens = _tokens(norm_title)
    artist_tokens = _tokens(norm_artists)

    # --------------------------------------------------------------------------
    # 1. Text & Exact Intent Matching (Additive 1000-point scale)
    # --------------------------------------------------------------------------
    if kind == "artist":
        if norm_title == clean_q:
            tier = RankingTier.TIER_A
            total_score += 500 + 320 + 180  # = 1000
            reasons.append("exact_artist_match +1000")
        elif norm_title.startswith(clean_q + " ") or norm_title.startswith(clean_q):
            tier = RankingTier.TIER_B
            total_score += 380 + 320 + 150  # = 850
            reasons.append("artist_starts_with_query +850")
        elif any(t == clean_q or t.startswith(clean_q) for t in title_tokens):
            tier = RankingTier.TIER_B
            total_score += 280 + 240 + 230  # = 750
            reasons.append("artist_word_starts_with_query +750")
        elif f" {clean_q} " in f" {norm_title} " or clean_q in norm_title:
            tier = RankingTier.TIER_B
            total_score += 280 + 240 + 80   # = 600
            reasons.append("exact_phrase_in_artist +600")
        elif _is_phonetic_match(clean_q, norm_title):
            tier = RankingTier.TIER_B
            total_score += 170 + 240 + 140  # = 550
            reasons.append("transliteration_match +550")
        elif _is_typo_match(clean_q, norm_title):
            tier = RankingTier.TIER_B
            total_score += 150 + 240 + 110  # = 500
            reasons.append("typo_corrected_match +500")
        else:
            sim = _similarity(clean_q, norm_title)
            score_part = int(sim * 100)
            total_score += score_part
            reasons.append(f"fuzzy_similarity +{score_part}")

    elif kind == "album":
        if norm_title == clean_q or (clean_album and clean_album == clean_q):
            tier = RankingTier.TIER_A
            total_score += 500 + 320 + 130  # = 950
            reasons.append("exact_album_match +950")
        elif norm_title.startswith(clean_q + " ") or (clean_album and clean_album.startswith(clean_q + " ")) or norm_title.startswith(clean_q) or (clean_album and clean_album.startswith(clean_q)):
            tier = RankingTier.TIER_B
            total_score += 420 + 320 + 110  # = 850
            reasons.append("album_starts_with_query +850")
        elif any(t == clean_q or t.startswith(clean_q) for t in title_tokens):
            tier = RankingTier.TIER_B
            total_score += 280 + 240 + 230  # = 750
            reasons.append("album_word_starts_with_query +750")
        elif f" {clean_q} " in f" {norm_title} " or (clean_album and f" {clean_q} " in f" {clean_album} ") or clean_q in norm_title or (clean_album and clean_q in clean_album):
            tier = RankingTier.TIER_B
            total_score += 280 + 240 + 80   # = 600
            reasons.append("exact_phrase_in_album +600")
        elif norm_artists == clean_q:
            tier = RankingTier.TIER_B
            total_score += 380 + 170        # = 550
            reasons.append("album_artist_match +550")
        elif _is_phonetic_match(clean_q, norm_title) or (clean_album and _is_phonetic_match(clean_q, clean_album)):
            tier = RankingTier.TIER_B
            total_score += 170 + 240 + 40   # = 450
            reasons.append("transliteration_match +450")
        elif _is_typo_match(clean_q, norm_title) or (clean_album and _is_typo_match(clean_q, clean_album)):
            tier = RankingTier.TIER_B
            total_score += 150 + 240 + 10   # = 400
            reasons.append("typo_corrected_match +400")
        elif clean_q in norm_artists:
            tier = RankingTier.TIER_C
            total_score += 350
            reasons.append("album_artist_contains_query +350")
        else:
            sim = _similarity(clean_q, norm_title)
            score_part = int(sim * 100)
            total_score += score_part
            reasons.append(f"fuzzy_similarity +{score_part}")

    else:  # Song
        title_hits = _matched_query_tokens(core_tokens, norm_title)
        artist_hits = _matched_query_tokens(core_tokens, norm_artists)
        album_hits = _matched_query_tokens(core_tokens, clean_album)
        all_hits = title_hits | artist_hits | album_hits

        # Complete Title + Artist combined match (e.g. "Believer Imagine Dragons")
        is_complete_combined = (
            len(core_tokens) > 1
            and title_hits
            and artist_hits
            and len(all_hits) == len(set(core_tokens))
        )

        if is_complete_combined:
            tier = RankingTier.TIER_A
            total_score += 1000  # Combined title + artist match is strongest intent
            reasons.append("title_artist_combined_exact +1000")
        elif norm_title == clean_q:
            tier = RankingTier.TIER_A
            total_score += 1000  # Exact title match
            reasons.append("exact_normalized_title +1000")
        elif clean_album and clean_album == clean_q:
            tier = RankingTier.TIER_A
            total_score += 950   # Exact album / soundtrack match
            reasons.append("exact_album_movie +950")
        elif norm_title.startswith(clean_q + " ") or norm_title.startswith(clean_q):
            tier = RankingTier.TIER_B
            total_score += 850
            reasons.append("title_starts_with_query +850")
        elif any(t == clean_q or t.startswith(clean_q) for t in title_tokens):
            tier = RankingTier.TIER_B
            total_score += 750
            reasons.append("title_word_starts_with_query +750")
        elif norm_artists == clean_q:
            tier = RankingTier.TIER_B
            total_score += 700
            reasons.append("exact_artist +700")
        elif f" {clean_q} " in f" {norm_title} " or clean_q in norm_title:
            tier = RankingTier.TIER_B
            total_score += 600
            reasons.append("exact_phrase_inside_title +600")
        elif clean_album and (clean_q in clean_album or f" {clean_q} " in f" {clean_album} "):
            tier = RankingTier.TIER_C
            total_score += 500
            reasons.append("album_movie_contains_query +500")
        elif norm_artists.startswith(clean_q) or any(at.startswith(clean_q) for at in artist_tokens):
            tier = RankingTier.TIER_C
            total_score += 450
            reasons.append("artist_starts_with_query +450")
        elif _is_phonetic_match(clean_q, norm_title):
            tier = RankingTier.TIER_B
            total_score += 380
            reasons.append("transliteration_match +380")
        elif clean_q in norm_artists:
            tier = RankingTier.TIER_C
            total_score += 350
            reasons.append("artist_contains_query +350")
        elif _is_typo_match(clean_q, norm_title):
            tier = RankingTier.TIER_B
            total_score += 320
            reasons.append("typo_corrected_match +320")
        elif len(core_tokens) > 1:
            combined = set(_tokens(f"{norm_title} {norm_artists} {clean_album}"))
            matched = set(core_tokens) & combined
            if len(matched) == len(core_tokens):
                tier = RankingTier.TIER_B
                total_score += 580
                reasons.append("all_query_words_present +580")
            elif len(matched) > 0:
                tier = RankingTier.TIER_C
                ratio = len(matched) / len(core_tokens)
                part = int(200 + ratio * 250)
                total_score += part
                reasons.append(f"multi_field_partial +{part}")
            else:
                sim = _similarity(clean_q, norm_title)
                score_part = int(sim * 100)
                total_score += score_part
                reasons.append(f"weak_fuzzy +{score_part}")
        else:
            sim = _similarity(clean_q, norm_title)
            score_part = int(sim * 100)
            total_score += score_part
            reasons.append(f"weak_fuzzy +{score_part}")

    # --------------------------------------------------------------------------
    # 2. Language Inference & Relevance
    # --------------------------------------------------------------------------
    if detected_lang and item_lang:
        if detected_lang in item_lang or item_lang in detected_lang:
            total_score += 80
            reasons.append(f"query_language_match({detected_lang}) +80")
        else:
            total_score -= 80
            reasons.append(f"query_language_mismatch({detected_lang} vs {item_lang}) -80")

    # --------------------------------------------------------------------------
    # 3. Personalization & History Signals (Capped at <= 100 points, max 10%)
    # --------------------------------------------------------------------------
    personalization_pts = 0
    # Preferred language (+80) — only applied as tie-breaker when explicit intent didn't contradict
    if user_languages and item_lang and not detected_lang:
        if any(l.lower() in item_lang or item_lang in l.lower() for l in user_languages):
            personalization_pts += 40
            reasons.append("user_preferred_language +40")

    # Preferred artist (+60)
    if user_artists and norm_artists:
        if any(normalize_query(a) in norm_artists or norm_artists in normalize_query(a) for a in user_artists):
            personalization_pts += 30
            reasons.append("user_preferred_artist +30")

    # Listening history relevance (max +20)
    item_id = str(item.get("id") or item.get("track_id") or item.get("seokey") or "")
    if history_tracks and item_id:
        if any(str(h.get("id") or h.get("track_id") or h.get("seokey") or "") == item_id for h in history_tracks):
            personalization_pts += 20
            reasons.append("listening_history +20")

    # Previous search relevance (max +10)
    if previous_searches and clean_q:
        if any(normalize_query(p) == clean_q for p in previous_searches):
            personalization_pts += 10
            reasons.append("previous_search +10")

    # Hard cap on personalization: max 100 points
    capped_personalization = min(personalization_pts, 100)
    total_score += capped_personalization

    # --------------------------------------------------------------------------
    # 4. Secondary Signals: Popularity (+40) & Recency (+30)
    # --------------------------------------------------------------------------
    popularity = item.get("popularity_score") or item.get("popularity") or 0
    try:
        pop_pts = min(int(float(popularity) * 40.0), 40)
        if pop_pts > 0:
            total_score += pop_pts
            reasons.append(f"popularity +{pop_pts}")
    except (TypeError, ValueError):
        pass

    # Recent release (+30)
    year = item.get("year") or item.get("release_date")
    if year:
        try:
            yr = int(str(year)[:4])
            if yr >= 2024:
                total_score += 30
                reasons.append("recent_release +30")
            elif yr >= 2020:
                total_score += 15
                reasons.append("semi_recent_release +15")
        except (TypeError, ValueError):
            pass

    # Official Record Label Boost (+25) as secondary authenticity signal
    label = str(item.get("label") or "").strip()
    is_official = bool(item.get("official_label_verified")) or is_verified_label(label)
    if is_official:
        total_score += 25
        reasons.append("verified_official_label +25")

    # Metadata Confidence (+15)
    meta_conf = item.get("metadata_confidence")
    if meta_conf is not None and float(meta_conf) >= 0.90:
        total_score += 15
        reasons.append("metadata_confidence +15")

    # --------------------------------------------------------------------------
    # 5. Unofficial Noise Penalty (-400)
    # --------------------------------------------------------------------------
    if not _UNOFFICIAL_NOISE.search(clean_q):
        if _UNOFFICIAL_NOISE.search(norm_title) or _UNOFFICIAL_NOISE.search(norm_album):
            total_score -= 400
            reasons.append("unofficial_noise_penalty -400")

    final_score = max(total_score, 0)

    logger.debug(
        "search_ranking query=%r id=%s title=%r tier=%s score=%d reasons=%s",
        query, item.get("id"), title, tier.name, final_score, "; ".join(reasons),
    )

    return tier, final_score, reasons


def score(
    query: str,
    item: dict[str, Any],
    kind: str,
    user_languages: list[str] | None = None,
    user_artists: list[str] | None = None,
    history_tracks: list[dict[str, Any]] | None = None,
    previous_searches: list[str] | None = None,
) -> float:
    """Returns the numeric search score (out of 1000) for an item."""
    _, num_score, _ = score_item(
        query,
        item,
        kind,
        user_languages=user_languages,
        user_artists=user_artists,
        history_tracks=history_tracks,
        previous_searches=previous_searches,
    )
    return float(num_score)


def confidence(query: str, item: dict[str, Any], kind: str) -> float:
    """Calculates search confidence score (0.0 to 1.0).

    0.90-1.00: Very strong exact intent
    0.75-0.89: Strong match
    0.50-0.74: Probable match
    < 0.50: Weak / related match
    """
    _, s, _ = score_item(query, item, kind)
    return min(max(s / 1000.0, 0.0), 1.0)


def _strong_match(query: str, item: dict[str, Any], kind: str) -> bool:
    """Return true only for a real word/prefix match, not a distant fuzzy match."""
    q = normalize_query(query)
    q_tokens = _tokens(q)
    if not q_tokens:
        return False
    title, artists, album_val = _text(item, kind)
    fields = [title] if kind == "artist" else [title, artists, album_val]
    for value in fields:
        normalized = normalize_query(value)
        tokens = _tokens(normalized)
        if (
            normalized == q
            or normalized.startswith(q)
            or any(token.startswith(q) for token in tokens)
            or q in tokens
            or q in normalized
            or _is_phonetic_match(q, normalized)
            or _is_typo_match(q, normalized)
        ):
            return True
        if len(q_tokens) > 1 and all(
            any(token == wanted or token.startswith(wanted) for token in tokens)
            for wanted in q_tokens
        ):
            return True
    if len(q_tokens) > 1:
        combined_tokens = _tokens(f"{title} {artists} {album_val}")
        if all(
            any(token == wanted or token.startswith(wanted) for token in combined_tokens)
            for wanted in q_tokens
        ):
            return True
    return False


def _word_match(query: str, item: dict[str, Any], kind: str) -> bool:
    q = normalize_query(query)
    q_tokens = _tokens(q)
    title, artists, album_val = _text(item, kind)
    fields = [title] if kind == "artist" else [title, artists, album_val]
    for value in fields:
        normalized = normalize_query(value)
        tokens = _tokens(normalized)
        if (
            normalized == q
            or normalized.startswith(q)
            or any(token == q or token.startswith(q) for token in tokens)
            or q in tokens
        ):
            return True
    if len(q_tokens) > 1:
        combined_tokens = _tokens(f"{title} {artists} {album_val}")
        if all(
            any(token == wanted or token.startswith(wanted) for token in combined_tokens)
            for wanted in q_tokens
        ):
            return True
    return False


def _semantic_fingerprint(item: dict[str, Any], kind: str) -> str:
    title, artists, album_val = _text(item, kind)
    norm_t = normalize_query(title)
    primary_art = normalize_query(artists.split(",")[0] if artists else "")
    norm_al = normalize_query(album_val)
    dur = item.get("duration") or item.get("duration_seconds") or 0
    try:
        dur_bucket = int(float(dur)) // 8
    except (TypeError, ValueError):
        dur_bucket = 0
    return f"{norm_t}::{primary_art}::{norm_al}::{dur_bucket}"


def rank(
    query: str,
    values: list[dict[str, Any]],
    kind: str,
    limit: int,
    user_languages: list[str] | None = None,
    user_artists: list[str] | None = None,
    history_tracks: list[dict[str, Any]] | None = None,
    previous_searches: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Deterministic ranking pipeline:
    1. Filter out broken / missing titles
    2. Canonical deduplication
    3. Score each candidate and assign to RankingTier (A, B, C, D)
    4. Suppress weak results (Tier D / score < 50) when strong Tier A / B matches exist
    5. Deterministically sort by: (tier, -score, starts_with_penalty, title_len, index)
    6. Return top subset up to limit
    """
    seen_ids: set[str] = set()
    fingerprint_map: dict[str, tuple[RankingTier, int, int, dict[str, Any]]] = {}

    for index, item in enumerate(values):
        # Filter broken content: missing title or missing ID
        title = str(item.get("title") or item.get("name") or "").strip()
        item_id = str(item.get("id") or item.get("track_id") or item.get("seokey") or "").strip()
        if not title:
            continue
        if item_id and item_id in seen_ids:
            continue
        if item_id:
            seen_ids.add(item_id)

        ranked_item = dict(item)
        item_tier, item_score, _ = score_item(
            query,
            ranked_item,
            kind,
            user_languages=user_languages,
            user_artists=user_artists,
            history_tracks=history_tracks,
            previous_searches=previous_searches,
        )
        ranked_item["_search_score"] = item_score
        ranked_item["_search_tier"] = item_tier.value

        fp = _semantic_fingerprint(ranked_item, kind)
        if fp in fingerprint_map:
            prev_tier, prev_score, prev_idx, prev_item = fingerprint_map[fp]
            if item_tier < prev_tier or (item_tier == prev_tier and item_score > prev_score):
                fingerprint_map[fp] = (item_tier, item_score, index, ranked_item)
        else:
            fingerprint_map[fp] = (item_tier, item_score, index, ranked_item)

    ranked = list(fingerprint_map.values())
    if not ranked:
        return []

    # Progressive zero-result fallback / candidate filtering:
    # Pass 1 & 2: Exact word / prefix matches
    word_matches = [item for item in ranked if _word_match(query, item[3], kind)]
    if word_matches:
        ranked = word_matches
    else:
        # Pass 3 & 4: Strong transliteration / typo / fuzzy matches
        strong = [item for item in ranked if _strong_match(query, item[3], kind)]
        if strong:
            ranked = strong

    q_norm = normalize_query(query)
    q_tokens = _tokens(query)

    # When kind is song and exact recording identities exist (exact title, exact album, or complete combined),
    # prioritize exact title recordings and matching soundtrack tracks while suppressing unrelated broad matches
    if kind == "song":
        exact_title_or_album_recordings = [
            item for item in ranked
            if (
                normalize_query(str(item[3].get("title") or item[3].get("name") or "")) == q_norm
                or _OST_KEYWORDS.sub("", normalize_query(str(item[3].get("album") or ""))).strip() == q_norm
                or (
                    len(q_tokens) > 1
                    and _matched_query_tokens(q_tokens, normalize_query(str(item[3].get("title") or "")))
                    and _matched_query_tokens(q_tokens, normalize_query(str(item[3].get("artists") or "")))
                    and len(_matched_query_tokens(q_tokens, normalize_query(str(item[3].get("title") or ""))) | _matched_query_tokens(q_tokens, normalize_query(str(item[3].get("artists") or "")))) == len(set(q_tokens))
                )
            )
        ]
        if exact_title_or_album_recordings:
            ranked = [
                item for item in ranked
                if (
                    item in exact_title_or_album_recordings
                    or _OST_KEYWORDS.sub("", normalize_query(str(item[3].get("album") or ""))).strip() == q_norm
                )
                and not _UNOFFICIAL_NOISE.search(str(item[3].get("title") or ""))
            ]

    # Multi-tier deterministic sort:
    # 1. Tier ascending (Tier A = 1, Tier B = 2, Tier C = 3, Tier D = 4)
    # 2. Score descending (-score)
    # 3. Starts with query (True before False)
    # 4. Length of title ascending (shorter more exact title wins)
    # 5. Original index for stability
    ranked.sort(
        key=lambda v: (
            v[0].value,
            -v[1],
            not normalize_query(str(v[3].get("title") or v[3].get("name") or "")).startswith(q_norm),
            len(str(v[3].get("title") or v[3].get("name") or "")),
            v[2],
        )
    )

    return [{k: val for k, val in item.items() if not k.startswith("_search_")} for _, _, _, item in ranked[:limit]]
