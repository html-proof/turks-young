import ast
import html
import json
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


def _is_artist_noise(s: str) -> bool:
    if not s:
        return True
    s_clean = str(s).lower().strip().strip("'\"{}[]")
    if not s_clean:
        return True
    if any(k in s_clean for k in ("type:", "'type'", '"type"', "artist}", "{artist", "artist_id:", "seokey:")):
        return True
    if s_clean in ("artist", "artists", "singer", "singers", "song", "track", "album", "true", "false", "none", "null"):
        return True
    return False


def _clean_artist_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, dict):
        name = str(val.get("name") or val.get("title") or val.get("id") or val.get("seokey") or "").strip()
        return "" if _is_artist_noise(name) else name
    s = str(val).strip()
    if _is_artist_noise(s):
        return ""
    if (s.startswith("{") or s.startswith("'") or s.startswith('"') or s.startswith("[")) and ("name" in s or "id" in s):
        match = re.search(r"['\"]name['\"]\s*:\s*['\"]([^'\"]+)['\"]", s)
        if match and not _is_artist_noise(match.group(1)):
            return match.group(1).strip()
        match_id = re.search(r"['\"](?:id|seokey|artist_id)['\"]\s*:\s*['\"]([^'\"]+)['\"]", s)
        if match_id and not _is_artist_noise(match_id.group(1)):
            return match_id.group(1).strip()
    s = re.sub(r"^[{}\[\]\'\"\s]+|[{}\[\]\'\"\s]+$", "", s).strip()
    return "" if _is_artist_noise(s) else s


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
    s = re.sub(r"^[{}\[\]\'\"\s]+|[{}\[\]\'\"\s]+$", "", s).strip()
    return "" if _is_artist_noise(s) else s


def _clean_artist_entry(val: Any) -> tuple[str, str]:
    if val is None:
        return "", ""
    if isinstance(val, dict):
        name = _clean_artist_str(val.get("name") or val.get("title"))
        aid = _clean_artist_id(val.get("id") or val.get("seokey") or val.get("artist_id"))
        return name, aid
    s = str(val).strip()
    if _is_artist_noise(s):
        return "", ""
    if (s.startswith("{") or s.startswith("'") or s.startswith('"')) and ("name" in s or "id" in s):
        name = _clean_artist_str(s)
        aid = _clean_artist_id(s)
        return name, aid
    clean = _clean_artist_str(s)
    return clean, ""


def _slugify_artist_name(name: str) -> str:
    return re.sub(r'[^a-zA-Z0-9]+', '-', str(name or '')).strip('-').lower()


def _dedupe_artist_tuples(tuples: list[tuple[str, str]]) -> list[tuple[str, str]]:
    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, aid in tuples:
        name_clean = name.strip()
        if not name_clean or _is_artist_noise(name_clean):
            continue
        key = name_clean.casefold()
        if key not in seen:
            unique.append((name_clean, aid.strip()))
            seen.add(key)

    filtered: list[tuple[str, str]] = []
    for name, aid in unique:
        is_slug = bool(re.match(r'^[a-z0-9\-]+$', name) and '-' in name)
        is_subsumed = False
        for other_name, _ in unique:
            if other_name != name and ' ' in other_name:
                other_words = [w.lower() for w in re.split(r'[\s\-]+', other_name) if w]
                name_words = [w.lower() for w in re.split(r'[\s\-]+', name) if w]
                if is_slug and any(w1 in w2 or w2 in w1 for w1 in name_words for w2 in other_words):
                    is_subsumed = True
                    break
                slug = re.sub(r'[^a-zA-Z0-9]+', '', name).lower()
                other_slug = re.sub(r'[^a-zA-Z0-9]+', '', other_name).lower()
                if slug == other_slug or slug in other_slug or other_slug in slug:
                    is_subsumed = True
                    break
        if not is_subsumed:
            filtered.append((name, aid))
    return filtered


