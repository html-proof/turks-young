import re
from typing import Any

from api.catalog.labels import is_verified_label, label_registry
from api.lyrics.service import duration_seconds


def compute_metadata_confidence(
    title: str,
    artists: list[Any],
    album: Any,
    duration_ms: int,
    artwork_url: str | None,
    stream_url: str | None,
) -> float:
    """Compute confidence (0.0 to 1.0) of song metadata completeness."""
    score = 0.0
    if title:
        score += 0.25
    if artists:
        score += 0.20
    if album:
        score += 0.15
    if duration_ms >= 30000:
        score += 0.15
    if artwork_url:
        score += 0.15
    if stream_url:
        score += 0.10
    return round(score, 2)


def _upgrade_image_quality(url: str | None) -> str | None:
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url:
        return None
    if url.startswith("http://"):
        url = "https://" + url[7:]

    # Gaana artwork size upgrades (size_s, size_m, size_xs, size_m_1748450138 -> size_l)
    url = re.sub(r'size_[smx]+(?=[_0-9\.\-])', 'size_l', url, flags=re.IGNORECASE)
    url = re.sub(r'img_[smx]+(?=[_0-9\.\-])', 'img_l', url, flags=re.IGNORECASE)

    # JioSaavn / Saavn and standard CDNs: 50x50, 150x150, 250x250, 320x320 -> 500x500 HD
    url = re.sub(r'[-_](?:50x50|80x80|150x150|250x250|320x320)([-_/\.\?])', r'-500x500\1', url, flags=re.IGNORECASE)
    url = re.sub(r'[-_](?:50x50|80x80|150x150|250x250|320x320)$', r'-500x500', url, flags=re.IGNORECASE)
    url = re.sub(r'/(?:50x50|80x80|150x150|250x250|320x320)/', r'/500x500/', url, flags=re.IGNORECASE)

    # YouTube thumbnail upgrades
    url = re.sub(r'/(?:default|mqdefault|sddefault)\.jpg', r'/hqdefault.jpg', url, flags=re.IGNORECASE)

    # Google / Firebase avatars
    url = re.sub(r'=s(?:96|120|150|200|300)-c', r'=s500-c', url, flags=re.IGNORECASE)

    # Dimension query parameters
    url = re.sub(r'([?&]w=)(?:50|80|100|150|200|250|300)', r'\g<1>500', url, flags=re.IGNORECASE)
    url = re.sub(r'([?&]width=)(?:50|80|100|150|200|250|300)', r'\g<1>500', url, flags=re.IGNORECASE)
    url = re.sub(r'([?&]h=)(?:50|80|100|150|200|250|300)', r'\g<1>500', url, flags=re.IGNORECASE)
    url = re.sub(r'([?&]height=)(?:50|80|100|150|200|250|300)', r'\g<1>500', url, flags=re.IGNORECASE)

    return url


def _clean_artist_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, dict):
        return str(val.get("name") or val.get("title") or val.get("id") or val.get("seokey") or "").strip()
    s = str(val).strip()
    if (s.startswith("{") or s.startswith("'") or s.startswith('"')) and ("name" in s or "id" in s):
        match = re.search(r"['\"]name['\"]\s*:\s*['\"]([^'\"]+)['\"]", s)
        if match:
            return match.group(1).strip()
        match_id = re.search(r"['\"](?:id|seokey|artist_id)['\"]\s*:\s*['\"]([^'\"]+)['\"]", s)
        if match_id:
            return match_id.group(1).strip()
    return s


def _clean_artist_id(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, dict):
        return str(val.get("id") or val.get("seokey") or val.get("artist_id") or "").strip()
    s = str(val).strip()
    if (s.startswith("{") or s.startswith("'") or s.startswith('"')) and ("id" in s or "seokey" in s):
        match = re.search(r"['\"](?:id|seokey|artist_id)['\"]\s*:\s*['\"]([^'\"]+)['\"]", s)
        if match:
            return match.group(1).strip()
    return s


