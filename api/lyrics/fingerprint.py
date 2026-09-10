import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TrackFingerprint:
    title: str
    clean_title: str
    primary_artist: str
    featured_artists: list[str] = field(default_factory=list)
    all_artists: str = ""
    album: str = ""
    language: str = ""
    duration_seconds: int = 0
    provider_track_id: str = ""
    isrc: str = ""
    release_year: int | None = None
    version_type: str = "original"  # "original", "remix", "live", "acoustic", "karaoke", "cover", "instrumental", "reprise", etc.

    @property
    def normalized_title(self) -> str:
        return normalize_text(self.clean_title if self.version_type == "original" else self.title)

    @property
    def normalized_primary_artist(self) -> str:
        return normalize_artist_name(self.primary_artist)


_VERSION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("karaoke", re.compile(r"\b(?:karaoke|backing\s+track|instrumental\s+karaoke)\b", re.IGNORECASE)),
    ("cover", re.compile(r"\b(?:cover|tribute|originally\s+performed\s+by)\b", re.IGNORECASE)),
    ("remix", re.compile(r"\b(?:remix|mix|club\s+mix|extended\s+mix|edm\s+version|dj\s+remix|dance\s+mix|bootleg)\b", re.IGNORECASE)),
    ("live", re.compile(r"\b(?:live|in\s+concert|live\s+at|unplugged|acoustic\s+live)\b", re.IGNORECASE)),
    ("acoustic", re.compile(r"\b(?:acoustic|piano\s+version|stripped|guitar\s+version)\b", re.IGNORECASE)),
    ("reprise", re.compile(r"\b(?:reprise)\b", re.IGNORECASE)),
    ("instrumental", re.compile(r"\b(?:instrumental)\b", re.IGNORECASE)),
    ("sped_up", re.compile(r"\b(?:sped\s+up|speed\s+up|nightcore)\b", re.IGNORECASE)),
    ("slowed", re.compile(r"\b(?:slowed|slowed\s+\+\s+reverb|slowed\s+down)\b", re.IGNORECASE)),
    ("radio_edit", re.compile(r"\b(?:radio\s+edit|radio\s+mix)\b", re.IGNORECASE)),
    ("extended", re.compile(r"\b(?:extended|extended\s+version)\b", re.IGNORECASE)),
    ("female_version", re.compile(r"\b(?:female\s+version|female)\b", re.IGNORECASE)),
    ("male_version", re.compile(r"\b(?:male\s+version|male)\b", re.IGNORECASE)),
]

_SPACE = re.compile(r"\s+")
_NOISE_TAGS = re.compile(
    r"\s*[\(\[](?:official\s+(?:audio|video|lyric\s+video|music\s+video)|lyric\s+video|audio|video|full\s+song|lyrical|promo|video\s+song)[\]\)]",
    re.IGNORECASE,
)
_FROM_MOVIE_TAGS = re.compile(
    r"\s*[\(\[](?:from\s+[\"']?[^\)\]]+[\"']?|from\s+the\s+(?:movie|film)\s+[\"']?[^\)\]]+[\"']?)[\]\)]",
    re.IGNORECASE,
)
_OST_TAGS = re.compile(
    r"\s*[\(\[](?:original\s+(?:motion\s+picture\s+)?soundtrack|soundtrack|ost)[\]\)]",
    re.IGNORECASE,
)
_LANGUAGE_TAGS = re.compile(
    r"\s*[\(\[](?:malayalam|telugu|tamil|hindi|kannada|punjabi|bengali|marathi|english|arabic)[\]\)]",
    re.IGNORECASE,
)
_DASH_NOISE = re.compile(
    r"\s*-\s*(?:from\s+[^\-]+|official[^\-]*|lyrical[^\-]*|original\s+soundtrack|theme|promo|full\s+song|reprise).*",
    re.IGNORECASE,
)


def normalize_text(text: str) -> str:
    """Normalize text with Unicode NFKD decomposition, strip punctuation and whitespace."""
    if not text:
        return ""
    # NFKD decompose and drop non-spacing mark (accents/diacritics)
    decomposed = unicodedata.normalize("NFKD", str(text))
    without_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    # Replace punctuation and symbols with single space
    cleaned = re.sub(r"[^\w\s]", " ", without_accents).lower()
    return _SPACE.sub(" ", cleaned).strip()


def normalize_artist_name(name: str) -> str:
    """Normalize artist names so 'A.R. Rahman', 'A R Rahman', and 'A. R. Rahman' match identically."""
    if not name:
        return ""
    # Normalize unicode
    base = normalize_text(name)
    # Collapse single-letter initials followed by spaces: 'a r rahman' -> 'ar rahman'
    # Pattern looks for single characters separated by space at beginning
    tokens = base.split()
    initials = []
    rest = []
    collecting_initials = True
    for t in tokens:
        if collecting_initials and len(t) == 1:
            initials.append(t)
        else:
            collecting_initials = False
            rest.append(t)
    if initials:
        combined_initials = "".join(initials)
        base = " ".join([combined_initials] + rest)
    return base.strip()


