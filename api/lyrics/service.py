import logging
from typing import Any

from api.lyrics.fingerprint import TrackFingerprint, create_track_fingerprint
from api.lyrics.lrc import parse_lrc
from api.lyrics.provider import LyricsProvider
from api.lyrics.verifier import (
    MIN_CONFIDENCE_THRESHOLD,
    LyricsVerifier,
    validate_synced_timestamps,
)

logger = logging.getLogger(__name__)


def duration_seconds(value: Any) -> int:
    if isinstance(value, (int, float)):
        return max(0, round(value))
    text = str(value or "").strip()
    if not text:
        return 0
    if ":" in text:
        try:
            parts = [int(part) for part in text.split(":")]
            total = 0
            for part in parts:
                total = total * 60 + part
            return max(0, total)
        except ValueError:
            return 0
    try:
        return max(0, round(float(text)))
    except ValueError:
        return 0


class LyricsService:
    def __init__(
        self,
        provider: LyricsProvider,
        repository,
        success_ttl: int,
        not_found_ttl: int,
        cache=None,
    ):
        self.provider = provider
        self.repository = repository
        self.success_ttl = success_ttl
        self.not_found_ttl = not_found_ttl
        self.cache = cache

    def _is_cache_valid(self, cached: dict[str, Any], fingerprint: TrackFingerprint) -> bool:
        """Validate whether cached entry has verified status and matches current track fingerprint."""
        if not cached or not isinstance(cached, dict):
            return False

        status = cached.get("status")
        if status == "not_found":
            return True

        if "verified" in cached:
            if not cached.get("verified"):
                return False
            score = cached.get("verification_score") or cached.get("verificationScore") or 0
            if score < MIN_CONFIDENCE_THRESHOLD:
                return False

        # If cached entry has title/artist metadata, confirm metadata matches current fingerprint
        cand_title = cached.get("title") or cached.get("trackName")
        cand_artist = cached.get("artist") or cached.get("artistName") or cached.get("artists")
        if cand_title or cand_artist:
            valid, score, _ = LyricsVerifier.verify_candidate({
                "trackName": cand_title or "",
                "artistName": cand_artist or "",
                "albumName": cached.get("album") or cached.get("albumName"),
                "duration": cached.get("duration_seconds") or cached.get("duration"),
                "isrc": cached.get("isrc"),
            }, fingerprint)
            if not valid:
                return False

        return True

    def _response(self, track_id: str, data: dict[str, Any], duration: int) -> dict[str, Any]:
        status = data.get("status", "available")
        instrumental = bool(data.get("instrumental"))
        synced_lyrics = data.get("syncedLyrics") or data.get("synced_lyrics")
        plain_lyrics = data.get("plainLyrics") or data.get("plain_lyrics")
        provider_id = data.get("id") or data.get("provider_lyrics_id")
        verified = bool(data.get("verified"))
        verification_score = int(data.get("verification_score") or data.get("verificationScore") or 0)

        raw_lines = parse_lrc(synced_lyrics, duration * 1000) if status == "available" else []

        # Validate synchronized timestamps against track length
        lines = []
        if raw_lines and status == "available":
            if validate_synced_timestamps(raw_lines, duration):
                lines = raw_lines
            else:
                logger.warning("Synced lyrics timestamp validation failed for %s", track_id)

        # If available but no lines and no plain lyrics and not instrumental, mark not_found
        if status == "available" and not instrumental and not lines and (not plain_lyrics or not plain_lyrics.strip()):
            status = "not_found"
            verified = False

        return {
            "trackId": track_id,
            "provider": data.get("provider", "lrclib") if status != "not_found" else None,
            "providerId": str(provider_id) if provider_id is not None else None,
            "status": status,
            "synced": bool(lines),
            "instrumental": instrumental,
            "plainLyrics": plain_lyrics if status != "not_found" else None,
            "lines": lines,
            "verified": verified,
            "verificationScore": verification_score,
        }

    async def get_lyrics(self, track: dict[str, Any]) -> dict[str, Any]:
        fingerprint = create_track_fingerprint(track)
        track_id = str(fingerprint.provider_track_id or track.get("seokey") or track.get("id") or "")
        duration = fingerprint.duration_seconds

        # 1. Check Redis memory cache
        if self.cache:
            try:
                cached_data = await self.cache.get(f"lyrics:{track_id}")
                if cached_data and isinstance(cached_data, dict):
                    if self._is_cache_valid(cached_data, fingerprint):
                        return self._response(track_id, cached_data, duration)
                    else:
                        logger.info("Invalidating unverified cached lyrics for %s", track_id)
            except Exception:
                pass

        # 2. Check Database repository
        cached = await self.repository.get(track_id, self.success_ttl, None)
        if cached and cached.get("status") != "not_found":
            if self._is_cache_valid(cached, fingerprint):
                if self.cache:
                    try:
                        await self.cache.set(f"lyrics:{track_id}", cached, self.success_ttl)
                    except Exception:
                        pass
                return self._response(track_id, cached, duration)
            else:
                logger.info("Invalidating unverified repository lyrics for %s", track_id)

        # Check negative cache
        cached_nf = await self.repository.get(track_id, self.not_found_ttl, "not_found")
        if cached_nf:
            return self._response(track_id, cached_nf, duration)

        # 3. Fetch from provider with fingerprint verification
        result = await self.provider.get_lyrics(track)

        if result is None:
            save_payload = {
                "provider": "lrclib",
                "status": "not_found",
                "instrumental": False,
                "verified": False,
                "verification_score": 0,
            }
            ttl = self.not_found_ttl
            await self.repository.put(track_id, save_payload)
            if self.cache:
                try:
                    await self.cache.set(f"lyrics:{track_id}", save_payload, ttl)
                except Exception:
                    pass
            return self._response(track_id, save_payload, duration)

        # 4. Verify candidate result
        score = result.get("_verification_score")
        is_verified = result.get("_verified")
        if is_verified is None or score is None:
            is_verified, score, _ = LyricsVerifier.verify_candidate(result, fingerprint)

        if not is_verified or score < MIN_CONFIDENCE_THRESHOLD:
            # Low confidence result rejected
            logger.info("Lyrics candidate rejected for %s: confidence %d < %d", track_id, score, MIN_CONFIDENCE_THRESHOLD)
            save_payload = {
                "provider": "lrclib",
                "status": "not_found",
                "instrumental": False,
                "verified": False,
                "verification_score": score,
            }
            ttl = self.not_found_ttl
            await self.repository.put(track_id, save_payload)
            if self.cache:
                try:
                    await self.cache.set(f"lyrics:{track_id}", save_payload, ttl)
                except Exception:
                    pass
            return self._response(track_id, save_payload, duration)

        # 5. Candidate is verified
        is_instrumental = bool(result.get("instrumental"))
        status = "instrumental" if is_instrumental else "available"
        save_payload = {
            **result,
            "provider": "lrclib",
            "status": status,
            "instrumental": is_instrumental,
            "verified": True,
            "verification_score": score,
            "title": fingerprint.title,
            "artist": fingerprint.primary_artist,
            "album": fingerprint.album,
            "duration_seconds": duration,
            "language": fingerprint.language,
        }
        ttl = self.success_ttl

        await self.repository.put(track_id, save_payload)
        if self.cache:
            try:
                await self.cache.set(f"lyrics:{track_id}", save_payload, ttl)
            except Exception:
                pass

        return self._response(track_id, save_payload, duration)