def _clean_artist_entry(val: Any) -> tuple[str, str]:
    if val is None:
        return "", ""
    if isinstance(val, dict):
        name = _clean_artist_str(val.get("name") or val.get("title"))
        aid = _clean_artist_id(val.get("id") or val.get("seokey") or val.get("artist_id"))
        return name, aid
    s = str(val).strip()
    if (s.startswith("{") or s.startswith("'") or s.startswith('"')) and ("name" in s or "id" in s):
        name = _clean_artist_str(s)
        aid = _clean_artist_id(s)
        return name, aid
    return s, ""


def _slugify_artist_name(name: str) -> str:
    return re.sub(r'[^a-zA-Z0-9]+', '-', str(name or '')).strip('-').lower()


def _normalize_artists_list(item: dict[str, Any]) -> list[dict[str, Any]]:
    raw_artists = item.get("artists")
    raw_artist = item.get("artist")
    raw_ids = item.get("artist_seokeys") or item.get("artist_ids")

    # If artists is already a list of dicts:
    if isinstance(raw_artists, list) and any(isinstance(x, dict) for x in raw_artists):
        clean_list: list[dict[str, Any]] = []
        for a in raw_artists:
            if isinstance(a, dict):
                clean_name, clean_id = _clean_artist_entry(a)
                if not clean_id and clean_name:
                    clean_id = _slugify_artist_name(clean_name)
                clean_image = a.get("image_url") or a.get("imageUrl") or a.get("image") or ""
                if isinstance(clean_image, dict):
                    clean_image = clean_image.get("large") or clean_image.get("medium") or ""
                record = {
                    "id": clean_id,
                    "name": clean_name,
                    "type": "artist",
                }
                if clean_image:
                    record["image_url"] = _upgrade_image_quality(str(clean_image))
                clean_list.append(record)
            elif isinstance(a, str):
                cn, cid = _clean_artist_entry(a)
                if cn:
                    clean_list.append({
                        "id": cid or _slugify_artist_name(cn),
                        "name": cn,
                        "type": "artist",
                    })
        if clean_list:
            return clean_list

    names: list[str] = []
    inline_ids: list[str] = []
    if isinstance(raw_artists, list):
        for x in raw_artists:
            cn, cid = _clean_artist_entry(x)
            if cn:
                names.append(cn)
                inline_ids.append(cid)
    elif isinstance(raw_artists, str) and raw_artists.strip():
        s = raw_artists.strip()
        if (s.startswith("{") or s.startswith("'") or s.startswith('"')) and ("name" in s or "id" in s):
            cn, cid = _clean_artist_entry(s)
            if cn:
                names.append(cn)
                inline_ids.append(cid)
        else:
            for part in s.split(","):
                cn = _clean_artist_str(part)
                if cn:
                    names.append(cn)
                    inline_ids.append("")

    if not names:
        if isinstance(raw_artist, dict):
            cn, cid = _clean_artist_entry(raw_artist)
            if cn:
                return [{
                    "id": cid or _slugify_artist_name(cn),
                    "name": cn,
                    "type": "artist",
                }]
        elif isinstance(raw_artist, str) and raw_artist.strip():
            s = raw_artist.strip()
            if (s.startswith("{") or s.startswith("'") or s.startswith('"')) and ("name" in s or "id" in s):
                cn, cid = _clean_artist_entry(s)
                if cn:
                    names.append(cn)
                    inline_ids.append(cid)
            else:
                for part in s.split(","):
                    cn = _clean_artist_str(part)
                    if cn:
                        names.append(cn)
                        inline_ids.append("")

    ids: list[str] = []
    if isinstance(raw_ids, list):
        for x in raw_ids:
            cid = _clean_artist_id(x)
            if cid:
                ids.append(cid)
    elif isinstance(raw_ids, str) and raw_ids.strip():
        ids = [_clean_artist_id(x) for x in raw_ids.split(",") if _clean_artist_id(x)]

    result: list[dict[str, Any]] = []
    for index, name in enumerate(names):
        if not name:
            continue
        artist_id = ""
        if index < len(ids) and ids[index]:
            artist_id = ids[index]
        elif index < len(inline_ids) and inline_ids[index]:
            artist_id = inline_ids[index]
        else:
            artist_id = _slugify_artist_name(name)
        result.append({
            "id": artist_id,
            "name": name,
            "type": "artist",
        })
    return result


