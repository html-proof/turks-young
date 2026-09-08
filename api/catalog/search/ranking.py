from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

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

_KNOWN_OFFICIAL_LABELS = {
    "sony music", "t-series", "saregama", "universal music", "warner music",
    "zee music", "lahari music", "think music", "muzik247", "satyam audios",
    "manorama music", "v cinemas", "star music", "aditya music", "rafa international",
    "speed records", "yrf music", "tips", "speed audio", "east coast",
    "millennium audios", "dvocean", "audio video media", "v cinemas international",
    "speed audio & video", "surya audio", "mango music", "madhu audio",
}


def normalize_query(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("’", "'").replace("‘", "'")
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
    album = item.get("album")
    album_text = ""
    if isinstance(album, dict):
        album_text = str(album.get("title") or album.get("name") or "")
    elif isinstance(album, str):
        album_text = album
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
    replacements = [
        ("zh", "l"),
        ("th", "t"),
        ("ee", "i"),
        ("oo", "u"),
        ("v", "w"),
        ("dh", "d"),
        ("sh", "s"),
        ("aa", "a"),
    ]
    for old, new in replacements:
        t = t.replace(old, new)
    return t


def _is_phonetic_match(query: str, target: str) -> bool:
    pq = _phonetic_normalize(query)
    pt = _phonetic_normalize(target)
    if not pq or not pt:
        return False
    return pq == pt or pt.startswith(pq) or any(tok.startswith(pq) for tok in pt.split())


def _is_typo_match(query: str, target: str) -> bool:
    q = normalize_query(query)
    t = normalize_query(target)
    if len(q) < 3 or not t:
        return False
    max_dist = 1 if len(q) <= 5 else 2
    if _levenshtein(q, t) <= max_dist:
        return True
    return any(_levenshtein(q, tok) <= max_dist for tok in t.split() if len(tok) >= 3)


def _similarity(query: str, value: str) -> float:
    if not value:
        return 0.0
    q_tokens, v_tokens = _tokens(query), _tokens(value)
    whole = SequenceMatcher(None, query, normalize_query(value)).ratio()
    token = sum(max(SequenceMatcher(None, token, candidate).ratio() for candidate in v_tokens) for token in q_tokens)
    return max(whole, token / len(q_tokens) if q_tokens else 0.0)


def _semantic_fingerprint(item: dict[str, Any], kind: str) -> str:
    title, artists, album = _text(item, kind)
    norm_t = normalize_query(title)
    primary_art = normalize_query(artists.split(",")[0] if artists else "")
    norm_al = normalize_query(album)
    dur = item.get("duration") or item.get("duration_seconds") or 0
    try:
        dur_bucket = int(float(dur)) // 8
    except (TypeError, ValueError):
        dur_bucket = 0
    return f"{norm_t}::{primary_art}::{norm_al}::{dur_bucket}"


def score(query: str, item: dict[str, Any], kind: str) -> float:
    q = normalize_query(query)
    if not q:
        return 0.0

    q_tokens = _tokens(q)
    detected_lang = None
    core_tokens = []
    for token in q_tokens:
        if token in KNOWN_LANGUAGES and not detected_lang:
            detected_lang = token
        else:
            core_tokens.append(token)

    clean_q = " ".join(core_tokens) if core_tokens else q

    title, artists, album = _text(item, kind)
    norm_title = normalize_query(title)
    norm_artists = normalize_query(artists)
    norm_album = normalize_query(album)
    clean_album = _OST_KEYWORDS.sub("", norm_album).strip()

    item_lang = str(item.get("language") or item.get("lang") or "").lower().strip()

    score_val = 0.0

    if kind == "artist":
        if norm_title == clean_q:
            score_val = 1000.0
        elif norm_title.startswith(clean_q):
            score_val = 850.0
        elif any(t.startswith(clean_q) for t in _tokens(norm_title)):
            score_val = 750.0
        elif clean_q in norm_title:
            score_val = 600.0
        elif _is_phonetic_match(clean_q, norm_title):
            score_val = 550.0
        elif _is_typo_match(clean_q, norm_title):
            score_val = 500.0
        else:
            score_val = 100.0 * _similarity(clean_q, norm_title)
    elif kind == "album":
        if norm_title == clean_q or (clean_album and clean_album == clean_q):
            score_val = 950.0
        elif norm_title.startswith(clean_q) or (clean_album and clean_album.startswith(clean_q)):
            score_val = 850.0
        elif any(t.startswith(clean_q) for t in _tokens(norm_title)):
            score_val = 750.0
        elif clean_q in norm_title:
            score_val = 600.0
        elif norm_artists == clean_q:
            score_val = 550.0
        elif clean_q in norm_artists:
            score_val = 400.0
        elif _is_phonetic_match(clean_q, norm_title):
            score_val = 450.0
        elif _is_typo_match(clean_q, norm_title):
            score_val = 400.0
        else:
            score_val = 100.0 * _similarity(clean_q, norm_title)
    else:  # song
        if norm_title == clean_q:
            score_val = 1000.0
        elif clean_album and clean_album == clean_q:
            score_val = 950.0
        elif norm_title.startswith(clean_q):
            score_val = 850.0
        elif any(t.startswith(clean_q) for t in _tokens(norm_title)):
            score_val = 750.0
        elif norm_artists == clean_q:
            score_val = 700.0
        elif clean_q in norm_title:
            score_val = 600.0
        elif clean_album and clean_q in clean_album:
            score_val = 500.0
        elif norm_artists.startswith(clean_q):
            score_val = 450.0
        elif clean_q in norm_artists:
            score_val = 350.0
        elif _is_phonetic_match(clean_q, norm_title):
            score_val = 380.0
        elif _is_typo_match(clean_q, norm_title):
            score_val = 320.0
        else:
            if len(core_tokens) > 1:
                combined = set(_tokens(f"{norm_title} {norm_artists} {norm_album}"))
                matched = set(core_tokens) & combined
                if len(matched) == len(core_tokens):
                    score_val = 580.0
                elif len(matched) > 0:
                    score_val = 200.0 + (len(matched) / len(core_tokens)) * 250.0
            if score_val == 0.0:
                score_val = 100.0 * _similarity(clean_q, norm_title)

    # Language intent bonus (+250)
    if detected_lang and item_lang:
        if detected_lang in item_lang or item_lang in detected_lang:
            score_val += 250.0
        else:
            score_val -= 100.0

    # Official Record Label Boost (+20)
    label = str(item.get("label") or "").lower().strip()
    if any(known in label for known in _KNOWN_OFFICIAL_LABELS):
        score_val += 20.0

    # Unofficial / Noise penalty (-400)
    if not _UNOFFICIAL_NOISE.search(clean_q):
        if _UNOFFICIAL_NOISE.search(norm_title) or _UNOFFICIAL_NOISE.search(norm_album):
            score_val -= 400.0

    # Popularity is strictly subordinate: max +30
    popularity = item.get("popularity_score") or item.get("popularity") or 0
    try:
        score_val += min(float(popularity), 1.0) * 30.0
    except (TypeError, ValueError):
        pass

    return max(score_val, 0.0)


def _strong_match(query: str, item: dict[str, Any], kind: str) -> bool:
    """Return true only for a real word/prefix match, not a distant fuzzy match."""
    q = normalize_query(query)
    q_tokens = _tokens(q)
    if not q_tokens:
        return False
    title, artists, album = _text(item, kind)
    fields = [title] if kind == "artist" else [title, artists, album]
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
        combined_tokens = _tokens(f"{title} {artists} {album}")
        if all(
            any(token == wanted or token.startswith(wanted) for token in combined_tokens)
            for wanted in q_tokens
        ):
            return True
    return False


def _word_match(query: str, item: dict[str, Any], kind: str) -> bool:
    q = normalize_query(query)
    title, artists, album = _text(item, kind)
    fields = [title] if kind == "artist" else [title, artists, album]
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
    return False


def rank(query: str, values: list[dict[str, Any]], kind: str, limit: int) -> list[dict[str, Any]]:
    seen_ids: set[str] = set()
    fingerprint_map: dict[str, tuple[float, int, dict[str, Any]]] = {}

    for index, item in enumerate(values):
        item_id = str(item.get("id") or "")
        if item_id and item_id in seen_ids:
            continue
        if item_id:
            seen_ids.add(item_id)

        ranked_item = dict(item)
        item_score = score(query, ranked_item, kind)
        ranked_item["_search_score"] = item_score

        fp = _semantic_fingerprint(ranked_item, kind)
        if fp in fingerprint_map:
            prev_score, prev_idx, prev_item = fingerprint_map[fp]
            if item_score > prev_score:
                fingerprint_map[fp] = (item_score, index, ranked_item)
        else:
            fingerprint_map[fp] = (item_score, index, ranked_item)

    ranked = list(fingerprint_map.values())
    word_matches = [item for item in ranked if _word_match(query, item[2], kind)]
    if word_matches:
        ranked = word_matches
    else:
        strong = [item for item in ranked if _strong_match(query, item[2], kind)]
        if strong:
            ranked = strong

    q_norm = normalize_query(query)
    ranked.sort(
        key=lambda v: (
            -v[0],
            not normalize_query(str(v[2].get("title") or v[2].get("name") or "")).startswith(q_norm),
            len(str(v[2].get("title") or v[2].get("name") or "")),
            v[1],
        )
    )
    return [{k: val for k, val in item.items() if k != "_search_score"} for _, _, item in ranked[:limit]]


def confidence(query: str, item: dict[str, Any], kind: str) -> float:
    return min(score(query, item, kind) / 1000.0, 1.0)