def extract_version_type(title: str) -> str:
    """Detect version tag from track title or subtitle."""
    text = str(title or "").lower()
    for v_type, pattern in _VERSION_PATTERNS:
        if pattern.search(text):
            return v_type
    return "original"


def clean_song_title(title: str) -> str:
    """Clean promotional, movie tags, and bracket noise while preserving version keywords."""
    raw = str(title or "").strip()
    if not raw:
        return ""
    s = _NOISE_TAGS.sub("", raw)
    s = _FROM_MOVIE_TAGS.sub("", s)
    s = _OST_TAGS.sub("", s)
    s = _LANGUAGE_TAGS.sub("", s)
    s = _DASH_NOISE.sub("", s)
    # Remove empty brackets/parens () []
    s = re.sub(r"\s*[\(\[]\s*[\)\]]", "", s)
    # Remove trailing dashes, commas, colons
    s = re.sub(r"[\s\-_–—:]+$", "", s).strip()
    return _SPACE.sub(" ", s).strip() or raw


def parse_artists(raw: Any) -> tuple[str, list[str]]:
    """Extract primary artist and featured artists list from various metadata structures.
    Handles Indian metadata (prioritizes singers over composer/lyricist if annotated).
    """
    if not raw:
        return "", []

    names: list[str] = []
    singer_names: list[str] = []

    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("title") or "").strip()
                role = str(item.get("role") or item.get("type") or "").lower()
                if name:
                    names.append(name)
                    if "sing" in role or "vocal" in role or "artist" in role:
                        singer_names.append(name)
            elif isinstance(item, str) and item.strip():
                names.append(item.strip())
    else:
        s = str(raw).strip()
        # Split on comma, &, /, feat., ft., with, vs., and
        split_pattern = re.compile(r",|&|/|(?:\s+(?:feat\.?|ft\.?|featuring|with|vs\.?|and)\s+)", re.IGNORECASE)
        parts = [p.strip() for p in split_pattern.split(s) if p.strip()]
        names.extend(parts)

    target_list = singer_names if singer_names else names
    if not target_list:
        return "", []

    primary = target_list[0]
    featured = [n for n in names if n.lower() != primary.lower()]
    return primary, featured


def extract_movie_or_album(track: dict[str, Any]) -> str:
    """Extract album or movie name, recognizing Indian movie credits."""
    album = str(track.get("album") or track.get("album_name") or track.get("movie") or "").strip()
    if not album:
        # Check if title has (From "Movie")
        match = re.search(r'(?:from\s+["\']?([^"\')\]]+)["\']?)', str(track.get("title") or ""), re.IGNORECASE)
        if match:
            album = match.group(1).strip()
    return album


def create_track_fingerprint(track: dict[str, Any]) -> TrackFingerprint:
    """Generate a canonical TrackFingerprint from track metadata dictionary."""
    raw_title = str(track.get("title") or track.get("trackName") or track.get("name") or "").strip()
    clean = clean_song_title(raw_title)
    version = extract_version_type(raw_title)

    primary_artist, featured_artists = parse_artists(
        track.get("artists") or track.get("artist") or track.get("artistName") or track.get("singers")
    )
    all_artists_str = ", ".join([primary_artist] + featured_artists) if primary_artist else ""

    album = extract_movie_or_album(track)

    raw_duration = track.get("duration_seconds") or track.get("duration") or track.get("duration_ms")
    duration_sec = 0
    if raw_duration is not None:
        try:
            if isinstance(raw_duration, str) and ":" in raw_duration:
                parts = raw_duration.split(":")
                if len(parts) == 2:
                    duration_sec = int(parts[0]) * 60 + int(parts[1])
                elif len(parts) == 3:
                    duration_sec = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            else:
                val = float(raw_duration)
                duration_sec = round(val / 1000) if val > 1000 else round(val)
        except (ValueError, TypeError):
            pass

    language = str(track.get("language") or "").strip().lower()
    isrc = str(track.get("isrc") or "").strip().upper()
    provider_track_id = str(track.get("seokey") or track.get("track_id") or track.get("id") or "").strip()

    year: int | None = None
    raw_year = track.get("year") or track.get("release_year") or track.get("releaseDate") or track.get("release_date")
    if raw_year:
        year_match = re.search(r"\b(19\d\d|20\d\d)\b", str(raw_year))
        if year_match:
            year = int(year_match.group(1))

    return TrackFingerprint(
        title=raw_title,
        clean_title=clean,
        primary_artist=primary_artist,
        featured_artists=featured_artists,
        all_artists=all_artists_str,
        album=album,
        language=language,
        duration_seconds=duration_sec,
        provider_track_id=provider_track_id,
        isrc=isrc,
        release_year=year,
        version_type=version,
    )