def _parse_artists_from_raw(val: Any) -> list[tuple[str, str]]:
    if not val:
        return []
    if isinstance(val, list):
        res: list[tuple[str, str]] = []
        for item in val:
            if isinstance(item, dict):
                n = _clean_artist_str(item.get("name") or item.get("title"))
                i = _clean_artist_id(item.get("id") or item.get("seokey") or item.get("artist_id"))
                if n and not _is_artist_noise(n):
                    res.append((n, i))
            elif isinstance(item, str):
                res.extend(_parse_artists_from_raw(item))
        return _dedupe_artist_tuples(res)
    if isinstance(val, dict):
        n = _clean_artist_str(val.get("name") or val.get("title"))
        i = _clean_artist_id(val.get("id") or val.get("seokey") or val.get("artist_id"))
        if n and not _is_artist_noise(n):
            return [(n, i)]
        return []
    s = str(val).strip()
    if not s:
        return []
    if (s.startswith("[") and s.endswith("]")) or (s.startswith("{") and s.endswith("}")):
        try:
            parsed = json.loads(s.replace("'", '"'))
            res = _parse_artists_from_raw(parsed)
            if res:
                return res
        except Exception:
            pass
        try:
            parsed = ast.literal_eval(s)
            res = _parse_artists_from_raw(parsed)
            if res:
                return res
        except Exception:
            pass
    cleaned_str = re.sub(r"""['"]?type['"]?\s*:\s*['"]?[a-zA-Z0-9_\-]+['"]?\}?""", "", s, flags=re.IGNORECASE)
    cleaned_str = re.sub(r"""['"]?(?:id|seokey|artist_id)['"]\s*:\s*['"]([^'"]+)['"]""", r"\1", cleaned_str)
    cleaned_str = re.sub(r"""['"]?name['"]\s*:\s*['"]([^'"]+)['"]""", r"\1", cleaned_str)
    cleaned_str = re.sub(r"""[\{\}\[\]]""", "", cleaned_str)
    parts = re.split(r",\s*|;\s*", cleaned_str)
    res = []
    for part in parts:
        cn = _clean_artist_str(part)
        if cn and not _is_artist_noise(cn):
            aid = ""
            if any(k in part.lower() for k in ("id:", "seokey:", "'id'", '"id"')):
                aid = _clean_artist_id(part)
            res.append((cn, aid))
    return _dedupe_artist_tuples(res)


