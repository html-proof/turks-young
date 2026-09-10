import abc
import asyncio
import re
from difflib import SequenceMatcher
from typing import Any

import aiohttp


class LyricsProviderError(RuntimeError):
    pass


class LyricsRateLimited(LyricsProviderError):
    def __init__(self, retry_after: int):
        super().__init__("Lyrics provider rate limit reached")
        self.retry_after = retry_after


class LyricsProvider(abc.ABC):
    @abc.abstractmethod
    async def get_lyrics(self, track: dict[str, Any]) -> dict[str, Any] | None:
        raise NotImplementedError


_SPACE = re.compile(r"\s+")
_NOISE = re.compile(
    r"\s*[\[(](?:official\s+(?:audio|video)|lyric\s+video|official\s+lyric\s+video)[\])]",
    flags=re.IGNORECASE,
)
from api.lyrics.fingerprint import (
    TrackFingerprint,
    clean_song_title,
    create_track_fingerprint,
    normalize_artist_name,
    normalize_text,
    parse_artists,
)
from api.lyrics.verifier import LyricsVerifier, MIN_CONFIDENCE_THRESHOLD


def normalize_track_name(name: str) -> str:
    """Basic track name normalization preserving version markers for backward compatibility."""
    return _SPACE.sub(" ", _NOISE.sub("", name)).strip()


def get_primary_artist(artists: Any) -> str:
    primary, _ = parse_artists(artists)
    return primary


def get_all_artist_string(artists: Any) -> str:
    if isinstance(artists, list):
        names = []
        for a in artists:
            if isinstance(a, dict):
                names.append(str(a.get("name") or a.get("title") or ""))
            else:
                names.append(str(a))
        return ", ".join([n.strip() for n in names if n.strip()])
    return str(artists or "").strip()


def _candidate_score(candidate: dict[str, Any], track: dict[str, Any]) -> int:
    fingerprint = create_track_fingerprint(track) if not isinstance(track, TrackFingerprint) else track
    score, _ = LyricsVerifier.calculate_confidence(candidate, fingerprint)
    return score


