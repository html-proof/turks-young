import re
from typing import Any

from api.lyrics.service import duration_seconds


def _upgrade_image_quality(url: str | None) -> str | None:
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url:
        return None
    if url.startswith("http://"):
        url = "https://" + url[7:]
    # Upgrade low-res thumbnail dimensions to 500x500 HD
    url = re.sub(r'[-_](?:50x50|80x80|150x150|250x250|320x320)(\.[a-zA-Z0-9]+)$', r'-500x500\1', url)
    return url


def _csv(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _image(item: dict[str, Any]) -> str | None:
    raw: str | None = None
    for key in (
        "imageUrl", "image_url", "thumbnail", "photo", "artist_image",
        "artworkUrl", "artwork", "artwork_large", "artwork_web",
        "artwork_medium", "album_artwork", "atw",
    ):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            raw = value.strip()
            break
    if not raw:
        direct_image = item.get("image")
        if isinstance(direct_image, str) and direct_image.strip():
            raw = direct_image.strip()
        elif isinstance(direct_image, dict):
            for key in ("large", "medium", "small", "url"):
                value = direct_image.get(key)
                if isinstance(value, str) and value.strip():
                    raw = value.strip()
                    break
    if not raw:
        urls = (item.get("images") or {}).get("urls") or {}
        raw = (
            urls.get("large_artwork") or urls.get("medium_artwork")
            or urls.get("small_artwork") or urls.get("large")
            or urls.get("medium") or urls.get("small") or None
        )
    return _upgrade_image_quality(raw)


def _images(item: dict[str, Any]) -> dict[str, str]:
    urls = (item.get("images") or {}).get("urls") or {}
    fallback = _image(item) or ""
    small = _upgrade_image_quality(urls.get("small_artwork") or urls.get("small")) or fallback
    medium = _upgrade_image_quality(urls.get("medium_artwork") or urls.get("medium")) or fallback
    large = _upgrade_image_quality(urls.get("large_artwork") or urls.get("large")) or fallback
    return {
        "small": small,
        "medium": medium,
        "large": large,
    }


def _song_artwork(item: dict[str, Any]) -> str | None:
    """Return verified track/album artwork without using an artist portrait."""
    for key in (
        "artwork_large", "artwork_web", "artwork_medium", "artwork",
        "album_artwork", "artworkUrl",
    ):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return _upgrade_image_quality(value.strip())
    album_value = item.get("album")
    if isinstance(album_value, dict):
        for key in ("artworkUrl", "imageUrl", "image_url", "artwork"):
            value = album_value.get(key)
            if isinstance(value, str) and value.strip():
                return _upgrade_image_quality(value.strip())
    urls = (item.get("images") or {}).get("urls") or {}
    for key in (
        "large_artwork", "medium_artwork", "small_artwork",
        "large", "medium", "small",
    ):
        value = urls.get(key)
        if isinstance(value, str) and value.strip():
            return _upgrade_image_quality(value.strip())
    artist_image = str(item.get("artist_image") or "").strip()
    for key in ("imageUrl", "image_url", "thumbnail"):
        value = item.get(key)
        if isinstance(value, str) and value.strip() and value.strip() != artist_image:
            return _upgrade_image_quality(value.strip())
    return None


def _int(value: Any) -> int:
    try:
        return int(float(str(value or 0).replace(",", "")))
    except ValueError:
        return 0


def artist(item: dict[str, Any]) -> dict[str, Any]:
    images = _images(item)
    image_url = _image(item) or images["large"] or images["medium"] or images["small"]
    return {
        "id": str(item.get("seokey") or item.get("id") or ""),
        "provider_id": str(item.get("artist_id") or item.get("provider_id") or ""),
        "type": "artist",
        "name": str(item.get("name") or ""),
        "image_url": image_url,
        "imageUrl": image_url,
        "image": {key: value for key, value in images.items() if value},
        "image_status": "verified" if image_url else "placeholder",
        "verified": bool(item.get("verified", False)),
        "followers_count": _int(item.get("favorite_count") or item.get("followers_count")),
    }


def song(item: dict[str, Any]) -> dict[str, Any]:
    names = _csv(item.get("artists"))
    ids = _csv(item.get("artist_seokeys") or item.get("artist_ids"))
    artists = [
        {"id": ids[index] if index < len(ids) else "", "name": name, "type": "artist"}
        for index, name in enumerate(names)
    ]
    streams = (item.get("stream_urls") or {}).get("urls") or {}
    # Catalog records may already be normalized and carry a single playable
    # URL instead of the provider's quality map. Preserve it when present so
    # album tracklists do not become metadata-only tracks.
    direct_stream = item.get("stream_url") or item.get("streamUrl") or ""
    seconds = duration_seconds(item.get("duration"))
    # Provider song records expose artist_image alongside the actual album
    # artwork. Prefer the track/album artwork fields so every song keeps its
    # own cover instead of inheriting an artist portrait.
    artwork_url = _song_artwork(item)
    album_id = str(item.get("album_seokey") or item.get("album_id") or "")
    album_title = str(item.get("album") or "")
    return {
        "id": str(item.get("seokey") or item.get("id") or ""),
        "provider_id": str(item.get("track_id") or item.get("provider_id") or ""),
        "type": "song",
        "title": str(item.get("title") or ""),
        "artist": artists[0] if artists else None,
        "artists": artists,
        "album": {
            "id": album_id,
            "provider_id": str(item.get("album_id") or ""),
            "name": album_title,
            "title": album_title,
            "artworkUrl": artwork_url,
            "type": "album",
        } if album_id or album_title else None,
        "image_url": artwork_url,
        "artworkUrl": artwork_url,
        "duration_ms": seconds * 1000,
        "durationMs": seconds * 1000,
        "language": str(item.get("language") or ""),
        "label": str(item.get("label") or ""),
        "release_date": item.get("release_date") or None,
        "explicit": bool(item.get("is_explicit", False)),
        "stream_url": (
            streams.get("very_high_quality") or streams.get("high_quality")
            or streams.get("medium_quality") or streams.get("low_quality")
            or direct_stream or None
        ),
        "lyrics_url": f"/api/v1/tracks/{item.get('seokey')}/lyrics" if item.get("seokey") else None,
    }


def album(item: dict[str, Any]) -> dict[str, Any]:
    names = _csv(item.get("artists"))
    ids = _csv(item.get("artist_seokeys") or item.get("artist_ids"))
    title = str(item.get("title") or item.get("name") or "")
    artwork_url = _image(item)
    release_date = item.get("release_date") or None
    release_year = None
    if release_date:
        try:
            release_year = int(str(release_date)[:4])
        except ValueError:
            release_year = None
    artist_values = [
        {"id": ids[index] if index < len(ids) else "", "name": name, "type": "artist"}
        for index, name in enumerate(names)
    ]
    track_count = _int(item.get("track_count") or item.get("song_count"))
    data = {
        "id": str(item.get("seokey") or item.get("id") or ""),
        "provider_id": str(item.get("album_id") or item.get("provider_id") or ""),
        "type": "album",
        "name": title,
        "title": title,
        "image_url": artwork_url,
        "imageUrl": artwork_url,
        "artworkUrl": artwork_url,
        "artists": artist_values,
        "artistNames": names,
        "artistIds": ids,
        "release_date": release_date,
        "releaseYear": release_year,
        "language": str(item.get("language") or ""),
        "label": str(item.get("label") or ""),
        "song_count": track_count,
        "trackCount": track_count,
    }
    if "tracks" in item:
        tracks = item.get("tracks") if isinstance(item.get("tracks"), list) else []
        data["songs"] = [song(track) for track in tracks if isinstance(track, dict)]
        data["tracks"] = data["songs"]
    return data


def playlist(item: dict[str, Any]) -> dict[str, Any]:
    artwork = item.get("image_url") or _image(item)
    tracks = item.get("tracks") if isinstance(item.get("tracks"), list) else []
    return {
        "id": str(item.get("seokey") or item.get("id") or ""),
        "type": "playlist",
        "name": str(item.get("title") or item.get("name") or ""),
        "description": str(item.get("description") or ""),
        "owner": item.get("owner") or None,
        "image_url": artwork,
        "song_count": _int(item.get("track_count") or len(tracks)),
        "songs": [song(track) for track in tracks if isinstance(track, dict)],
    }


NORMALIZERS = {"song": song, "artist": artist, "album": album, "playlist": playlist}


def items(value: Any, kind: str) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get(f"{kind}s") or []
    if not isinstance(value, list):
        return []
    normalize = NORMALIZERS[kind]
    return [
        normalized
        for raw in value
        if isinstance(raw, dict)
        if (normalized := normalize(raw))["id"]
        and (normalized.get("title") or normalized.get("name"))
    ]