def _normalize_artists_list(item: dict[str, Any]) -> list[dict[str, Any]]:
    raw_artists = item.get("artists")
    raw_artist = item.get("artist")
    raw_ids = item.get("artist_seokeys") or item.get("artist_ids")

    # Extract authentic original artist portraits if available in item
    artist_img_map: dict[str, str] = {}
    details = item.get("artist_detail")
    if isinstance(details, list):
        for d in details:
            if isinstance(d, dict):
                dname, did = _clean_artist_entry(d)
                dimg = _upgrade_image_quality(
                    d.get("atw") or d.get("artwork_large") or d.get("artwork_175x175")
                    or d.get("artwork") or d.get("image_url") or d.get("imageUrl") or d.get("image")
                )
                if dimg:
                    if dname:
                        artist_img_map[dname.lower()] = dimg
                    if did:
                        artist_img_map[did.lower()] = dimg
    if isinstance(raw_artist, list):
        for a in raw_artist:
            if isinstance(a, dict):
                aname, aid = _clean_artist_entry(a)
                aimg = _upgrade_image_quality(
                    a.get("atw") or a.get("artwork_large") or a.get("artwork_175x175")
                    or a.get("artwork") or a.get("image_url") or a.get("imageUrl") or a.get("image")
                )
                if aimg:
                    if aname and aname.lower() not in artist_img_map:
                        artist_img_map[aname.lower()] = aimg
                    if aid and aid.lower() not in artist_img_map:
                        artist_img_map[aid.lower()] = aimg

    primary_artist_img = _upgrade_image_quality(item.get("artist_image"))

    # If artists is already a list of dicts:
    if isinstance(raw_artists, list) and any(isinstance(x, dict) for x in raw_artists):
        clean_list: list[dict[str, Any]] = []
        for index, a in enumerate(raw_artists):
            if isinstance(a, dict):
                clean_name, clean_id = _clean_artist_entry(a)
                if not clean_name or _is_artist_noise(clean_name):
                    continue
                if not clean_id and clean_name:
                    clean_id = _slugify_artist_name(clean_name)
                clean_image = a.get("image_url") or a.get("imageUrl") or a.get("image") or a.get("atw") or ""
                if isinstance(clean_image, dict):
                    clean_image = clean_image.get("large") or clean_image.get("medium") or ""
                clean_img_str = _upgrade_image_quality(str(clean_image)) if clean_image else ""
                if not clean_img_str:
                    clean_img_str = (
                        artist_img_map.get(clean_id.lower())
                        or artist_img_map.get(clean_name.lower())
                        or (primary_artist_img if index == 0 else "")
                    )
                record = {
                    "id": clean_id,
                    "name": clean_name,
                    "type": "artist",
                }
                if clean_img_str:
                    record["image_url"] = clean_img_str
                    record["imageUrl"] = clean_img_str
                    record["image_status"] = "verified"
                clean_list.append(record)
            elif isinstance(a, str):
                for cn, cid in _parse_artists_from_raw(a):
                    clean_id = cid or _slugify_artist_name(cn)
                    clean_img_str = (
                        artist_img_map.get(clean_id.lower())
                        or artist_img_map.get(cn.lower())
                        or (primary_artist_img if index == 0 else "")
                    )
                    rec = {
                        "id": clean_id,
                        "name": cn,
                        "type": "artist",
                    }
                    if clean_img_str:
                        rec["image_url"] = clean_img_str
                        rec["imageUrl"] = clean_img_str
                        rec["image_status"] = "verified"
                    clean_list.append(rec)
        if clean_list:
            deduped = []
            for rec in clean_list:
                rname = rec["name"]
                is_slug = bool(re.match(r'^[a-z0-9\-]+$', rname) and '-' in rname)
                is_subsumed = False
                for other in clean_list:
                    if other["name"] != rname and ' ' in other["name"]:
                        other_words = [w.lower() for w in re.split(r'[\s\-]+', other["name"]) if w]
                        r_words = [w.lower() for w in re.split(r'[\s\-]+', rname) if w]
                        if is_slug and any(w1 in w2 or w2 in w1 for w1 in r_words for w2 in other_words):
                            is_subsumed = True
                            break
                        slug = re.sub(r'[^a-zA-Z0-9]+', '', rname).lower()
                        other_slug = re.sub(r'[^a-zA-Z0-9]+', '', other["name"]).lower()
                        if slug == other_slug or slug in other_slug or other_slug in slug:
                            is_subsumed = True
                            break
                if not is_subsumed:
                    deduped.append(rec)
            return deduped or clean_list

    parsed_tuples = _parse_artists_from_raw(raw_artists)
    if not parsed_tuples:
        parsed_tuples = _parse_artists_from_raw(raw_artist)

    ids: list[str] = []
    if isinstance(raw_ids, list):
        for x in raw_ids:
            cid = _clean_artist_id(x)
            if cid and not _is_artist_noise(cid):
                ids.append(cid)
    elif isinstance(raw_ids, str) and raw_ids.strip():
        ids = [_clean_artist_id(x) for x in raw_ids.split(",") if _clean_artist_id(x) and not _is_artist_noise(x)]

    result: list[dict[str, Any]] = []
    for index, (name, aid) in enumerate(parsed_tuples):
        if not name or _is_artist_noise(name):
            continue
        artist_id = aid
        if not artist_id:
            if index < len(ids) and ids[index]:
                artist_id = ids[index]
            else:
                artist_id = _slugify_artist_name(name)
        clean_img_str = (
            artist_img_map.get(artist_id.lower())
            or artist_img_map.get(name.lower())
            or (primary_artist_img if index == 0 else "")
        )
        record = {
            "id": artist_id,
            "name": name,
            "type": "artist",
        }
        if clean_img_str:
            record["image_url"] = clean_img_str
            record["imageUrl"] = clean_img_str
            record["image_status"] = "verified"
        result.append(record)
    return result