class LRCLibProvider(LyricsProvider):
    BASE_URL = "https://lrclib.net/api"

    def __init__(self, session: aiohttp.ClientSession | None = None, user_agent: str = "MusicHub/1.0", timeout: float = 10.0):
        # Cloudflare on lrclib.net rejects requests containing X-Forwarded-For or CF-IPCountry headers with HTTP 503.
        # If a session with these headers is passed, create a clean dedicated session instead.
        self._owned_session = False
        if session is not None and getattr(session, "_default_headers", None):
            has_blocked_headers = any(
                k.lower() in ("x-forwarded-for", "cf-ipcountry")
                for k in session._default_headers.keys()
            )
            if has_blocked_headers:
                session = None

        if session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))
            self._owned_session = True
        else:
            self.session = session

        self.headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def close(self):
        if self._owned_session and self.session and not self.session.closed:
            await self.session.close()

    async def _request(self, path: str, params: dict[str, Any]) -> Any:
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                async with self.session.get(
                    f"{self.BASE_URL}/{path}",
                    params=params,
                    headers=self.headers,
                    timeout=self.timeout,
                ) as response:
                    if response.status == 404:
                        return None
                    if response.status == 429:
                        raw_retry = response.headers.get("Retry-After", "5")
                        try:
                            retry_after = max(1, int(raw_retry))
                        except ValueError:
                            retry_after = 5
                        raise LyricsRateLimited(retry_after)
                    if response.status in (500, 502, 503, 504) and attempt == 0:
                        await asyncio.sleep(0.3)
                        continue
                    response.raise_for_status()
                    return await response.json()
            except LyricsRateLimited:
                raise
            except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                last_exc = exc
                if attempt == 0:
                    await asyncio.sleep(0.3)
                    continue
                detail = f"{exc.__class__.__name__}: {exc}" if str(exc) else exc.__class__.__name__
                raise LyricsProviderError(f"Lyrics provider is unavailable ({detail})") from exc

        if last_exc:
            detail = f"{last_exc.__class__.__name__}: {last_exc}" if str(last_exc) else last_exc.__class__.__name__
            raise LyricsProviderError(f"Lyrics provider is unavailable ({detail})") from last_exc

    async def get_lyrics(self, track: dict[str, Any]) -> dict[str, Any] | None:
        fingerprint = create_track_fingerprint(track) if not isinstance(track, TrackFingerprint) else track
        title = fingerprint.title
        clean_title = fingerprint.clean_title
        artists = fingerprint.all_artists
        primary_artist = fingerprint.primary_artist
        duration = fingerprint.duration_seconds
        album = fingerprint.album

        # Step 1: Direct exact get endpoint with album and duration
        if title and (primary_artist or artists):
            search_art = primary_artist or artists
            if duration and album:
                try:
                    exact = await self._request("get", {
                        "track_name": title,
                        "artist_name": search_art,
                        "album_name": album,
                        "duration": duration,
                    })
                    if exact and (exact.get("syncedLyrics") or exact.get("plainLyrics") or exact.get("instrumental")):
                        is_verified, score, _ = LyricsVerifier.verify_candidate(exact, fingerprint)
                        if is_verified:
                            exact["_verification_score"] = score
                            exact["_verified"] = True
                            return exact
                except (LyricsProviderError, Exception):
                    pass

            if duration:
                try:
                    exact = await self._request("get", {
                        "track_name": title,
                        "artist_name": search_art,
                        "duration": duration,
                    })
                    if exact and (exact.get("syncedLyrics") or exact.get("plainLyrics") or exact.get("instrumental")):
                        is_verified, score, _ = LyricsVerifier.verify_candidate(exact, fingerprint)
                        if is_verified:
                            exact["_verification_score"] = score
                            exact["_verified"] = True
                            return exact
                except (LyricsProviderError, Exception):
                    pass

            if clean_title != title and primary_artist and duration:
                try:
                    exact = await self._request("get", {
                        "track_name": clean_title,
                        "artist_name": primary_artist,
                        "duration": duration,
                    })
                    if exact and (exact.get("syncedLyrics") or exact.get("plainLyrics") or exact.get("instrumental")):
                        is_verified, score, _ = LyricsVerifier.verify_candidate(exact, fingerprint)
                        if is_verified:
                            exact["_verification_score"] = score
                            exact["_verified"] = True
                            return exact
                except (LyricsProviderError, Exception):
                    pass

        # Step 2: Multi-query Search
        search_queries: list[dict[str, str]] = []
        if clean_title and primary_artist:
            search_queries.append({"track_name": clean_title, "artist_name": primary_artist})
            search_queries.append({"q": f"{clean_title} {primary_artist}"})
        if title and primary_artist and title != clean_title:
            search_queries.append({"track_name": title, "artist_name": primary_artist})
        if clean_title and album:
            search_queries.append({"track_name": clean_title, "album_name": album})
        if title and artists and artists != primary_artist:
            search_queries.append({"track_name": title, "artist_name": artists})
        if clean_title:
            search_queries.append({"q": clean_title})

        all_candidates: list[dict[str, Any]] = []
        seen_ids = set()
        last_provider_error: LyricsProviderError | None = None

        for query_params in search_queries:
            try:
                candidates = await self._request("search", query_params)
                if isinstance(candidates, list):
                    for cand in candidates:
                        cand_id = cand.get("id")
                        if cand_id and cand_id not in seen_ids:
                            seen_ids.add(cand_id)
                            all_candidates.append(cand)
            except LyricsRateLimited:
                raise
            except LyricsProviderError as exc:
                last_provider_error = exc
                continue
            except Exception:
                pass

        if not all_candidates:
            if last_provider_error and not seen_ids:
                raise last_provider_error
            return None

        # Filter candidates having lyrics content
        valid_candidates = [
            c for c in all_candidates
            if c.get("syncedLyrics") or c.get("plainLyrics") or c.get("instrumental")
        ]
        if not valid_candidates:
            return None

        # Verify and score every candidate against the fingerprint
        scored_candidates: list[tuple[dict[str, Any], int]] = []
        for cand in valid_candidates:
            is_verified, score, _ = LyricsVerifier.verify_candidate(cand, fingerprint)
            if is_verified:
                cand["_verification_score"] = score
                cand["_verified"] = True
                scored_candidates.append((cand, score))

        if not scored_candidates:
            # No candidate achieved verification threshold (score >= 850)
            return None

        # Select candidate with highest verified confidence score
        scored_candidates.sort(key=lambda item: item[1], reverse=True)
        best_candidate, best_score = scored_candidates[0]
        return best_candidate

