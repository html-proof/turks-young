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
    value = value.strip().replace("&amp;", "&")
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
        if url.scheme == "http" and url.hostname.endswith((".saavncdn.com", ".gaanacdn.com")):
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
            for key in (*FIELDS, "urls", "large_artwork", "large", "medium_artwork",
                        "medium", "small_artwork", "small", "url", "link", "album"):
                visit(item.get(key), depth + 1)

    visit(value)
    return result
