from typing import Any

from api.lyrics.service import duration_seconds


def _csv(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _image(item: dict[str, Any]) -> str | None:
    urls = (item.get("images") or {}).get("urls") or {}
    return urls.get("large_artwork") or urls.get("medium_artwork") or urls.get("small_artwork") or None


def _int(value: Any) -> int:
    try:
        return int(float(str(value or 0).replace(",", "")))
    except ValueError:
        return 0


def artist(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item.get("seokey") or item.get("id") or ""),
        "provider_id": str(item.get("artist_id") or item.get("provider_id") or ""),
        "type": "artist",
        "name": str(item.get("name") or ""),
        "image_url": item.get("image_url") or _image(item),
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
    seconds = duration_seconds(item.get("duration"))
    return {
        "id": str(item.get("seokey") or item.get("id") or ""),
        "provider_id": str(item.get("track_id") or item.get("provider_id") or ""),
        "type": "song",
        "title": str(item.get("title") or ""),
        "artist": artists[0] if artists else None,
        "artists": artists,
        "album": {
            "id": str(item.get("album_seokey") or item.get("album_id") or ""),
            "provider_id": str(item.get("album_id") or ""),
            "name": str(item.get("album") or ""),
            "type": "album",
        } if item.get("album") else None,
        "image_url": item.get("image_url") or _image(item),
        "duration_ms": seconds * 1000,
        "explicit": bool(item.get("is_explicit", False)),
        "stream_url": (
            streams.get("very_high_quality") or streams.get("high_quality")
            or streams.get("medium_quality") or streams.get("low_quality") or None
        ),
        "lyrics_url": f"/api/v1/tracks/{item.get('seokey')}/lyrics" if item.get("seokey") else None,
    }


def album(item: dict[str, Any]) -> dict[str, Any]:
    names = _csv(item.get("artists"))
    ids = _csv(item.get("artist_seokeys") or item.get("artist_ids"))
    data = {
        "id": str(item.get("seokey") or item.get("id") or ""),
        "provider_id": str(item.get("album_id") or item.get("provider_id") or ""),
        "type": "album",
        "name": str(item.get("title") or item.get("name") or ""),
        "image_url": item.get("image_url") or _image(item),
        "artists": [
            {"id": ids[index] if index < len(ids) else "", "name": name, "type": "artist"}
            for index, name in enumerate(names)
        ],
        "release_date": item.get("release_date") or None,
        "song_count": _int(item.get("track_count") or item.get("song_count")),
    }
    if "tracks" in item:
        tracks = item.get("tracks") if isinstance(item.get("tracks"), list) else []
        data["songs"] = [song(track) for track in tracks if isinstance(track, dict)]
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
