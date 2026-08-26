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
        return str(item.get("name") or ""), "", ""
    title = str(item.get("title") or item.get("name") or "")
    artists = item.get("artists") or []
    artist_text = ", ".join(str(a.get("name") or "") for a in artists if isinstance(a, dict))
    if not artist_text and isinstance(item.get("artist"), dict):
        artist_text = str(item["artist"].get("name") or "")
    album = item.get("album") if isinstance(item.get("album"), dict) else {}
    return title, artist_text, str(album.get("name") or "")


def _similarity(query: str, value: str) -> float:
    if not value:
        return 0.0
    q_tokens, v_tokens = _tokens(query), _tokens(value)
    whole = SequenceMatcher(None, query, normalize_query(value)).ratio()
    token = sum(max(SequenceMatcher(None, token, candidate).ratio() for candidate in v_tokens) for token in q_tokens)
    return max(whole, token / len(q_tokens) if q_tokens else 0.0)


def score(query: str, item: dict[str, Any], kind: str) -> float:
    q = normalize_query(query)
    title, artists, album = _text(item, kind)
    fields = [(title, 100.0), (artists, 75.0), (album, 55.0)]
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
            best = max(best, weight * 0.80)
        elif all(token in _tokens(normalized) for token in _tokens(q)):
            best = max(best, weight * 0.65)
        else:
            # Fuzzy matching is intentionally gated by the endpoint's candidate
            # retrieval; once a provider returns a near match, retain enough
            # signal for common misspellings such as "arjit sing".
            best = max(best, weight * 0.75 * _similarity(q, normalized))
    query_tokens = set(_tokens(q))
    if len(query_tokens) > 1 and query_tokens & set(_tokens(title)) and query_tokens & set(_tokens(artists)):
        best += 25.0
    popularity = item.get("popularity_score") or item.get("popularity") or 0
    try:
        best += min(float(popularity), 1.0) * 12.0
    except (TypeError, ValueError):
        pass
    return best


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
    ranked.sort(key=lambda value: (-value[0], value[1]))
    return [{key: value for key, value in item.items() if key != "_search_score"} for _, _, item in ranked[:limit]]


def confidence(query: str, item: dict[str, Any], kind: str) -> float:
    return min(score(query, item, kind) / 125.0, 1.0)
