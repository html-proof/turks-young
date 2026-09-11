import asyncio
import base64
import json
import logging
import re
from typing import Any, Optional
from urllib.parse import quote
import aiohttp
from Crypto.Cipher import DES

logger = logging.getLogger(__name__)

_DES_KEY = b"38343638"


def decrypt_saavn_media_url(encrypted_url: str) -> str:
    """Decrypt a JioSaavn encrypted_media_url using standard DES-ECB."""
    if not encrypted_url or not isinstance(encrypted_url, str):
        return ""
    try:
        cipher = DES.new(_DES_KEY, DES.MODE_ECB)
        raw = base64.b64decode(encrypted_url.strip())
        dec = cipher.decrypt(raw)
        pad = dec[-1]
        if 1 <= pad <= 8:
            dec = dec[:-pad]
        dec_str = dec.decode("utf-8").strip()
        if dec_str.startswith("http://"):
            dec_str = "https://" + dec_str[7:]
        return dec_str
    except Exception as exc:
        logger.debug("Failed to decrypt saavn media url: %s", exc)
        return ""


def _normalize_tokens(text: str) -> set[str]:
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", text.lower())
    return {w for w in cleaned.split() if w}


class StreamFallbackResolver:
    """Provides fallback audio stream URLs for tracks when primary provider has no stream."""

    def __init__(self, session: Optional[aiohttp.ClientSession] = None, cache: Any = None):
        self._session = session
        self._cache = cache
        self._internal_session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            return self._session
        if self._internal_session is None or self._internal_session.closed:
            self._internal_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=4.0),
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                    "Accept": "application/json",
                },
            )
        return self._internal_session

    async def close(self) -> None:
        if self._internal_session and not self._internal_session.closed:
            await self._internal_session.close()

    def decrypt_url(self, encrypted_url: str) -> str:
        return decrypt_saavn_media_url(encrypted_url)

    def build_fallback_stream_urls(self, decrypted: str = "", preview_url: str = "") -> dict[str, str]:
        high_quality = ""
        medium_quality = ""
        low_quality = ""

        if decrypted:
            if re.search(r"_(?:96|160|320)\.mp4", decrypted):
                high_quality = re.sub(r"_(?:96|160|320)\.mp4", "_320.mp4", decrypted)
                medium_quality = re.sub(r"_(?:96|160|320)\.mp4", "_160.mp4", decrypted)
                low_quality = re.sub(r"_(?:96|160|320)\.mp4", "_96.mp4", decrypted)
            else:
                high_quality = decrypted
                medium_quality = decrypted
                low_quality = decrypted
        elif preview_url:
            high_quality = preview_url
            medium_quality = preview_url
            low_quality = preview_url

        primary_stream = high_quality or medium_quality or low_quality or preview_url
        return {
            "very_high_quality": high_quality,
            "high_quality": high_quality,
            "medium_quality": medium_quality,
            "low_quality": low_quality,
            "default": primary_stream,
            "raw": decrypted or preview_url,
        }

    async def get_song_by_id(self, pid: str) -> dict[str, Any]:
        """Fetch song details by JioSaavn song ID."""
        clean_pid = re.sub(r"^saavn[:-]?", "", pid.strip())
        if not clean_pid:
            return {}

        cache_key = f"stream_fallback:song_id:{clean_pid}"
        if self._cache:
            try:
                cached = await self._cache.get(cache_key)
                if cached:
                    if isinstance(cached, str):
                        cached = json.loads(cached)
                    return cached
            except Exception:
                pass

        details_url = f"https://www.jiosaavn.com/api.php?__call=song.getDetails&pids={quote(clean_pid)}&_format=json&ctx=android"
        try:
            session = await self._get_session()
            async with session.get(details_url) as resp:
                if resp.status != 200:
                    return {}
                try:
                    data = await resp.json()
                except Exception:
                    text = await resp.text()
                    try:
                        data = json.loads(text)
                    except Exception:
                        return {}

            item = None
            if isinstance(data, dict):
                if clean_pid in data and isinstance(data[clean_pid], dict):
                    item = data[clean_pid]
                elif "songs" in data and isinstance(data["songs"], list) and data["songs"]:
                    item = data["songs"][0]
                elif "data" in data and isinstance(data["data"], dict):
                    item = data["data"].get(clean_pid) or (data["data"].get("songs", [None])[0] if isinstance(data["data"].get("songs"), list) else None)

            if not item or not isinstance(item, dict):
                return {}

            song_title = item.get("song") or item.get("title") or ""
            primary_artists = item.get("primary_artists") or item.get("singers") or ""
            encrypted_url = item.get("encrypted_media_url") or ""
            decrypted = decrypt_saavn_media_url(encrypted_url) if encrypted_url else ""
            preview_url = item.get("media_preview_url") or ""
            if preview_url.startswith("http://"):
                preview_url = "https://" + preview_url[7:]

            stream_dict = self.build_fallback_stream_urls(decrypted=decrypted, preview_url=preview_url)
            primary_stream = stream_dict.get("default") or stream_dict.get("high_quality") or ""
            if not primary_stream:
                return {}

            img_url = item.get("image") or ""
            if img_url.startswith("http://"):
                img_url = "https://" + img_url[7:]
            large_artwork = re.sub(r"[-_](?:50x50|80x80|150x150|250x250|320x320)", "-500x500", img_url)

            result_dict = {
                "id": f"saavn:{clean_pid}",
                "track_id": f"saavn:{clean_pid}",
                "seokey": f"saavn-{clean_pid}",
                "title": song_title,
                "artist": primary_artists,
                "artists": [{"name": a.strip()} for a in primary_artists.split(",") if a.strip()],
                "album": item.get("album") or "",
                "duration": str(item.get("duration") or "180"),
                "image_url": large_artwork,
                "artworkUrl": large_artwork,
                "images": {
                    "urls": {
                        "large_artwork": large_artwork,
                        "medium_artwork": img_url,
                        "small_artwork": img_url,
                    }
                },
                "stream_url": primary_stream,
                "stream_urls": {
                    "urls": stream_dict
                },
                "fallback_source": "jiosaavn",
            }

            if self._cache:
                try:
                    await self._cache.set(cache_key, result_dict, 86400)
                except Exception:
                    pass

            return result_dict
        except Exception as exc:
            logger.warning("Stream fallback get_song_by_id failed for pid='%s': %s", clean_pid, exc)
            return {}

    async def resolve_stream(self, title: str, artist: str = "") -> dict[str, Any]:
        """Search secondary open provider (JioSaavn) for a matching playable stream."""
        clean_title = (title or "").strip()
        if not clean_title:
            return {}

        if clean_title.startswith("saavn:") or clean_title.startswith("saavn-"):
            by_id = await self.get_song_by_id(clean_title)
            if by_id:
                return by_id

        if clean_title.lower().startswith("saavn "):
            clean_title = clean_title[6:].strip()
            if not clean_title:
                return {}

        cache_key = f"stream_fallback:{clean_title.lower()}:{artist.lower().strip()}"
        if self._cache:
            try:
                cached = await self._cache.get(cache_key)
                if cached:
                    if isinstance(cached, str):
                        cached = json.loads(cached)
                    return cached
            except Exception:
                pass

        query_terms = f"{clean_title} {artist}".strip()
        encoded = quote(query_terms, safe="")
        search_url = f"https://www.jiosaavn.com/api.php?__call=search.getResults&_format=json&n=5&p=1&_marker=0&ctx=android&q={encoded}"

        try:
            session = await self._get_session()
            async with session.get(search_url) as resp:
                if resp.status != 200:
                    return {}
                try:
                    data = await resp.json()
                except Exception:
                    text = await resp.text()
                    try:
                        data = json.loads(text)
                    except Exception:
                        return {}

            results = data.get("results") or (data.get("data", {}).get("results") if isinstance(data.get("data"), dict) else [])
            if not isinstance(results, list) or not results:
                return {}

            target_tokens = _normalize_tokens(clean_title)
            best_item = None
            best_score = -1

            for item in results:
                if not isinstance(item, dict):
                    continue
                item_title = item.get("song") or item.get("title") or ""
                item_tokens = _normalize_tokens(item_title)
                if not target_tokens:
                    continue
                overlap = len(target_tokens & item_tokens) / max(len(target_tokens), 1)
                if overlap > best_score and overlap >= 0.5:
                    best_score = overlap
                    best_item = item

            if not best_item:
                best_item = results[0]

            encrypted_url = best_item.get("encrypted_media_url") or ""
            decrypted = decrypt_saavn_media_url(encrypted_url) if encrypted_url else ""
            preview_url = best_item.get("media_preview_url") or ""
            if preview_url.startswith("http://"):
                preview_url = "https://" + preview_url[7:]

            stream_dict = self.build_fallback_stream_urls(decrypted=decrypted, preview_url=preview_url)
            primary_stream = stream_dict.get("default") or stream_dict.get("high_quality") or ""
            if not primary_stream:
                return {}

            result_dict = {
                "stream_url": primary_stream,
                "stream_urls": {
                    "urls": stream_dict
                },
                "fallback_source": "jiosaavn",
            }

            if self._cache:
                try:
                    await self._cache.set(cache_key, result_dict, 86400)
                except Exception:
                    pass

            return result_dict
        except Exception as exc:
            logger.warning("Stream fallback lookup failed for query='%s': %s", query_terms, exc)
            return {}

    async def search_tracks(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search secondary open provider (JioSaavn) for playable tracks when primary provider is unavailable."""
        clean_q = (query or "").strip()
        if not clean_q:
            return []

        cache_key = f"stream_fallback:search:{clean_q.lower()}:{limit}"
        if self._cache:
            try:
                cached = await self._cache.get(cache_key)
                if cached:
                    if isinstance(cached, str):
                        cached = json.loads(cached)
                    return cached
            except Exception:
                pass

        encoded = quote(clean_q, safe="")
        search_url = f"https://www.jiosaavn.com/api.php?__call=search.getResults&_format=json&n={limit}&p=1&_marker=0&ctx=android&q={encoded}"

        try:
            session = await self._get_session()
            async with session.get(search_url) as resp:
                if resp.status != 200:
                    return []
                try:
                    data = await resp.json()
                except Exception:
                    text = await resp.text()
                    try:
                        data = json.loads(text)
                    except Exception:
                        return []

            results = data.get("results") or (data.get("data", {}).get("results") if isinstance(data.get("data"), dict) else [])
            if not isinstance(results, list) or not results:
                return []

            tracks = []
            for item in results:
                if not isinstance(item, dict):
                    continue
                song_title = item.get("song") or item.get("title") or ""
                if not song_title:
                    continue

                primary_artists = item.get("primary_artists") or item.get("singers") or ""
                encrypted_url = item.get("encrypted_media_url") or ""
                decrypted = decrypt_saavn_media_url(encrypted_url) if encrypted_url else ""
                preview_url = item.get("media_preview_url") or ""
                if preview_url.startswith("http://"):
                    preview_url = "https://" + preview_url[7:]

                stream_dict = self.build_fallback_stream_urls(decrypted=decrypted, preview_url=preview_url)
                primary_stream = stream_dict.get("default") or stream_dict.get("high_quality") or ""

                img_url = item.get("image") or ""
                if img_url.startswith("http://"):
                    img_url = "https://" + img_url[7:]
                large_artwork = re.sub(r"[-_](?:50x50|80x80|150x150|250x250|320x320)", "-500x500", img_url)

                track_dict = {
                    "id": f"saavn:{item.get('id')}",
                    "track_id": f"saavn:{item.get('id')}",
                    "seokey": f"saavn-{item.get('id')}",
                    "title": song_title,
                    "artist": primary_artists,
                    "artists": [{"name": a.strip()} for a in primary_artists.split(",") if a.strip()],
                    "album": item.get("album") or "",
                    "duration": str(item.get("duration") or "180"),
                    "image_url": large_artwork,
                    "artworkUrl": large_artwork,
                    "images": {
                        "urls": {
                            "large_artwork": large_artwork,
                            "medium_artwork": img_url,
                            "small_artwork": img_url,
                        }
                    },
                    "stream_url": primary_stream,
                    "stream_urls": {
                        "urls": stream_dict
                    },
                    "source": "jiosaavn_fallback",
                }
                tracks.append(track_dict)

            if self._cache and tracks:
                try:
                    await self._cache.set(cache_key, tracks, 3600)
                except Exception:
                    pass

            return tracks
        except Exception as exc:
            logger.warning("Stream fallback search failed for query='%s': %s", clean_q, exc)
            return []


_singleton_resolver: Optional[StreamFallbackResolver] = None


def get_stream_fallback_resolver() -> StreamFallbackResolver:
    global _singleton_resolver
    if _singleton_resolver is None:
        _singleton_resolver = StreamFallbackResolver()
    return _singleton_resolver


def set_stream_fallback_resolver(resolver: StreamFallbackResolver) -> None:
    global _singleton_resolver
    _singleton_resolver = resolver
