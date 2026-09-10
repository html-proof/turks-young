from difflib import SequenceMatcher
from typing import Any

from api.lyrics.fingerprint import (
    TrackFingerprint,
    create_track_fingerprint,
    extract_version_type,
    normalize_artist_name,
    normalize_text,
)

MIN_CONFIDENCE_THRESHOLD = 850


def text_similarity(s1: str, s2: str) -> float:
    """Compute string similarity ratio between normalized strings."""
    n1 = normalize_text(s1)
    n2 = normalize_text(s2)
    if not n1 or not n2:
        return 0.0
    if n1 == n2:
        return 1.0
    return SequenceMatcher(None, n1, n2).ratio()


def artist_similarity(art1: str, art2: str) -> float:
    """Compute normalized artist similarity taking into account initials."""
    n1 = normalize_artist_name(art1)
    n2 = normalize_artist_name(art2)
    if not n1 or not n2:
        return 0.0
    if n1 == n2:
        return 1.0
    # Contained match for multi-token artists (e.g. "Arijit Singh" in "Arijit Singh, Mithoon")
    if (len(n1) >= 4 and n1 in n2) or (len(n2) >= 4 and n2 in n1):
        return 0.95
    return SequenceMatcher(None, n1, n2).ratio()


def validate_synced_timestamps(lines: list[dict[str, Any]], duration_seconds: int = 0) -> bool:
    """Validate that synchronized lyrics timestamps are sensible and match track length.
    - Timestamps must not be negative.
    - Timestamps must be non-decreasing.
    - Timestamps cannot extend far beyond track duration.
    - Majority of timestamps must fall within track duration.
    """
    if not lines:
        return False

    prev_start = -1
    in_bounds_count = 0
    total_count = len(lines)
    max_allowed_ms = (duration_seconds + 12) * 1000 if duration_seconds > 0 else None

    for line in lines:
        start_ms = line.get("startMs")
        if start_ms is None or not isinstance(start_ms, (int, float)):
            return False
        start_ms = int(start_ms)

        # Non-negative
        if start_ms < 0:
            return False

        # Monotonically increasing
        if start_ms < prev_start:
            return False
        prev_start = start_ms

        if duration_seconds > 0:
            # Cannot exceed track duration + tolerance
            if max_allowed_ms and start_ms > max_allowed_ms:
                return False
            if start_ms <= (duration_seconds + 3) * 1000:
                in_bounds_count += 1

    # If duration is known, require at least 75% of timestamps to fall within track duration
    if duration_seconds > 10 and total_count >= 4:
        if (in_bounds_count / total_count) < 0.75:
            return False

    return True