def _csv(value: Any) -> list[str]:
    parsed = _parse_artists_from_raw(value)
    if parsed:
        return [p[0] for p in parsed]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            c = _clean_artist_str(item)
            if c and not _is_artist_noise(c):
                out.append(c)
        return out
    s = _clean_artist_str(value)
    return [item.strip() for item in s.split(",") if item.strip() and not _is_artist_noise(item)]


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
        "album_artwork", "artworkUrl", "cover", "cover_image", "album_image",
    ):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return _upgrade_image_quality(value.strip())
    album_value = item.get("album")
    if isinstance(album_value, dict):
        for key in ("artworkUrl", "imageUrl", "image_url", "artwork", "image"):
            value = album_value.get(key)
            if isinstance(value, str) and value.strip():
                return _upgrade_image_quality(value.strip())
    elif isinstance(album_value, str) and album_value.strip().startswith("http"):
        return _upgrade_image_quality(album_value.strip())

    direct_image = item.get("image")
    if isinstance(direct_image, str) and direct_image.strip():
        return _upgrade_image_quality(direct_image.strip())
    elif isinstance(direct_image, dict):
        for key in ("large", "medium", "small", "url"):
            value = direct_image.get(key)
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
    for key in ("imageUrl", "image_url", "thumbnail", "photo", "atw"):
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
        "seokey": clean_id,
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


