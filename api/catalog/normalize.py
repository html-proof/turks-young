from typing import Any

from api.lyrics.service import duration_seconds


def _csv(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _image(item: dict[str, Any]) -> str | None:
    for key in ("imageUrl", "image_url", "thumbnail", "photo", "artist_image", "artworkUrl", "artwork"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    direct_image = item.get("image")
    if isinstance(direct_image, str) and direct_image.strip():
        return direct_image.strip()
    if isinstance(direct_image, dict):
        for key in ("large", "medium", "small", "url"):
            value = direct_image.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    urls = (item.get("images") or {}).get("urls") or {}
    return urls.get("large_artwork") or urls.get("medium_artwork") or urls.get("small_artwork") or None


def _images(item: dict[str, Any]) -> dict[str, str]:
    urls = (item.get("images") or {}).get("urls") or {}
    return {
        "small": urls.get("small_artwork") or "",
        "medium": urls.get("medium_artwork") or "",
        "large": urls.get("large_artwork") or "",
    }


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
    artwork_url = _image(item)
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