class LyricsVerifier:
    """Strict confidence scoring and metadata validation engine for song lyrics."""

    @classmethod
    def calculate_confidence(
        cls,
        candidate: dict[str, Any],
        target: TrackFingerprint,
    ) -> tuple[int, list[str]]:
        """Compute confidence score (-1000 to +1000+) and list of evaluation notes."""
        score = 0
        reasons: list[str] = []

        cand_title = str(candidate.get("trackName") or candidate.get("title") or "").strip()
        cand_artist = str(candidate.get("artistName") or candidate.get("artist") or candidate.get("artists") or "").strip()
        cand_album = str(candidate.get("albumName") or candidate.get("album") or "").strip()
        cand_duration = candidate.get("duration") or candidate.get("duration_seconds")
        cand_isrc = str(candidate.get("isrc") or "").strip().upper()
        cand_id = str(candidate.get("id") or candidate.get("provider_lyrics_id") or "").strip()
        cand_version = extract_version_type(cand_title)

        # 1. Exact Recording Identifier (ISRC)
        if target.isrc and cand_isrc and target.isrc == cand_isrc:
            score += 1000
            reasons.append("exact_isrc (+1000)")
            return score, reasons

        # 2. Exact Provider Track ID match
        if target.provider_track_id and cand_id and target.provider_track_id == cand_id:
            score += 900
            reasons.append("exact_provider_track_id (+900)")

        # 3. Title Verification
        title_sim = max(
            text_similarity(cand_title, target.title),
            text_similarity(cand_title, target.clean_title),
        )
        if title_sim >= 0.98:
            score += 500
            reasons.append(f"exact_title (+500, sim={title_sim:.2f})")
        elif title_sim >= 0.88:
            score += 400
            reasons.append(f"high_title_match (+400, sim={title_sim:.2f})")
        elif title_sim >= 0.70:
            score += 200
            reasons.append(f"moderate_title_match (+200, sim={title_sim:.2f})")
        else:
            score -= 1000
            reasons.append(f"wrong_title (-1000, sim={title_sim:.2f})")
            return score, reasons  # Early exit on wrong title

        # 4. Mandatory Artist Verification
        target_art_norm = target.normalized_primary_artist
        cand_art_norm = normalize_artist_name(cand_artist)

        art_sim = artist_similarity(cand_artist, target.primary_artist)
        # Check against all featured artists if present
        featured_sims = [artist_similarity(cand_artist, fa) for fa in target.featured_artists]
        max_featured_sim = max(featured_sims) if featured_sims else 0.0

        if art_sim >= 0.95 or (target_art_norm and cand_art_norm and target_art_norm == cand_art_norm):
            score += 500
            reasons.append(f"exact_artist (+500, sim={art_sim:.2f})")
        elif art_sim >= 0.82 or (target_art_norm and len(target_art_norm) >= 4 and target_art_norm in cand_art_norm):
            score += 400
            reasons.append(f"high_artist_match (+400, sim={art_sim:.2f})")
        elif max_featured_sim >= 0.85:
            score += 300
            reasons.append(f"featured_artist_match (+300, sim={max_featured_sim:.2f})")
        elif art_sim >= 0.60:
            score += 100
            reasons.append(f"partial_artist_match (+100, sim={art_sim:.2f})")
        else:
            score -= 1000
            reasons.append(f"wrong_artist (-1000, sim={art_sim:.2f})")
            return score, reasons  # Early exit on wrong artist

        # 5. Version Information Preservation
        # Reject mismatched remixes, covers, live versions, acoustics, etc.
        if target.version_type != cand_version:
            if cand_version == "cover" or target.version_type == "cover":
                score -= 900
                reasons.append(f"cover_mismatch (-900, target={target.version_type}, cand={cand_version})")
            elif cand_version == "karaoke" or target.version_type == "karaoke":
                score -= 900
                reasons.append(f"karaoke_mismatch (-900, target={target.version_type}, cand={cand_version})")
            elif cand_version == "remix" or target.version_type == "remix":
                score -= 700
                reasons.append(f"remix_mismatch (-700, target={target.version_type}, cand={cand_version})")
            elif cand_version == "live" or target.version_type == "live":
                score -= 700
                reasons.append(f"live_mismatch (-700, target={target.version_type}, cand={cand_version})")
            elif cand_version in ("acoustic", "unplugged", "reprise") or target.version_type in ("acoustic", "unplugged", "reprise"):
                score -= 700
                reasons.append(f"acoustic_reprise_mismatch (-700, target={target.version_type}, cand={cand_version})")
            else:
                score -= 500
                reasons.append(f"version_mismatch (-500, target={target.version_type}, cand={cand_version})")
        elif target.version_type != "original":
            # Bonus when specific version types match (e.g. both are acoustic or both are remix)
            score += 150
            reasons.append(f"matching_version_type (+150, {cand_version})")

        # 6. Album / Movie Verification
        if target.album and cand_album:
            alb_sim = text_similarity(cand_album, target.album)
            if alb_sim >= 0.85:
                score += 250
                reasons.append(f"exact_album (+250, sim={alb_sim:.2f})")
            elif alb_sim >= 0.60:
                score += 150
                reasons.append(f"similar_album (+150, sim={alb_sim:.2f})")
            elif len(target.album) >= 4 and len(cand_album) >= 4:
                # Strong album mismatch penalty (especially useful for distinct movies)
                score -= 300
                reasons.append(f"album_mismatch (-300, '{target.album}' vs '{cand_album}')")

        # 7. Duration Verification
        if target.duration_seconds > 0 and cand_duration is not None:
            try:
                cand_dur_val = float(cand_duration)
                diff = abs(target.duration_seconds - cand_dur_val)
                if diff <= 5:
                    score += 200
                    reasons.append(f"duration_exact (+200, diff={diff:.1f}s)")
                elif diff <= 10:
                    score += 100
                    reasons.append(f"duration_acceptable (+100, diff={diff:.1f}s)")
                elif diff <= 15:
                    reasons.append(f"duration_neutral (diff={diff:.1f}s)")
                elif diff <= 30:
                    score -= 300
                    reasons.append(f"duration_mismatch (-300, diff={diff:.1f}s)")
                else:
                    score -= 600
                    reasons.append(f"duration_severe_mismatch (-600, diff={diff:.1f}s)")
            except (ValueError, TypeError):
                pass

        # 8. Language Verification
        cand_lang = str(candidate.get("language") or "").strip().lower()
        if target.language and cand_lang:
            if target.language == cand_lang:
                score += 150
                reasons.append(f"language_match (+150, {cand_lang})")
            else:
                score -= 500
                reasons.append(f"language_mismatch (-500, {target.language} vs {cand_lang})")

        return score, reasons

    @classmethod
    def verify_candidate(
        cls,
        candidate: dict[str, Any],
        target: TrackFingerprint,
    ) -> tuple[bool, int, str]:
        """Verify whether candidate matches target with confidence >= MIN_CONFIDENCE_THRESHOLD."""
        score, reasons = cls.calculate_confidence(candidate, target)
        is_verified = score >= MIN_CONFIDENCE_THRESHOLD
        summary = f"score={score} (verified={is_verified}): {'; '.join(reasons)}"
        return is_verified, score, summary
