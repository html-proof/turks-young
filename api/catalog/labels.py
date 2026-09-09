from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import re
from typing import Any
import unicodedata

logger = logging.getLogger(__name__)

# ==============================================================================
# 1. Seed List (Initial seed only — NOT the final source of truth)
# ==============================================================================
_KNOWN_OFFICIAL_LABELS = {
    # Major / international
    "sony music",
    "sony music india",
    "sony music entertainment",
    "universal music",
    "universal music india",
    "warner music",
    "warner music india",
    "emi",
    "virgin music",
    "believe music",
    "orchard",
    "the orchard",
    "ada",
    "ada music",

    # Major Indian labels
    "t-series",
    "saregama",
    "saregama india",
    "zee music",
    "zee music company",
    "tips",
    "tips industries",
    "tips official",
    "yrf music",
    "eros music",
    "venus",
    "venus worldwide entertainment",
    "speed records",
    "times music",

    # South Indian labels
    "think music",
    "think music india",
    "lahari music",
    "lahari recording company",
    "aditya music",
    "mango music",
    "star music",
    "muzik247",
    "muzik 247",
    "satyam audios",
    "satyam audio",
    "manorama music",
    "millennium audios",
    "millennium audio",
    "east coast",
    "east coast audio",
    "speed audio",
    "speed audio & video",
    "surya audio",
    "madhu audio",
    "rafa international",
    "dvocean",
    "audio video media",

    # Malayalam / regional
    "goodwill entertainments",
    "goodwill entertainment",
    "123musix",
    "123 musix",
    "music 247",
    "satya audios",
    "mc audios",
    "celebrations",
    "celebrations audio",
    "johny sagariga",
    "johny sagariga audio",
    "saina",
    "saina audio and video",
    "saina music",
    "saina movies",
    "central pictures",
    "central music",
    "bhavana media vision",
    "ap international",
    "a p international",

    # Tamil / Telugu / Kannada
    "anirudh official",
    "divo",
    "divo music",
    "divo movies",
    "uie",
    "u1 records",
    "u1 records private limited",
    "think originals",
    "trend music",
    "trendmusic",
    "santhosh narayanan",
    "maajja",
    "think indie",
    "aananda audio",
    "anand audio",
    "ashwini recording company",
    "jhankar music",
    "akshaya audio",
    "supreme music",
    "volga video",
    "silicon audio",
    "shivaranjani music",
    "sri balaji music",
    "sri venkateswara creations",
    "sri venkateswara audio",
    "madhura audio",
    "madhura audio originals",

    # Film / studio-linked music
    "sun pictures",
    "sun tv",
    "sun tv network",
    "gemini tv",
    "hombale films",
    "lyca productions",
    "dream warrior pictures",
    "ags entertainment",
    "seven screen studio",
    "mythri movie makers",
    "geetha arts",
    "sithara entertainments",
    "dharma productions",
    "red giant movies",
    "v cinemas",
    "v cinemas international",
}

# ==============================================================================
# 2. Canonical Aliases Mapping
# ==============================================================================
_LABEL_ALIASES = {
    "sony music india": "sony music",
    "sony music entertainment": "sony music",
    "saregama india": "saregama",
    "zee music company": "zee music",
    "tips industries": "tips",
    "tips official": "tips",
    "venus worldwide entertainment": "venus",
    "think music india": "think music",
    "lahari recording company": "lahari music",
    "muzik 247": "muzik247",
    "music 247": "muzik247",
    "satyam audio": "satyam audios",
    "satya audios": "satyam audios",
    "millennium audio": "millennium audios",
    "east coast audio": "east coast",
    "speed audio and video": "speed audio",
    "speed audio & video": "speed audio",
    "goodwill entertainment": "goodwill entertainments",
    "123 musix": "123musix",
    "the orchard": "orchard",
    "a p international": "ap international",
    "anand audio": "aananda audio",
    "celebrations audio": "celebrations",
    "johny sagariga audio": "johny sagariga",
    "saina audio and video": "saina",
    "saina music": "saina",
    "saina movies": "saina",
    "central music": "central pictures",
    "divo music": "divo",
    "divo movies": "divo",
    "u1 records private limited": "u1 records",
    "trendmusic": "trend music",
    "sun tv": "sun pictures",
    "sun tv network": "sun pictures",
    "v cinemas international": "v cinemas",
    "madhura audio originals": "madhura audio",
}

# Generic / disallowed tokens that should never become official labels
_DISALLOWED_LABELS = {
    "unknown",
    "various artists",
    "music",
    "audio",
    "official",
    "songs",
    "soundtrack",
    "ost",
    "various",
    "na",
    "null",
    "none",
    "undefined",
    "video",
    "track",
    "records",
    "record",
    "entertainment",
    "company",
    "production",
    "productions",
    "media",
    "digital",
}