def _csv(value: Any) -> list[str]:
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            c = _clean_artist_str(item)
            if c:
                out.append(c)
        return out
    s = _clean_artist_str(value)
    return [item.strip() for item in s.split(",") if item.strip()]


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
    raw_name = item.get("name") or ""
    clean_name = _clean_artist_str(raw_name)
    raw_id = item.get("seokey") or item.get("id") or ""
    clean_id = _clean_artist_id(raw_id) or (_slugify_artist_name(clean_name) if clean_name else "")
    return {
        "id": clean_id,
        "provider_id": str(item.get("artist_id") or item.get("provider_id") or ""),
        "type": "artist",
        "name": clean_name,
        "image_url": image_url,
        "imageUrl": image_url,
        "image": {key: value for key, value in images.items() if value},
        "image_status": "verified" if image_url else "placeholder",
        "verified": bool(item.get("verified", False)),
        "followers_count": _int(item.get("favorite_count") or item.get("followers_count")),
    }


def song(item: dict[str, Any]) -> dict[str, Any]:
    artists = _normalize_artists_list(item)
    streams = (item.get("stream_urls") or {}).get("urls") or {}
    # Catalog records may already be normalized and carry a single playable
    # URL instead of the provider's quality map. Preserve it when present so
    # album tracklists do not become metadata-only tracks.
    direct_stream = item.get("stream_url") or item.get("streamUrl") or ""
    seconds = duration_seconds(item.get("duration"))
    # Provider song records expose artist_image alongside the actual album
    # artwork. Prefer the track/album artwork fields so every song keeps its
    # own cover instead of inheriting an artist portrait.
    label_registry.observe_song(item)
    label_str = str(item.get("label") or "").strip()
    is_official = is_verified_label(label_str) or bool(item.get("official_label_verified"))
    title_str = str(item.get("title") or "")
    artwork_url = _song_artwork(item)
    album_id = str(item.get("album_seokey") or item.get("album_id") or "")
    album_title = str(item.get("album") or "")
    stream_final = (
        streams.get("high_quality") or streams.get("medium_quality")
        or streams.get("very_high_quality") or streams.get("low_quality")
        or direct_stream or None
    )
    meta_conf = compute_metadata_confidence(
        title=title_str,
        artists=artists,
        album=album_title or album_id,
        duration_ms=seconds * 1000,
        artwork_url=artwork_url,
        stream_url=stream_final,
    )
    return {
        "id": str(item.get("seokey") or item.get("id") or ""),
        "provider_id": str(item.get("track_id") or item.get("provider_id") or ""),
        "type": "song",
        "title": title_str,
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
        "label": label_str,
        "official_label_verified": is_official,
        "metadata_confidence": meta_conf,
        "release_date": item.get("release_date") or None,
        "explicit": bool(item.get("is_explicit", False)),
        "stream_url": stream_final,
        "stream_urls": item.get("stream_urls") or ({"urls": streams} if streams else None),
        "lyrics_url": f"/api/v1/tracks/{item.get('seokey')}/lyrics" if item.get("seokey") else None,
    }


def album(item: dict[str, Any]) -> dict[str, Any]:
    label_registry.observe_album(item)
    artist_values = _normalize_artists_list(item)
    names = [a["name"] for a in artist_values]
    ids = [a["id"] for a in artist_values]
    title = str(item.get("title") or item.get("name") or "")
    artwork_url = _image(item)
    release_date = item.get("release_date") or None
    release_year = None
    if release_date:
        try:
            release_year = int(str(release_date)[:4])
        except ValueError:
            release_year = None
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
