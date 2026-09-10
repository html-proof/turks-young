from __future__ import annotations

import dataclasses
import re
from typing import Any

from api.catalog.search.ranking import KNOWN_LANGUAGES, LANGUAGE_CONNECTORS, normalize_query

# Filter keywords supported in queries (e.g. "artist:Anirudh", "album:Premam", "year:2015", "year:1990-1999")
FILTER_KEYS = {
    "artist",
    "album",
    "year",
    "genre",
    "language",
    "song",
    "movie",
    "composer",
    "singer",
    "mood",
}

# Regex for explicit filters: key:"value with spaces" or key:value (multi-word up to next filter)
_FILTER_REGEX = re.compile(
    r'(?:^|\s)(artist|album|year|genre|language|song|movie|composer|singer|mood):(?:"([^"]+)"|([^:]+?))(?=\s*(?:artist|album|year|genre|language|song|movie|composer|singer|mood):|$)',
    re.IGNORECASE,
)

# Quoted phrase regex: "phrase here"
_QUOTED_PHRASE_REGEX = re.compile(r'"([^"]+)"')

# Era regex: "90s", "80s", "70s", "2000s", "2010s", "2020s"
_ERA_REGEX = re.compile(r"\b(?:(19[5-9]0|20[0-2]0)s|([5-9]0)s)\b", re.IGNORECASE)

# Common genre keywords for Indian & international music
KNOWN_GENRES = {
    "melody",
    "romantic",
    "romance",
    "rock",
    "pop",
    "hip hop",
    "hip-hop",
    "folk",
    "classical",
    "carnatic",
    "hindustani",
    "devotional",
    "party",
    "dance",
    "acoustic",
    "electronic",
    "edm",
    "jazz",
    "blues",
    "lofi",
    "lo-fi",
    "sad",
    "chill",
    "workout",
    "duet",
    "qawwali",
    "ghazal",
    "mass",
    "kuthu",
}

# Common mood keywords
KNOWN_MOODS = {
    "sad",
    "happy",
    "chill",
    "relax",
    "romantic",
    "party",
    "energetic",
    "workout",
    "sleep",
    "focus",
    "meditation",
    "nostalgic",
    "motivational",
    "feel good",
}

# Common stop words for intent and lyric word counting
STOP_WORDS = {
    "the", "a", "an", "and", "or", "in", "on", "at", "by", "for", "with",
    "about", "against", "between", "into", "through", "during", "before",
    "after", "above", "below", "to", "from", "up", "down", "of", "off",
    "over", "under", "again", "further", "then", "once", "here", "there",
    "when", "where", "why", "how", "all", "any", "both", "each", "few",
    "more", "most", "other", "some", "such", "no", "nor", "not", "only",
    "own", "same", "so", "than", "too", "very", "s", "t", "can", "will",
    "just", "don", "should", "now", "songs", "song", "track", "tracks",
    "music", "audio", "mp3", "hits", "collection",
}


@dataclasses.dataclass(slots=True)
class ParsedSearchQuery:
    """Structured representation of a parsed search query."""
    raw_query: str
    normalized_query: str
    free_text: str
    entity_intent: str  # SONG, ARTIST, ALBUM, PLAYLIST, COMBINED, GENERAL
    filters: dict[str, Any]
    year: int | None = None
    year_range: tuple[int, int] | None = None
    artist: str | None = None
    album: str | None = None
    song: str | None = None
    movie: str | None = None
    genre: str | None = None
    language: str | None = None
    mood: str | None = None
    composer: str | None = None
    singer: str | None = None
    lyric_candidate: bool = False
    is_exact_phrase: bool = False
    quoted_phrase: str | None = None


