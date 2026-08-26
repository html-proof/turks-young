from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_SEPARATORS = re.compile(r"\b(?:feat\.?|ft\.?|featuring|with)\b|\s+-\s+", re.I)


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


def _similarity(query: str, value: str) -> float:
    if not value:
        return 0.0
    q_tokens, v_tokens = _tokens(query), _tokens(value)
    whole = SequenceMatcher(None, query, normalize_query(value)).ratio()
    token = sum(max(SequenceMatcher(None, token, candidate).ratio() for candidate in v_tokens) for token in q_tokens)
    return max(whole, token / len(q_tokens) if q_tokens else 0.0)


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


def score(query: str, item: dict[str, Any], kind: str) -> float:
    q = normalize_query(query)
    query_tokens = set(_tokens(q))
    title, artists, album = _text(item, kind)
    fields = [(title, 100.0), (artists, 75.0), (album, 65.0)]
    if kind == "artist":
        fields = [(title, 110.0)]
    best = 0.0
    for value, weight in fields:
        normalized = normalize_query(value)
        if not normalized:
            continue
        if normalized == q:
            best = max(best, weight)
        elif normalized.startswith(q):
            best = max(best, weight * 0.85)
        elif all(token in _tokens(normalized) for token in _tokens(q)):
            best = max(best, weight * 0.75)
        else:
            best = max(best, weight * 0.70 * _similarity(q, normalized))

    combined_tokens = set(_tokens(f"{title} {artists} {album}"))
    if len(query_tokens) > 1:
        matched_tokens = query_tokens & combined_tokens
        if len(matched_tokens) == len(query_tokens):
            best = max(best, 95.0)
        elif len(matched_tokens) > 1:
            best += (len(matched_tokens) / len(query_tokens)) * 30.0
        if query_tokens & set(_tokens(title)) and query_tokens & set(_tokens(artists)):
            best += 25.0

    # 1. Soundtrack / Album match boost (e.g. searching "classmates", "pattalam", "operation java", "jilla")
    norm_album = normalize_query(album)
    clean_album = _OST_KEYWORDS.sub("", norm_album).strip()
    if clean_album and (clean_album == q or clean_album.startswith(q)):
        best = max(best, 95.0)
        if _OST_KEYWORDS.search(norm_album) or _OST_KEYWORDS.search(title):
            best += 10.0

    # 2. Official Record Label Boost
    label = str(item.get("label") or "").lower().strip()
    if any(known in label for known in _KNOWN_OFFICIAL_LABELS):
        best += 12.0

    # 3. Unofficial / Noise penalty (karaoke, bedroom covers, slowed reverb, ringtones, status)
    if not _UNOFFICIAL_NOISE.search(q):
        if _UNOFFICIAL_NOISE.search(title) or _UNOFFICIAL_NOISE.search(norm_album):
            best -= 45.0

    popularity = item.get("popularity_score") or item.get("popularity") or 0
    try:
        best += min(float(popularity), 1.0) * 10.0
    except (TypeError, ValueError):
        pass
    return max(best, 0.0)


def _strong_match(query: str, item: dict[str, Any], kind: str) -> bool:
    """Return true only for a real word/prefix match, not a fuzzy neighbour."""
    q = normalize_query(query)
    query_tokens = _tokens(q)
    if not query_tokens:
        return False
    title, artists, album = _text(item, kind)
    fields = [title] if kind == "artist" else [title, artists, album]
    for value in fields:
        normalized = normalize_query(value)
        tokens = _tokens(normalized)
        if normalized == q or normalized.startswith(q) or q in tokens:
            return True
        # Multi-word searches may match across a title and its artist credit,
        # but every query word must still be present as a complete/prefix word.
        if len(query_tokens) > 1 and all(
            any(token == wanted or token.startswith(wanted) for token in tokens)
            for wanted in query_tokens
        ):
            return True
    # Cross-field token matching (e.g. searching "Believer Imagine Dragons" or "Tum Hi Ho Arijit")
    if len(query_tokens) > 1:
        combined_tokens = _tokens(f"{title} {artists} {album}")
        if all(
            any(token == wanted or token.startswith(wanted) for token in combined_tokens)
            for wanted in query_tokens
        ):
            return True
    return False


def rank(query: str, values: list[dict[str, Any]], kind: str, limit: int) -> list[dict[str, Any]]:
    ranked: list[tuple[float, int, dict[str, Any]]] = []
    seen: set[str] = set()
    for index, item in enumerate(values):
        item_id = str(item.get("id") or "")
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        ranked_item = dict(item)
        ranked_item["_search_score"] = score(query, ranked_item, kind)
        ranked.append((ranked_item["_search_score"], index, ranked_item))
    # Provider search is intentionally broad. If it gives us any genuine
    # title/artist/album word matches, do not let typo-neighbours such as
    # "pattanam" outrank or clutter results for "pattalam".
    strong = [item for item in ranked if _strong_match(query, item[2], kind)]
    if strong:
        ranked = strong
    ranked.sort(key=lambda value: (-value[0], value[1]))
    return [{key: value for key, value in item.items() if key != "_search_score"} for _, _, item in ranked[:limit]]


def confidence(query: str, item: dict[str, Any], kind: str) -> float:
    return min(score(query, item, kind) / 125.0, 1.0)
