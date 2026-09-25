"""Artwork metadata only: no network I/O or speculative CDN URL rewrites."""
from urllib.parse import urlsplit, urlunsplit, urljoin
import re

FIELDS = (
    "imageUrl", "image_url", "artworkUrl", "artwork_url", "artwork_candidates",
    "artwork", "image", "images", "cover", "cover_url", "cover_image",
    "album_art", "albumArt", "album_image", "album_artwork", "artwork_large",
    "artwork_web", "artwork_medium", "thumbnail", "photo", "atw",
)


def normalize_url(value, provider_base=None):
    if not isinstance(value, str):
        return None
    value = value.strip().replace("&amp;", "&").replace(" ", "%20")
    if value.startswith("//"):
        value = "https:" + value
    elif value.startswith("/") and provider_base:
        value = urljoin(provider_base, value)
    try:
        url = urlsplit(value)
        if url.scheme not in ("http", "https") or not url.hostname or url.username:
            return None
        if re.search(r"\s", value):
            return None
        path = url.path.lower()
        if any(token in path for token in ("placeholder", "artist-default", "default-album")) or path.endswith(".html"):
            return None
        if url.scheme == "http":
            # Mobile clients block cleartext traffic; every artwork CDN we
            # receive from the provider also serves the same path over TLS.
            url = url._replace(scheme="https")
        return urlunsplit(url)
    except ValueError:
        return None


def artwork_candidates(value, provider_base=None):
    result = []

    def size(item):
        if not isinstance(item, dict):
            return 0
        match = re.match(r"\d+", str(item.get("width") or item.get("quality") or ""))
        return int(match[0]) if match else 0

    def visit(item, depth=0):
        if depth > 8:
            return
        url = normalize_url(item, provider_base)
        if url:
            if url not in result:
                result.append(url)
        elif isinstance(item, list):
            for child in sorted(item, key=size, reverse=True):
                visit(child, depth + 1)
        elif isinstance(item, dict):
            for key in ("high", "original", "artwork_large", "large_artwork", "large",
                        "medium", "medium_artwork", "thumbnail", "small_artwork", "small",
                        *FIELDS, "urls", "url", "link", "album"):
                visit(item.get(key), depth + 1)

    visit(value)
    return result


def with_upgraded_candidates(candidates, upgrade):
    """Place a higher-resolution guess before each URL without dropping it.

    Size upgrades are string rewrites of provider CDN paths and are not
    guaranteed to exist, so the original URL always stays in the list as the
    next fallback.
    """
    result = []
    for url in candidates:
        upgraded = normalize_url(upgrade(url)) if url else None
        for candidate in (upgraded, url):
            if candidate and candidate not in result:
                result.append(candidate)
    return result


def artwork_cache_key(track_id: str, provider: str = "gaana") -> str:
    return f"artwork:v2:{provider}:{track_id}"


async def validate_artwork_url(client: Any, url: str) -> bool:
    """Follow redirects and verify status 200 with an image/* content type."""
    if not url:
        return False
    try:
        response = await client.head(url, follow_redirects=True, timeout=3.0)
        content_type = response.headers.get("content-type", "").lower()
        if response.status_code == 200 and "image" in content_type:
            return True
        # If HEAD returned 405 Method Not Allowed or 403, probe with a tiny GET
        if response.status_code in (403, 405):
            async with client.stream(
                "GET",
                url,
                headers={"Range": "bytes=0-1023"},
                follow_redirects=True,
                timeout=3.0,
            ) as get_resp:
                c_type = get_resp.headers.get("content-type", "").lower()
                return get_resp.status_code in (200, 206) and "image" in c_type
    except Exception:
        pass
    return False


import logging
logger = logging.getLogger(__name__)

ARTWORK_TTL_SECONDS = 7 * 86400  # 7 days


