"""In-memory search vocabulary for typeahead suggestions and typo correction."""
from __future__ import annotations

from typing import Any

from api.catalog.search.ranking import normalize_query

try:
    from rapidfuzz import fuzz, process
    from rapidfuzz.distance import DamerauLevenshtein
except ImportError:  # pragma: no cover - exercised only without the optional wheel
    fuzz = process = DamerauLevenshtein = None


class SearchVocabulary:
    def __init__(self, max_terms: int = 20000):
        self.max_terms = max_terms
        self._terms: dict[str, str] = {}

    def __len__(self) -> int:
        return len(self._terms)

    def add(self, text: Any) -> None:
        display = " ".join(str(text or "").split())
        key = normalize_query(display)
        if len(key) < 3 or key.isdigit() or key.startswith("{"):
            return
        if key in self._terms:
            return
        if len(self._terms) >= self.max_terms:
            oldest = next(iter(self._terms))
            del self._terms[oldest]
        self._terms[key] = display

    def observe(self, values: list[dict[str, Any]], kind: str | None = None) -> None:
        for item in values or []:
            if not isinstance(item, dict):
                continue
            self.add(item.get("title") or item.get("name"))
            for artist in item.get("artists") or []:
                if isinstance(artist, dict):
                    self.add(artist.get("name"))
            raw_artist = item.get("artist")
            if isinstance(raw_artist, dict):
                self.add(raw_artist.get("name"))
            elif isinstance(raw_artist, str):
                for part in raw_artist.split(","):
                    self.add(part)
            album = item.get("album")
            if isinstance(album, dict):
                self.add(album.get("title") or album.get("name"))
            elif isinstance(album, str):
                self.add(album)

    def suggest(self, query: str, limit: int = 8) -> list[str]:
        key = normalize_query(query)
        if not key or not self._terms:
            return []
        seen: list[str] = []
        for term, display in self._terms.items():
            if term.startswith(key) or any(tok.startswith(key) for tok in term.split()):
                seen.append(display)
                if len(seen) >= limit:
                    return seen
        if process is None or len(key) < 3:
            return seen
        matches = process.extract(key, list(self._terms), scorer=fuzz.WRatio, limit=limit * 2, score_cutoff=72)
        for term, _score, _index in matches:
            display = self._terms[term]
            if display not in seen:
                seen.append(display)
            if len(seen) >= limit:
                break
        return seen

    def correct(self, query: str) -> str | None:
        """Best spelling correction for a query that produced (almost) no results."""
        key = normalize_query(query)
        if process is None or len(key) < 4 or not self._terms or key in self._terms:
            return None
        best = process.extractOne(key, list(self._terms), scorer=fuzz.WRatio, score_cutoff=82)
        if not best:
            return None
        term, _score, _index = best
        display = self._terms[term]
        if term == key:
            return None
        edit_sim = DamerauLevenshtein.normalized_similarity(key, term)
        token_sim = fuzz.token_set_ratio(key, term) / 100.0
        if edit_sim < 0.72 and token_sim < 0.9:
            return None
        return display