def clean_album_or_title(val: Any) -> str:
    if not val:
        return ""
    if isinstance(val, dict):
        t = val.get("title") or val.get("name") or val.get("album") or ""
        if t:
            return clean_album_or_title(t)
        slug = val.get("id") or val.get("seokey") or val.get("album_seokey") or ""
        if slug:
            return str(slug).replace("-", " ").title()
        return ""
    s = str(val).strip()
    if not s:
        return ""
    if (s.startswith("{") and s.endswith("}")) or (
        "'id':" in s or '"id":' in s or "'title':" in s or '"title":' in s or "'name':" in s or '"name":' in s
    ):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, dict):
                t = parsed.get("title") or parsed.get("name") or parsed.get("album")
                if t:
                    return clean_album_or_title(t)
                slug = parsed.get("id") or parsed.get("seokey") or parsed.get("album_seokey") or ""
                if slug:
                    return str(slug).replace("-", " ").title()
        except Exception:
            pass
        try:
            parsed = ast.literal_eval(s)
            if isinstance(parsed, dict):
                t = parsed.get("title") or parsed.get("name") or parsed.get("album")
                if t:
                    return clean_album_or_title(t)
                slug = parsed.get("id") or parsed.get("seokey") or parsed.get("album_seokey") or ""
                if slug:
                    return str(slug).replace("-", " ").title()
        except Exception:
            pass
        m = re.search(r"""['"](?:title|name)['"]\s*:\s*['"]([^'"]+)['"]""", s)
        if m and m.group(1).strip():
            return clean_album_or_title(m.group(1).strip())
        m_id = re.search(r"""['"](?:id|seokey|album_seokey)['"]\s*:\s*['"]([^'"]+)['"]""", s)
        if m_id and m_id.group(1).strip():
            return m_id.group(1).strip().replace("-", " ").title()
    # Provider metadata occasionally contains HTML entities (for example,
    # ``&quot;`` around a movie name). They are display data, not markup, so
    # decode them before returning data to every catalog client.
    for _ in range(2):
        decoded = html.unescape(s)
        if decoded == s:
            break
        s = decoded
    return " ".join(s.split())


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
    raw_title = (
        item.get("title")
        or item.get("name")
        or item.get("song")
        or item.get("song_name")
        or item.get("songName")
        or item.get("track_name")
        or item.get("trackName")
        or item.get("track")
        or ""
    )
    title_str = clean_album_or_title(raw_title)
    if not title_str:
        slug = str(item.get("seokey") or item.get("track_id") or item.get("id") or "").strip()
        if slug and not slug.startswith("{") and ":" not in slug and slug not in ("unknown", "unknown-track"):
            title_str = re.sub(r"[-_]+", " ", slug).strip().title()
    artwork_url = _song_artwork(item)

    raw_album = item.get("album")
    album_id = str(item.get("album_seokey") or item.get("album_id") or "")
    provider_album_id = str(item.get("album_id") or "")
    album_title = ""
    if isinstance(raw_album, dict):
        album_title = clean_album_or_title(raw_album.get("title") or raw_album.get("name") or "")
        if not album_id:
            album_id = str(raw_album.get("id") or raw_album.get("seokey") or raw_album.get("album_seokey") or "")
        if not provider_album_id:
            provider_album_id = str(raw_album.get("provider_id") or raw_album.get("album_id") or "")
    elif isinstance(raw_album, str):
        album_title = clean_album_or_title(raw_album)
        if not album_id and (raw_album.strip().startswith("{") or "'id':" in raw_album or '"id":' in raw_album):
            m_id = re.search(r"""['"](?:id|seokey|album_seokey)['"]\s*:\s*['"]([^'"]+)['"]""", raw_album)
            if m_id:
                album_id = m_id.group(1).strip()
            m_pid = re.search(r"""['"](?:provider_id|album_id)['"]\s*:\s*['"]([^'"]+)['"]""", raw_album)
            if m_pid:
                provider_album_id = m_pid.group(1).strip()

    if not album_title and album_id:
        album_title = album_id.replace("-", " ").title()

    if album_id and (album_id.strip().startswith("{") or "'id':" in album_id or '"id":' in album_id):
        m_id = re.search(r"""['"](?:id|seokey|album_seokey)['"]\s*:\s*['"]([^'"]+)['"]""", album_id)
        if m_id:
            album_id = m_id.group(1).strip()

    stream_final = (
        streams.get("high_quality") or streams.get("medium_quality")
        or streams.get("very_high_quality") or streams.get("low_quality")
        or direct_stream or None
    )
    if stream_final and "320.mp4" in stream_final and "saavn" not in stream_final:
        stream_final = stream_final.replace("320.mp4", "128.mp4")
    artist_image_url = _upgrade_image_quality(item.get("artist_image"))
    if not artist_image_url and artists and artists[0].get("image_url"):
        artist_image_url = artists[0]["image_url"]
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
        "artist_image": artist_image_url or None,
        "artistImage": artist_image_url or None,
        "album": {
            "id": album_id,
            "provider_id": provider_album_id,
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
    raw_title = item.get("title") or item.get("name") or ""
    title = clean_album_or_title(raw_title)
    artwork_url = _image(item)
    release_date = item.get("release_date") or None
    release_year = None
    if release_date:
        try:
            release_year = int(str(release_date)[:4])
        except ValueError:
            release_year = None
    track_count = _int(item.get("track_count") or item.get("song_count"))
    raw_id = str(item.get("seokey") or item.get("id") or "")
    if raw_id.strip().startswith("{") or "'id':" in raw_id or '"id":' in raw_id:
        m_id = re.search(r"""['"](?:id|seokey|album_seokey)['"]\s*:\s*['"]([^'"]+)['"]""", raw_id)
        if m_id:
            raw_id = m_id.group(1).strip()
    data = {
        "id": raw_id,
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
    raw_id = str(item.get("seokey") or item.get("id") or "")
    if raw_id.strip().startswith("{") or "'id':" in raw_id or '"id":' in raw_id:
        m_id = re.search(r"""['"](?:id|seokey)['"]\s*:\s*['"]([^'"]+)['"]""", raw_id)
        if m_id:
            raw_id = m_id.group(1).strip()
    return {
        "id": raw_id,
        "type": "playlist",
        "name": clean_album_or_title(item.get("title") or item.get("name") or ""),
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