class ArtworkResolver:
    """Centralized artwork resolver following canonical priority order:
    1. Track high-resolution artwork
    2. Album artwork
    3. Source-specific track lookup
    4. Source-specific album lookup
    5. Alternate provider artwork
    6. Artist image
    7. Generated placeholder
    """

    def __init__(self, cache: Any = None, http_client: Any = None, catalog: Any = None) -> None:
        self.cache = cache
        self.http_client = http_client
        self.catalog = catalog

    async def resolve(
        self,
        track_or_id: dict[str, Any] | str,
        *,
        refresh: bool = False,
        client: Any = None,
    ) -> dict[str, Any]:
        track_id = ""
        track_data: dict[str, Any] = {}
        if isinstance(track_or_id, str):
            track_id = track_or_id.strip()
            track_data = {"track_id": track_id, "id": track_id}
        elif isinstance(track_or_id, dict):
            track_data = track_or_id
            track_id = str(
                track_data.get("track_id")
                or track_data.get("seokey")
                or track_data.get("id")
                or ""
            ).strip()

        provider = str(track_data.get("source") or "gaana").strip()
        cache_key = artwork_cache_key(track_id, provider)

        if self.cache and hasattr(self.cache, "get") and not refresh and track_id:
            try:
                cached = await self.cache.get(cache_key)
                if isinstance(cached, dict) and cached.get("artwork_url"):
                    return cached
            except Exception:
                pass

        # Collect candidate sources in strict canonical priority order:
        # 1. Track high-resolution artwork
        # 2. Album artwork
        # 3. Source-specific track/album lookup
        # 4. Alternate provider artwork
        # 5. Artist image
        # 6. Generated placeholder
        candidates: list[tuple[str, str]] = []  # (source_kind, url)

        # 1. Track high-resolution image (excluding album and artist_image)
        track_cover_data = {
            k: v for k, v in track_data.items()
            if k not in ("album", "artist_image", "artistImage", "artist_photo", "artistPhoto")
        }
        for url in artwork_candidates(track_cover_data):
            candidates.append(("track_high", url))

        # 2. Album artwork
        album_data = track_data.get("album")
        if isinstance(album_data, dict):
            for url in artwork_candidates(album_data):
                candidates.append(("album", url))
        elif isinstance(album_data, str) and (album_data.startswith("http://") or album_data.startswith("https://") or album_data.startswith("//")):
            norm_alb = normalize_url(album_data)
            if norm_alb:
                candidates.append(("album", norm_alb))

        # 3. Source-specific track/album lookup if catalog is available
        album_id = str(track_data.get("album_id") or track_data.get("albumId") or "").strip()
        if not candidates and album_id and self.catalog and hasattr(self.catalog, "get_album_info"):
            try:
                album_info = await self.catalog.get_album_info([album_id], False)
                if isinstance(album_info, list) and album_info:
                    for url in artwork_candidates(album_info[0]):
                        candidates.append(("album_lookup", url))
            except Exception:
                pass

        # 4. Alternate provider artwork
        alt_sources = track_data.get("alternate_sources") or []
        for alt in alt_sources:
            if isinstance(alt, dict) and "artwork" in alt:
                for url in artwork_candidates(alt["artwork"]):
                    candidates.append(("alternate_provider", url))

        # 5. Artist image (only as fallback cover, never replacing actual track/album artwork)
        artist_img = normalize_url(track_data.get("artist_image") or track_data.get("artistImage"))
        if artist_img:
            candidates.append(("artist", artist_img))

        # Filter out empty or duplicate URLs while preserving source priority
        seen: set[str] = set()
        ordered_candidates: list[tuple[str, str]] = []
        for src, url in candidates:
            if url and url not in seen:
                seen.add(url)
                ordered_candidates.append((src, url))

        chosen_url: str | None = None
        chosen_source: str = "placeholder"

        if ordered_candidates:
            # First candidate is primary
            primary_src, primary_url = ordered_candidates[0]
            chosen_url = primary_url
            chosen_source = primary_src
            if len(ordered_candidates) > 1:
                logger.info(
                    "ARTWORK_FALLBACK_SUCCESS track_id=%s source=%s url=%s",
                    track_id, primary_src, primary_url,
                )
        else:
            logger.warning("ARTWORK_PRIMARY_FAILED track_id=%s reason=no_candidates", track_id)

        result = {
            "track_id": track_id,
            "artwork_url": chosen_url or "",
            "source": chosen_source,
            "candidates": [url for _, url in ordered_candidates],
        }

        if self.cache and hasattr(self.cache, "set") and chosen_url and track_id:
            try:
                await self.cache.set(cache_key, result, ARTWORK_TTL_SECONDS)
            except Exception:
                pass

        return result