class AdvancedQueryParser:
    """Central deterministic query & filter parser for search intent."""

    @classmethod
    def parse(cls, raw_query: str) -> ParsedSearchQuery:
        raw = raw_query.strip()
        norm = normalize_query(raw)
        if not norm:
            return ParsedSearchQuery(
                raw_query="",
                normalized_query="",
                free_text="",
                entity_intent="GENERAL",
                filters={},
            )

        filters: dict[str, Any] = {}
        working_text = raw

        # 1. Extract explicit filters (e.g. artist:Rahman, album:"Premam", year:1990-1999)
        def _filter_repl(match: re.Match) -> str:
            key = match.group(1).lower()
            val = (match.group(2) or match.group(3) or "").strip()
            if key in FILTER_KEYS and val:
                filters[key] = val
            return " "

        cleaned_text = _FILTER_REGEX.sub(_filter_repl, working_text).strip()

        # 2. Extract quoted phrases (e.g. "nee kavithaigala")
        quoted_match = _QUOTED_PHRASE_REGEX.search(cleaned_text)
        is_exact_phrase = False
        quoted_phrase = None
        if quoted_match:
            is_exact_phrase = True
            quoted_phrase = quoted_match.group(1).strip()
            # Replace quotes with inner phrase in free text
            cleaned_text = _QUOTED_PHRASE_REGEX.sub(r"\1", cleaned_text).strip()

        # 3. Process year or year range from filters or natural language
        year: int | None = None
        year_range: tuple[int, int] | None = None

        raw_year = filters.get("year")
        if raw_year:
            if "-" in raw_year:
                parts = raw_year.split("-")
                try:
                    y_start, y_end = int(parts[0].strip()), int(parts[1].strip())
                    year_range = (min(y_start, y_end), max(y_start, y_end))
                except ValueError:
                    pass
            else:
                try:
                    year = int(raw_year)
                except ValueError:
                    pass

        # 4. Check for natural language era in cleaned text (e.g. "90s Tamil songs")
        era_match = _ERA_REGEX.search(cleaned_text)
        if era_match and not year and not year_range:
            full_yr = era_match.group(1)
            short_yr = era_match.group(2)
            if full_yr:
                start_year = int(full_yr)
            elif short_yr:
                start_year = 1900 + int(short_yr)
            else:
                start_year = 1990
            year_range = (start_year, start_year + 9)
            cleaned_text = _ERA_REGEX.sub(" ", cleaned_text).strip()

        # 5. Extract language from filters or natural language free text
        detected_language: str | None = filters.get("language")
        if detected_language:
            detected_language = detected_language.lower().strip()
        else:
            tokens = cleaned_text.split()
            remaining_tokens = []
            for token in tokens:
                clean_tok = re.sub(r"[^\w]", "", token.lower())
                if clean_tok in KNOWN_LANGUAGES and not detected_language:
                    detected_language = clean_tok
                else:
                    remaining_tokens.append(token)
            cleaned_text = " ".join(remaining_tokens).strip()

        # 6. Extract genre & mood from filters or natural language free text
        detected_genre: str | None = filters.get("genre")
        if detected_genre:
            detected_genre = detected_genre.lower().strip()
        else:
            norm_lower = f" {cleaned_text.lower()} "
            for g in sorted(KNOWN_GENRES, key=len, reverse=True):
                if f" {g} " in norm_lower:
                    detected_genre = g
                    cleaned_text = re.sub(rf"\b{re.escape(g)}\b", " ", cleaned_text, flags=re.IGNORECASE).strip()
                    break

        detected_mood: str | None = filters.get("mood")
        if detected_mood:
            detected_mood = detected_mood.lower().strip()
        elif not detected_genre:
            norm_lower = f" {cleaned_text.lower()} "
            for m in sorted(KNOWN_MOODS, key=len, reverse=True):
                if f" {m} " in norm_lower:
                    detected_mood = m
                    cleaned_text = re.sub(rf"\b{re.escape(m)}\b", " ", cleaned_text, flags=re.IGNORECASE).strip()
                    break

        # Strip remaining language connector words if language/genre/era was matched
        free_tokens = []
        for tok in cleaned_text.split():
            clean_tok = re.sub(r"[^\w]", "", tok.lower())
            if (detected_language or year_range or detected_genre) and clean_tok in LANGUAGE_CONNECTORS:
                continue
            free_tokens.append(tok)
        free_text = " ".join(free_tokens).strip()
        if not free_text and (detected_language or filters or year_range or detected_genre):
            free_text = raw

        # 7. Intent detection
        entity_intent = "GENERAL"
        if "song" in filters:
            entity_intent = "SONG"
        elif "album" in filters or "movie" in filters:
            entity_intent = "ALBUM"
        elif "artist" in filters or "singer" in filters or "composer" in filters:
            entity_intent = "ARTIST"
        else:
            norm_free = normalize_query(free_text)
            tokens_free = norm_free.split()
            if any(k in tokens_free for k in ["song", "songs", "track", "music", "audio"]):
                if any(k in tokens_free for k in ["movie", "soundtrack", "ost", "album", "film"]):
                    entity_intent = "ALBUM"
                else:
                    entity_intent = "SONG"
            elif any(k in tokens_free for k in ["movie", "soundtrack", "ost", "album", "film"]):
                entity_intent = "ALBUM"
            elif any(k in tokens_free for k in ["singer", "artist", "composer", "director"]):
                entity_intent = "ARTIST"
            elif any(k in tokens_free for k in ["playlist", "collection", "party"]):
                entity_intent = "PLAYLIST"
            elif len(tokens_free) >= 2:
                entity_intent = "COMBINED"
            else:
                entity_intent = "GENERAL"

        # 8. Lyric candidate detection:
        # Eligible if query has >= 3 meaningful words (non-stopwords) and total character length >= 12,
        # with no explicit filter syntax.
        meaningful_words = [
            w for w in normalize_query(free_text).split()
            if len(w) > 2 and w not in STOP_WORDS and w not in KNOWN_LANGUAGES
        ]
        lyric_candidate = (
            not filters
            and len(meaningful_words) >= 3
            and len(free_text) >= 12
            and not is_exact_phrase
        )

        return ParsedSearchQuery(
            raw_query=raw,
            normalized_query=norm,
            free_text=free_text,
            entity_intent=entity_intent,
            filters=filters,
            year=year,
            year_range=year_range,
            artist=filters.get("artist"),
            album=filters.get("album"),
            song=filters.get("song"),
            movie=filters.get("movie"),
            genre=detected_genre,
            language=detected_language,
            mood=detected_mood,
            composer=filters.get("composer"),
            singer=filters.get("singer"),
            lyric_candidate=lyric_candidate,
            is_exact_phrase=is_exact_phrase,
            quoted_phrase=quoted_phrase,
        )