# ==============================================================================
# 3. Label Normalization & Canonicalization
# ==============================================================================
def normalize_label(value: str) -> str:
    """Normalize label string using NFKC, lowercase, stripping punctuation."""
    if not value:
        return ""

    value = unicodedata.normalize("NFKC", str(value))
    value = value.casefold().strip()

    value = value.replace("&", " and ")

    value = re.sub(
        r"\b(official youtube channel|official channel|official)\b",
        "",
        value,
    )

    value = re.sub(r"[^a-z0-9 ]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()

    return value


def canonicalize_label(label: str) -> str:
    """Resolve aliases into their canonical name."""
    normalized = normalize_label(label)
    if not normalized:
        return ""
    return _LABEL_ALIASES.get(normalized, normalized)


# ==============================================================================
# 4. Multi-Signal Confidence Calculator
# ==============================================================================
def calculate_label_confidence(
    appears_in_label_field: bool = False,
    appears_in_copyright: bool = False,
    appears_across_multiple_songs: bool = False,
    appears_across_multiple_albums: bool = False,
    provider_marks_official: bool = False,
) -> int:
    """
    Calculate confidence score (0 to 100) for a discovered label candidate:
    - appears_in_label_field: +40
    - appears_in_copyright: +20
    - appears_across_multiple_songs: +15
    - appears_across_multiple_albums: +15
    - provider_marks_official: +10
    """
    score = 0

    if appears_in_label_field:
        score += 40

    if appears_in_copyright:
        score += 20

    if appears_across_multiple_songs:
        score += 15

    if appears_across_multiple_albums:
        score += 15

    if provider_marks_official:
        score += 10

    return min(score, 100)


# ==============================================================================
# 5. Metadata Candidate Extraction
# ==============================================================================
_COPYRIGHT_CLEANUP = re.compile(
    r"^(?:(?:\([cCpP]\)|©|℗|\bcopy\b|\bphonographic\b)\s*)+|\b(?:19|20)\d{2}\b",
    re.IGNORECASE,
)


def _clean_copyright_candidate(raw: str) -> str:
    """Strip leading copyright symbols, years, and legal noise from copyright string."""
    cleaned = _COPYRIGHT_CLEANUP.sub(" ", raw.strip())
    # Remove recurring legal suffixes like 'Pvt. Ltd.', 'Inc.', 'LLC' if trailing
    cleaned = re.sub(r"\b(pvt\.?\s*ltd\.?|private limited|inc\.?|llc|corp\.?)\b", " ", cleaned, flags=re.I)
    return cleaned.strip()


def extract_candidates_from_metadata(item: dict[str, Any]) -> list[tuple[str, str]]:
    """
    Extract (candidate_string, field_source) from song or album metadata.
    Sources: 'label', 'copyright', 'publisher', 'production_company', 'channel'.
    """
    candidates: list[tuple[str, str]] = []

    # 1. Label fields
    for key in ("label", "record_label", "music_label", "label_name"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            candidates.append((val.strip(), "label"))

    # 2. Copyright fields
    for key in ("copyright", "copyright_text", "c_line", "p_line", "copyright_line"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            cleaned = _clean_copyright_candidate(val)
            if cleaned:
                candidates.append((cleaned, "copyright"))

    # 3. Publisher
    for key in ("publisher", "music_publisher"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            candidates.append((val.strip(), "publisher"))

    # 4. Production Company / Studio
    for key in ("production_company", "production", "producer", "studio"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            candidates.append((val.strip(), "production"))

    # 5. Channel
    for key in ("channel", "channel_name", "youtube_channel"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            candidates.append((val.strip(), "channel"))

    # 6. Nested album metadata
    album = item.get("album")
    if isinstance(album, dict):
        for val, src in extract_candidates_from_metadata(album):
            candidates.append((val, f"album_{src}"))

    return candidates


# ==============================================================================
# 6. Label Record & Observation Model
# ==============================================================================
@dataclass
class LabelRecord:
    canonical_name: str
    normalized_name: str
    aliases: list[str] = field(default_factory=list)
    source: str = "dynamic"  # "seed" or "dynamic"
    verified: bool = False
    confidence: int = 0
    song_count: int = 1
    album_count: int = 1
    first_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    seen_song_ids: set[str] = field(default_factory=set)
    seen_album_ids: set[str] = field(default_factory=set)
    in_label_field: bool = False
    in_copyright: bool = False
    provider_official: bool = False


# ==============================================================================
# 7. Dynamic Label Registry
# ==============================================================================
class LabelRegistry:
    """
    In-memory registry of known and discovered official record labels and studios.
    - Seeded with comprehensive seed set.
    - Dynamically learns new labels from incoming metadata.
    - Updates confidence based on multi-source evidence.
    - Promotes candidates to verified when confidence >= 70.
    """

    def __init__(self, repository: PostgresLabelRepository | None = None):
        self.repository = repository
        self._verified_labels: set[str] = set()
        self._records: dict[str, LabelRecord] = {}
        self._aliases: dict[str, str] = dict(_LABEL_ALIASES)
        self._pending_saves: dict[str, LabelRecord] = {}
        self._seed_initial_labels()

    def _seed_initial_labels(self) -> None:
        """Seed the registry with initial known official labels."""
        for raw in _KNOWN_OFFICIAL_LABELS:
            norm = normalize_label(raw)
            if not norm or len(norm) < 3 or norm in _DISALLOWED_LABELS:
                continue
            canonical = _LABEL_ALIASES.get(norm, norm)
            self._verified_labels.add(norm)
            self._verified_labels.add(canonical)

            if canonical not in self._records:
                self._records[canonical] = LabelRecord(
                    canonical_name=canonical,
                    normalized_name=norm,
                    aliases=[norm] if norm != canonical else [],
                    source="seed",
                    verified=True,
                    confidence=100,
                    song_count=100,
                    album_count=50,
                    in_label_field=True,
                    in_copyright=True,
                    provider_official=True,
                )
            else:
                if norm != canonical and norm not in self._records[canonical].aliases:
                    self._records[canonical].aliases.append(norm)

    def is_verified_label(self, label: str) -> bool:
        """
        Fast O(1) verification check.
        Returns True if the label is recognized as a verified official label.
        """
        if not label:
            return False

        norm = normalize_label(label)
        if not norm:
            return False

        if norm in self._verified_labels:
            return True

        canonical = self._aliases.get(norm, norm)
        if canonical in self._verified_labels:
            return True

        # Substring check for multi-word labels (e.g. "sony music entertainment india" matching "sony music")
        for verified in self._verified_labels:
            if len(verified) >= 4 and (verified in norm or (len(norm) >= 6 and norm in verified)):
                return True

        return False

    def register_candidate(
        self,
        raw_label: str,
        appears_in_label_field: bool = False,
        appears_in_copyright: bool = False,
        song_id: str | None = None,
        album_id: str | None = None,
        provider_marks_official: bool = False,
    ) -> str | None:
        """
        Process a candidate label string:
        - Normalizes & canonicalizes
        - Discards invalid/garbage inputs
        - Updates observations (song count, album count, field appearances)
        - Computes new confidence
        - Promotes to verified if confidence >= 70
        """
        norm = normalize_label(raw_label)
        if not norm or len(norm) < 3 or norm in _DISALLOWED_LABELS:
            return None

        canonical = self._aliases.get(norm, norm)

        now = datetime.now(timezone.utc)
        record = self._records.get(canonical)

        if record is None:
            record = LabelRecord(
                canonical_name=canonical,
                normalized_name=norm,
                aliases=[norm] if norm != canonical else [],
                source="dynamic",
                verified=False,
                confidence=0,
                song_count=0,
                album_count=0,
                first_seen_at=now,
                last_seen_at=now,
            )
            self._records[canonical] = record

        # Update timestamps and aliases
        record.last_seen_at = now
        if norm != canonical and norm not in record.aliases:
            record.aliases.append(norm)
            self._aliases[norm] = canonical

        # Update observations
        if appears_in_label_field:
            record.in_label_field = True
        if appears_in_copyright:
            record.in_copyright = True
        if provider_marks_official:
            record.provider_official = True

        if song_id:
            record.seen_song_ids.add(str(song_id))
            record.song_count = len(record.seen_song_ids)

        if album_id:
            record.seen_album_ids.add(str(album_id))
            record.album_count = len(record.seen_album_ids)

        # Recalculate confidence
        multi_songs = record.song_count > 1
        multi_albums = record.album_count > 1

        confidence = calculate_label_confidence(
            appears_in_label_field=record.in_label_field,
            appears_in_copyright=record.in_copyright,
            appears_across_multiple_songs=multi_songs,
            appears_across_multiple_albums=multi_albums,
            provider_marks_official=record.provider_official,
        )

        record.confidence = max(record.confidence, confidence)

        # Promotion check
        if record.confidence >= 70:
            record.verified = True
            self._verified_labels.add(canonical)
            self._verified_labels.add(norm)
            for alias in record.aliases:
                self._verified_labels.add(alias)
            logger.info("label_verified canonical=%s confidence=%d", canonical, record.confidence)

        # Queue for DB persistence
        self._pending_saves[canonical] = record

        return canonical

    def observe_song(self, song: dict[str, Any]) -> None:
        """Extract and register all candidate labels from song metadata."""
        if not isinstance(song, dict):
            return

        song_id = song.get("id") or song.get("seokey") or song.get("track_id")
        album = song.get("album")
        album_id = (album.get("id") or album.get("provider_id")) if isinstance(album, dict) else song.get("album_id")

        candidates = extract_candidates_from_metadata(song)
        for raw_val, src in candidates:
            in_label = "label" in src or "publisher" in src
            in_copyright = "copyright" in src
            self.register_candidate(
                raw_val,
                appears_in_label_field=in_label,
                appears_in_copyright=in_copyright,
                song_id=str(song_id) if song_id else None,
                album_id=str(album_id) if album_id else None,
            )

    def observe_album(self, album: dict[str, Any]) -> None:
        """Extract and register all candidate labels from album metadata."""
        if not isinstance(album, dict):
            return

        album_id = album.get("id") or album.get("provider_id") or album.get("album_id")
        candidates = extract_candidates_from_metadata(album)
        for raw_val, src in candidates:
            in_label = "label" in src or "publisher" in src
            in_copyright = "copyright" in src
            self.register_candidate(
                raw_val,
                appears_in_label_field=in_label,
                appears_in_copyright=in_copyright,
                album_id=str(album_id) if album_id else None,
            )

    async def flush_pending_to_db(self) -> None:
        """Flush newly discovered or updated labels to PostgreSQL."""
        if not self.repository or not self._pending_saves:
            return

        to_save = list(self._pending_saves.values())
        self._pending_saves.clear()
        try:
            await self.repository.batch_upsert(to_save)
        except Exception as exc:
            logger.warning("failed to flush labels to db: %s", exc)

    async def load_from_db(self) -> None:
        """Load verified labels and aliases from the database."""
        if not self.repository:
            return

        try:
            records = await self.repository.load_all()
            for rec in records:
                self._records[rec.canonical_name] = rec
                if rec.verified:
                    self._verified_labels.add(rec.canonical_name)
                    self._verified_labels.add(rec.normalized_name)
                    for alias in rec.aliases:
                        self._verified_labels.add(alias)
                        self._aliases[alias] = rec.canonical_name
            logger.info("loaded %d labels from database into registry", len(records))
        except Exception as exc:
            logger.warning("failed to load labels from db: %s", exc)


# ==============================================================================
# 8. PostgreSQL Persistence Repository
# ==============================================================================
class PostgresLabelRepository:
    """PostgreSQL storage repository for official_labels table."""

    def __init__(self, pool: Any | None):
        self.pool = pool

    async def load_all(self) -> list[LabelRecord]:
        if self.pool is None:
            return []

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT canonical_name, normalized_name, aliases, source, verified,
                       confidence, song_count, album_count, first_seen_at, last_seen_at
                  FROM official_labels
                """
            )

        records = []
        for r in rows:
            records.append(
                LabelRecord(
                    canonical_name=r["canonical_name"],
                    normalized_name=r["normalized_name"],
                    aliases=list(r["aliases"] or []),
                    source=r["source"],
                    verified=r["verified"],
                    confidence=r["confidence"],
                    song_count=r["song_count"],
                    album_count=r.get("album_count", 1),
                    first_seen_at=r["first_seen_at"],
                    last_seen_at=r["last_seen_at"],
                )
            )
        return records

    async def batch_upsert(self, records: list[LabelRecord]) -> None:
        if self.pool is None or not records:
            return

        query = """
        INSERT INTO official_labels (
            canonical_name, normalized_name, aliases, source, verified,
            confidence, song_count, album_count, first_seen_at, last_seen_at, updated_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, now())
        ON CONFLICT (canonical_name) DO UPDATE SET
            aliases = EXCLUDED.aliases,
            verified = EXCLUDED.verified,
            confidence = EXCLUDED.confidence,
            song_count = EXCLUDED.song_count,
            album_count = EXCLUDED.album_count,
            last_seen_at = EXCLUDED.last_seen_at,
            updated_at = now()
        """

        async with self.pool.acquire() as conn:
            for rec in records:
                await conn.execute(
                    query,
                    rec.canonical_name,
                    rec.normalized_name,
                    rec.aliases,
                    rec.source,
                    rec.verified,
                    rec.confidence,
                    rec.song_count,
                    rec.album_count,
                    rec.first_seen_at,
                    rec.last_seen_at,
                )


# ==============================================================================
# 9. Singleton Instance & Module Functions
# ==============================================================================
label_registry = LabelRegistry()


def is_verified_label(label: str) -> bool:
    """Convenience helper to check if a label is verified in the global registry."""
    return label_registry.is_verified_label(label)
