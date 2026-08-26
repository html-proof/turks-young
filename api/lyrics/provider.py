import abc
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
    r"\s*[\[(](?:official\s+(?:audio|video)|lyric\s+video)[\])]",
    flags=re.IGNORECASE,
)
_SPACE = re.compile(r"\s+")


def normalize_track_name(name: str) -> str:
    return _SPACE.sub(" ", _NOISE.sub("", name)).strip()


def _comparable(value: Any) -> str:
    value = normalize_track_name(str(value or "")).casefold()
    return _SPACE.sub(" ", re.sub(r"[^\w]+", " ", value)).strip()


def _similarity(left: Any, right: Any) -> float:
    return SequenceMatcher(None, _comparable(left), _comparable(right)).ratio()


def _candidate_score(candidate: dict[str, Any], track: dict[str, Any]) -> int:
    score = round(40 * _similarity(candidate.get("trackName"), track.get("title")))
    score += round(40 * _similarity(candidate.get("artistName"), track.get("artists")))
    if track.get("album") and candidate.get("albumName"):
        score += round(10 * _similarity(candidate.get("albumName"), track.get("album")))
    candidate_duration = candidate.get("duration")
    duration = track.get("duration_seconds")
    if candidate_duration is not None and duration is not None:
        difference = abs(float(candidate_duration) - float(duration))
        if difference <= 2:
            score += 10
        elif difference <= 5:
            score += 5
    return score


class LRCLibProvider(LyricsProvider):
    BASE_URL = "https://lrclib.net/api"

    def __init__(self, session: aiohttp.ClientSession, user_agent: str, timeout: float = 10.0):
        self.session = session
        self.headers = {"User-Agent": user_agent}
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def _request(self, path: str, params: dict[str, Any]) -> Any:
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
                response.raise_for_status()
                return await response.json()
        except LyricsRateLimited:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise LyricsProviderError("Lyrics provider is unavailable") from exc

    async def get_lyrics(self, track: dict[str, Any]) -> dict[str, Any] | None:
        base_params = {
            "track_name": normalize_track_name(track["title"]),
            "artist_name": track["artists"],
        }
        duration = track.get("duration_seconds")
        exact_params = {**base_params, "duration": duration} if duration else None
        if exact_params and track.get("album"):
            exact = await self._request("get", {**exact_params, "album_name": track["album"]})
            if exact:
                return exact

        if exact_params:
            exact = await self._request("get", exact_params)
            if exact:
                return exact

        candidates = await self._request("search", {
            "track_name": base_params["track_name"],
            "artist_name": base_params["artist_name"],
        })
        if not isinstance(candidates, list):
            return None
        ranked = sorted(candidates, key=lambda item: _candidate_score(item, track), reverse=True)
        if not ranked or _candidate_score(ranked[0], track) < 80:
            return None
        return ranked[0]
