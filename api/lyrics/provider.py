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


_NOISE = re.compile(
    r"\s*[\[(](?:official\s+(?:audio|video)|lyric\s+video|official\s+lyric\s+video)[\])]",
    flags=re.IGNORECASE,
)
_SPACE = re.compile(r"\s+")


def normalize_track_name(name: str) -> str:
    """Basic track name normalization preserving version markers for backward compatibility."""
    return _SPACE.sub(" ", _NOISE.sub("", name)).strip()


def clean_song_title(title: str) -> str:
    """Aggressively clean movie names, version suffixes, and noisy bracketed tags for search matching."""
    cleaned = str(title or "").strip()
    patterns = [
        # (From "Movie"), [From "Movie"], (From Movie), [From The Film ...]
        r"\s*[\(\[](?:from\s+[\"']?[^\)\]]+[\"']?|from\s+the\s+(?:movie|film)\s+[\"']?[^\)\]]+[\"']?)[\]\)]",
        # (feat. ...), (ft. ...)
        r"\s*[\(\[](?:feat\.?|ft\.?)[^\)\]]+[\]\)]",
        # (Official ...), (Lyric ...), (Audio), (Video), (Full Song), (Lyrical)
        r"\s*[\(\[](?:official[^\)\]]*|lyric[^\)\]]*|audio|video|full\s+song|lyrical[^\)\]]*|song)[\]\)]",
        # (Original Motion Picture Soundtrack), (Soundtrack), (OST)
        r"\s*[\(\[](?:original\s+(?:motion\s+picture\s+)?soundtrack|soundtrack|ost)[\]\)]",
        # [Malayalam], (Telugu), (Tamil), (Hindi), (Kannada), (Punjabi), (English)
        r"\s*[\(\[](?:malayalam|telugu|tamil|hindi|kannada|punjabi|bengali|marathi|english|arabic|instrumental)[\]\)]",
        # - From "Movie" / - Reprise / - Remix / - Title Track / - Lyrical
        r"\s*-\s*(?:from\s+[^\-]+|reprise|remix|title\s+track|official[^\-]*|lyrical[^\-]*|original\s+soundtrack|theme|promo|full\s+song).*",
    ]
    for p in patterns:
        cleaned = re.sub(p, "", cleaned, flags=re.IGNORECASE)
    cleaned = _SPACE.sub(" ", cleaned).strip()
    cleaned = re.sub(r"[\s\-_–—:]+$", "", cleaned).strip()
    return cleaned if cleaned else title


def get_primary_artist(artists: Any) -> str:
    if isinstance(artists, list):
        if not artists:
            return ""
        first = artists[0]
        if isinstance(first, dict):
            return str(first.get("name") or first.get("title") or "").strip()
        return str(first).strip()
    s = str(artists or "").strip()
    parts = re.split(r",|&|/|(?:\s+(?:feat\.?|ft\.?|with)\s+)", s, flags=re.IGNORECASE)
    return parts[0].strip() if parts else s


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


def _comparable(value: Any) -> str:
    value = normalize_track_name(str(value or "")).casefold()
    return _SPACE.sub(" ", re.sub(r"[^\w]+", " ", value)).strip()


def _similarity(left: Any, right: Any) -> float:
    l_str = _comparable(left)
    r_str = _comparable(right)
    if not l_str or not r_str:
        return 0.0
    if l_str == r_str:
        return 1.0
    return SequenceMatcher(None, l_str, r_str).ratio()


