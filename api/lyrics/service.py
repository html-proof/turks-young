from typing import Any

from api.lyrics.lrc import parse_lrc
from api.lyrics.provider import LyricsProvider


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
    ):
        self.provider = provider
        self.repository = repository
        self.success_ttl = success_ttl
        self.not_found_ttl = not_found_ttl

    def _response(self, track_id: str, data: dict[str, Any], duration: int) -> dict[str, Any]:
        status = data.get("status", "available")
        instrumental = bool(data.get("instrumental"))
        synced_lyrics = data.get("syncedLyrics") or data.get("synced_lyrics")
        plain_lyrics = data.get("plainLyrics") or data.get("plain_lyrics")
        provider_id = data.get("id") or data.get("provider_lyrics_id")
        lines = parse_lrc(synced_lyrics, duration * 1000) if status == "available" else []
        return {
            "trackId": track_id,
            "provider": data.get("provider", "lrclib") if status != "not_found" else None,
            "providerId": str(provider_id) if provider_id is not None else None,
            "status": status,
            "synced": bool(lines),
            "instrumental": instrumental,
            "plainLyrics": plain_lyrics,
            "lines": lines,
        }

    async def get_lyrics(self, track: dict[str, Any]) -> dict[str, Any]:
        track_id = str(track["seokey"])
        duration = duration_seconds(track.get("duration"))

        cached = await self.repository.get(track_id, self.success_ttl, None)
        if cached and cached.get("status") != "not_found":
            return self._response(track_id, cached, duration)
        cached = await self.repository.get(track_id, self.not_found_ttl, "not_found")
        if cached:
            return self._response(track_id, cached, duration)

        provider_track = {
            "title": track.get("title", ""),
            "artists": track.get("artists", ""),
            "album": track.get("album", ""),
            "duration_seconds": duration,
        }
        result = await self.provider.get_lyrics(provider_track)
        if result is None:
            result = {"provider": "lrclib", "status": "not_found", "instrumental": False}
        else:
            result = {**result, "provider": "lrclib"}
            result["status"] = "instrumental" if result.get("instrumental") else "available"
        await self.repository.put(track_id, result)
        return self._response(track_id, result, duration)