def _candidate_score(candidate: dict[str, Any], track: dict[str, Any]) -> int:
    cand_title = candidate.get("trackName") or ""
    cand_artist = candidate.get("artistName") or ""
    target_title = track.get("title") or ""
    target_clean = clean_song_title(target_title)
    target_artist = get_all_artist_string(track.get("artists"))
    primary_artist = get_primary_artist(target_artist)

    # 1. Title Similarity (0 to 45 pts)
    title_sim = max(
        _similarity(cand_title, target_title),
        _similarity(cand_title, target_clean),
        _similarity(clean_song_title(cand_title), target_clean),
    )
    score = round(45 * title_sim)

    # 2. Artist Similarity (0 to 35 pts)
    cand_art_comp = _comparable(cand_artist)
    prim_art_comp = _comparable(primary_artist)
    all_art_comp = _comparable(target_artist)
    
    is_contained = (
        (prim_art_comp and len(prim_art_comp) >= 3 and (prim_art_comp in cand_art_comp or cand_art_comp in prim_art_comp)) or
        (cand_art_comp and len(cand_art_comp) >= 3 and cand_art_comp in all_art_comp)
    )
    
    art_sim = max(
        _similarity(cand_artist, target_artist),
        _similarity(cand_artist, primary_artist),
        1.0 if is_contained else 0.0,
    )
    score += round(35 * art_sim)

    # 3. Album Match (0 to 10 pts)
    if track.get("album") and candidate.get("albumName"):
        score += round(10 * _similarity(candidate.get("albumName"), track.get("album")))

    # 4. Duration match (0 to 10 pts)
    cand_dur = candidate.get("duration")
    target_dur = track.get("duration_seconds")
    if cand_dur is not None and target_dur and float(target_dur) > 0:
        diff = abs(float(cand_dur) - float(target_dur))
        if diff <= 3:
            score += 10
        elif diff <= 8:
            score += 5
        elif diff > 35:
            score -= 15

    # 5. Synced lyrics bonus (+5 pts)
    if candidate.get("syncedLyrics"):
        score += 5

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
        title = track.get("title", "").strip()
        artists = get_all_artist_string(track.get("artists"))
        primary_artist = get_primary_artist(artists)
        clean_title = clean_song_title(title)
        duration = track.get("duration_seconds")
        album = str(track.get("album") or "").strip()

        # Step 1: Direct exact get endpoint with album and duration
        if title and artists:
            if duration and album:
                try:
                    exact = await self._request("get", {
                        "track_name": normalize_track_name(title),
                        "artist_name": artists,
                        "album_name": album,
                        "duration": duration,
                    })
                    if exact and (exact.get("syncedLyrics") or exact.get("plainLyrics") or exact.get("instrumental")):
                        return exact
                except (LyricsProviderError, Exception):
                    pass

            if duration:
                try:
                    exact = await self._request("get", {
                        "track_name": normalize_track_name(title),
                        "artist_name": artists,
                        "duration": duration,
                    })
                    if exact and (exact.get("syncedLyrics") or exact.get("plainLyrics") or exact.get("instrumental")):
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
                        return exact
                except (LyricsProviderError, Exception):
                    pass

        # Step 2: Multi-query Search
        search_queries: list[dict[str, str]] = []
        if clean_title and primary_artist:
            search_queries.append({"track_name": clean_title, "artist_name": primary_artist})
            search_queries.append({"q": f"{clean_title} {primary_artist}"})
        if title and artists and (title != clean_title or artists != primary_artist):
            search_queries.append({"track_name": normalize_track_name(title), "artist_name": artists})
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
                if all_candidates:
                    best = max(all_candidates, key=lambda item: _candidate_score(item, track))
                    if _candidate_score(best, track) >= 75 and best.get("syncedLyrics"):
                        return best
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

        # Filter and rank candidates
        valid_candidates = [
            c for c in all_candidates
            if c.get("syncedLyrics") or c.get("plainLyrics") or c.get("instrumental")
        ]
        if not valid_candidates:
            return None

        ranked = sorted(valid_candidates, key=lambda item: _candidate_score(item, track), reverse=True)
        best = ranked[0]
        score = _candidate_score(best, track)

        min_threshold = 40 if best.get("syncedLyrics") else 45
        if score < min_threshold:
            return None
        return best

