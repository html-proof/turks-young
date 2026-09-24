import asyncio
import html
import logging
import re
import time

import httpx

from api.catalog.artwork import normalize_url, with_upgraded_candidates
from api.core import config
from api.provider_search import encoded_id, encoded_query, search_entries
from api.songs.playback import (
    DECRYPT_FAILED,
    IDENTITY_MATCH_THRESHOLD,
    NO_VALID_SOURCE,
    identity_score,
    playback_cache_key,
    playback_cache_ttl,
    redact_url,
    song_info_cache_key,
    validate_stream,
)

logger = logging.getLogger(__name__)

_STREAM_QUALITIES = ('medium', 'high', 'auto')
_BITRATE_VARIANTS = ("128", "64", "320", "16")
_IDENTITY_SEARCH_LIMIT = 3


def _upgraded_artwork(url: str) -> str:
    return re.sub(r'size_[sm](?=\.jpg)', 'size_l', url)


class Songs:
    async def search_songs(self, search_query: str, limit: int) -> list:
        endpoints = self.api_endpoints
        errors = self.errors
        clean_q = search_query.strip()
        result = await self._safe_request("GET", endpoints.search_songs_url + encoded_query(clean_q))
        entries = search_entries(result) if not (isinstance(result, dict) and "error" in result) else []

        from api.core.circuit_breaker import CBState
        if not entries and self._circuit_breaker.state == CBState.CLOSED:
            clean_lower = clean_q.lower()
            tokens = set(clean_lower.split())
            expansions = [
                f"{clean_q} {suffix}"
                for suffix in ("movie", "songs", "soundtrack", "track")
                if suffix not in tokens and not clean_lower.endswith(suffix)
            ]
            for expansion in expansions[:2]:
                exp_result = await self._safe_request("GET", endpoints.search_songs_url + encoded_query(expansion))
                if not (isinstance(exp_result, dict) and "error" in exp_result):
                    exp_entries = search_entries(exp_result)
                    if exp_entries:
                        entries = exp_entries
                        break

        if len(entries) == 0:
            return await errors.no_results()

        track_ids = []
        for i in range(min(limit, len(entries))):
            seo = entries[i].get('seo') or entries[i].get('seokey')
            if seo:
                track_ids.append(seo)
        if len(track_ids) == 0:
            return await errors.no_results()
        track_info = await self.get_track_info(track_ids)
        return track_info

    async def search_candidates(self, search_query: str, limit: int) -> list:
        """Lightweight candidate search for recommendations without fanning out songDetail calls."""
        endpoints = self.api_endpoints
        clean_q = search_query.strip()
        result = await self._safe_request("GET", endpoints.search_songs_url + encoded_query(clean_q))
        entries = search_entries(result) if not (isinstance(result, dict) and "error" in result) else []
        candidates = []
        for entry in entries[:limit]:
            if isinstance(entry, dict):
                seo = entry.get("seo") or entry.get("seokey") or entry.get("id") or ""
                title = entry.get("title") or entry.get("ti") or entry.get("track_title") or entry.get("name") or ""
                artist = entry.get("artist") or entry.get("artists") or entry.get("sti") or []
                artwork = entry.get("atw") or entry.get("aw") or entry.get("artwork") or entry.get("artwork_large") or ""
                candidates.append({
                    "id": str(seo),
                    "seokey": str(seo),
                    "track_id": str(entry.get("track_id") or entry.get("id") or ""),
                    "title": title,
                    "artist": artist,
                    "artist_detail": entry.get("artist_detail") or [],
                    "album": entry.get("album") or entry.get("album_title") or "",
                    "album_seokey": entry.get("album_seokey") or entry.get("albumseokey") or "",
                    "duration": entry.get("duration") or 0,
                    "artwork": artwork,
                    "language": entry.get("language") or "",
                })
        return candidates

    async def get_track_info(self, track_id: list, *, force_refresh: bool = False) -> list:
        endpoints = self.api_endpoints
        errors = self.errors
        cache = getattr(self, "cache", None)

        track_info = []
        missing_ids = []

        if cache and hasattr(cache, "get") and not force_refresh:
            for tid in track_id:
                try:
                    hit = await cache.get(song_info_cache_key(tid))
                    if hit and isinstance(hit, list) and len(hit) > 0:
                        track_info.extend(hit)
                    else:
                        missing_ids.append(tid)
                except Exception:
                    missing_ids.append(tid)
        else:
            missing_ids = list(track_id)

        if missing_ids:
            results = await asyncio.gather(*[
                self._safe_request("POST", endpoints.song_details_url + encoded_id(i))
                for i in missing_ids
            ])
            for i, result in zip(missing_ids, results):
                if isinstance(result, dict) and "error" in result:
                    continue
                tracks = result.get('tracks')
                if not tracks:
                    continue
                formatted_tracks = await asyncio.gather(*[self.format_json_songs(t) for t in tracks])
                valid_formatted = [t for t in formatted_tracks if t and not (isinstance(t, dict) and "error" in t)]
                if valid_formatted:
                    track_info.extend(valid_formatted)
                    # An entry without a stream URL is a provider failure,
                    # not a fact about the song; never pin it in the cache.
                    if cache and hasattr(cache, "set") and all(t.get('stream_url') for t in valid_formatted):
                        try:
                            await cache.set(song_info_cache_key(i), valid_formatted, config.TTL_SONG)
                        except Exception:
                            pass

        if len(track_info) == 0:
            return await errors.no_results()
        return track_info

    async def format_json_songs(self, results: dict, *, resolve_stream: bool = True) -> dict:
        functions = self.functions
        errors = self.errors
        data = {}

        seokey = results.get('seokey')
        if not seokey:
            return await errors.invalid_seokey()

        data['seokey'] = seokey
        data['album_seokey'] = results.get('albumseokey') or results.get('album_seokey') or ''
        data['track_id'] = str(results.get('track_id') or results.get('id') or '')
        raw_title = results.get('track_title') or results.get('title') or ''
        # Gaana sometimes encodes quotation marks as ``&quot;``. Return a real
        # title so legacy /songs endpoints match the catalog API.
        data['title'] = html.unescape(html.unescape(str(raw_title))).strip()
        data['artists'] = await functions.findArtistNames(
            results.get('artist') or []
        )
        data['artist_seokeys'] = await functions.findArtistSeoKeys(
            results.get('artist') or []
        )
        data['artist_ids'] = await functions.findArtistIds(
            results.get('artist') or []
        )
        artist_detail = results.get('artist_detail') or results.get('artist') or []
        data['artist_detail'] = artist_detail
        data['artist_image'] = (
            artist_detail[0].get('atw', '')
            if artist_detail and isinstance(artist_detail, list) and len(artist_detail) > 0 and isinstance(artist_detail[0], dict)
            else ''
        )
        data['album'] = results.get('album_title') or results.get('album') or ''
        data['album_id'] = str(results.get('album_id') or '')
        data['duration'] = results.get('duration', '')
        data['popularity'] = results.get('popularity', '')
        data['genres'] = await functions.findGenres(
            results.get('gener') or []
        )
        data['is_explicit'] = await functions.isExplicit(
            results.get('parental_warning', 0)
        )
        data['language'] = results.get('language', '')
        data['label'] = results.get('vendor_name') or results.get('recordlevel') or ''
        data['release_date'] = results.get('release_date', '')
        data['play_count'] = results.get('play_ct', '')
        data['favorite_count'] = results.get('total_favourite_count', '')
        data['song_url'] = f"https://gaana.com/song/{data['seokey']}"
        data['album_url'] = (
            f"https://gaana.com/album/{data['album_seokey']}"
            if data['album_seokey'] else ''
        )
        artwork_sizes = [
            normalize_url(results.get(field)) or ''
            for field in ('artwork_large', 'artwork_web', 'artwork')
        ]
        artwork = next((url for url in artwork_sizes if url), '')
        # Only the provider's own URLs fill the size slots. A guessed larger
        # size is offered solely as an extra candidate ahead of the original.
        data['images'] = {'urls': {
            'large_artwork': artwork_sizes[0] or artwork,
            'medium_artwork': artwork_sizes[1] or artwork,
            'small_artwork': artwork_sizes[2] or artwork,
        }}
        data['artwork_candidates'] = with_upgraded_candidates(
            [url for url in artwork_sizes if url], _upgraded_artwork
        )
        data['stream_urls'] = {'urls': {}}

        stream_messages = self._stream_messages(results)
        base_url = ''
        for message in stream_messages:
            base_url = await functions.decryptLink(message)
            if base_url:
                break
        urls = data['stream_urls']['urls']
        if base_url:
            urls['raw'] = base_url
            urls['default'] = base_url
            base_clean = re.sub(r'\b(?:16|64|128|320)\.mp4', '{bitrate}.mp4', base_url)
            if "{bitrate}.mp4" in base_clean:
                urls['very_high_quality'] = base_clean.format(bitrate="128")
                urls['high_quality'] = base_clean.format(bitrate="128")
                urls['medium_quality'] = base_clean.format(bitrate="128")
                urls['low_quality'] = base_clean.format(bitrate="64")
            else:
                urls['very_high_quality'] = base_url
                urls['high_quality'] = base_url
                urls['medium_quality'] = base_url
                urls['low_quality'] = base_url
        else:
            urls['very_high_quality'] = ""
            urls['high_quality'] = ""
            urls['medium_quality'] = ""
            urls['low_quality'] = ""

        data['stream_url'] = (
            urls.get('high_quality')
            or urls.get('medium_quality')
            or urls.get('default')
            or urls.get('raw')
            or urls.get('low_quality')
            or ""
        )
        data['playable'] = bool(data['stream_url'])
        data['unavailable_reason'] = (
            None if data['playable']
            else DECRYPT_FAILED if stream_messages
            else NO_VALID_SOURCE
        )

        return data

    @staticmethod
    def _stream_messages(results: dict) -> list:
        urls = results.get('urls') if isinstance(results, dict) else None
        if not isinstance(urls, dict):
            return []
        messages = []
        for quality in _STREAM_QUALITIES:
            q_dict = urls.get(quality)
            if isinstance(q_dict, dict) and q_dict.get('message') and q_dict['message'] not in messages:
                messages.append(q_dict['message'])
        return messages

    async def _stream_variants(self, track: dict, raw: dict) -> list:
        """Every distinct decrypted URL and bitrate rendition for a track, primary first."""
        urls = (track.get('stream_urls') or {}).get('urls') or {}
        ordered = [track.get('stream_url')]
        ordered += [urls.get(k) for k in ('high_quality', 'medium_quality', 'default', 'raw', 'low_quality')]
        for message in self._stream_messages(raw):
            ordered.append(await self.functions.decryptLink(message))
        variants = []
        for url in ordered:
            if not url:
                continue
            template = re.sub(r'\b(?:16|64|128|320)\.mp4', '{bitrate}.mp4', url)
            expanded = [url]
            if '{bitrate}.mp4' in template:
                expanded += [template.replace('{bitrate}', bitrate) for bitrate in _BITRATE_VARIANTS]
            for candidate in expanded:
                if candidate not in variants:
                    variants.append(candidate)
        return variants

    @staticmethod
    def _apply_stream(track: dict, url: str) -> None:
        urls = track.setdefault('stream_urls', {}).setdefault('urls', {})
        for key in ('raw', 'default', 'very_high_quality', 'high_quality', 'medium_quality', 'low_quality'):
            urls[key] = url
        track['stream_url'] = url
        track['playable'] = True
        track['unavailable_reason'] = None

    @staticmethod
    def _mark_unplayable(track: dict, reason: str) -> None:
        urls = track.setdefault('stream_urls', {}).setdefault('urls', {})
        for key in list(urls):
            urls[key] = ""
        track['stream_url'] = ""
        track['playable'] = False
        track['unavailable_reason'] = reason

    async def _song_detail_tracks(self, seokey: str) -> list:
        result = await self._safe_request("POST", self.api_endpoints.song_details_url + encoded_id(seokey))
        if not isinstance(result, dict) or "error" in result:
            return []
        tracks = result.get('tracks')
        return [t for t in tracks if isinstance(t, dict)] if isinstance(tracks, list) else []

    async def _first_valid_stream(self, client: httpx.AsyncClient, seokey: str, variants: list) -> str:
        for url in variants:
            check = await validate_stream(client, url)
            if check.ok:
                return url
            logger.warning(
                "STREAM_VALIDATION_FAILED provider=gaana id=%s url=%s status=%s content_type=%s error=%s",
                seokey, redact_url(url), check.status, check.content_type or "-", check.error or "-",
            )
        return ""

    async def _recover_by_identity(self, track: dict, client: httpx.AsyncClient) -> bool:
        """Find the same recording under another Gaana id via a title search."""
        seokey = track.get('seokey') or ''
        title = track.get('title') or ''
        if not title:
            return False
        primary_artist = str(track.get('artists') or '').split(',')[0].strip()
        query = f"{title} {primary_artist}".strip()
        result = await self._safe_request("GET", self.api_endpoints.search_songs_url + encoded_query(query))
        entries = search_entries(result) if not (isinstance(result, dict) and "error" in result) else []
        candidate_ids = []
        for entry in entries:
            cid = (entry.get('seo') or entry.get('seokey')) if isinstance(entry, dict) else None
            if cid and cid != seokey and cid not in candidate_ids:
                candidate_ids.append(cid)
            if len(candidate_ids) >= _IDENTITY_SEARCH_LIMIT:
                break
        for cid in candidate_ids:
            for raw in await self._song_detail_tracks(cid):
                candidate = await self.format_json_songs(raw)
                if not isinstance(candidate, dict) or "error" in candidate or not candidate.get('stream_url'):
                    continue
                score = identity_score(track, candidate)
                if score < IDENTITY_MATCH_THRESHOLD:
                    logger.info(
                        "identity candidate rejected provider=gaana id=%s candidate=%s score=%.3f",
                        seokey, cid, score,
                    )
                    continue
                url = await self._first_valid_stream(client, cid, await self._stream_variants(candidate, raw))
                if url:
                    self._apply_stream(track, url)
                    track['playback_source'] = {
                        'provider': 'gaana',
                        'id': candidate.get('seokey') or cid,
                        'match_score': score,
                    }
                    return True
        return False

    async def _resolve_track(self, track: dict, raw: dict, client: httpx.AsyncClient) -> dict:
        seokey = track.get('seokey') or ''
        started = time.perf_counter()
        logger.info("PLAYBACK_RESOLVE_STARTED provider=gaana id=%s", seokey)
        primary = track.get('stream_url') or ''
        if primary and await self._first_valid_stream(client, seokey, [primary]):
            track['playable'] = True
            track['unavailable_reason'] = None
            source = 'primary'
        else:
            decrypt_failed = not primary and bool(self._stream_messages(raw))
            if decrypt_failed:
                failure = DECRYPT_FAILED
            elif primary:
                failure = "VALIDATION_FAILED"
            else:
                failure = NO_VALID_SOURCE
            logger.warning("PRIMARY_RESOLVE_FAILED provider=gaana id=%s reason=%s", seokey, failure)
            logger.info("ALTERNATE_RESOLVE_STARTED provider=gaana id=%s source=variants", seokey)
            variants = [url for url in await self._stream_variants(track, raw) if url != primary]
            url = await self._first_valid_stream(client, seokey, variants)
            if url:
                self._apply_stream(track, url)
                source = 'variant'
            else:
                logger.info("ALTERNATE_RESOLVE_STARTED provider=gaana id=%s source=identity_search", seokey)
                if await self._recover_by_identity(track, client):
                    source = 'identity'
                else:
                    reason = DECRYPT_FAILED if decrypt_failed else NO_VALID_SOURCE
                    self._mark_unplayable(track, reason)
                    logger.warning(
                        "PLAYBACK_UNAVAILABLE provider=gaana id=%s reason=%s latency_ms=%.1f",
                        seokey, reason, (time.perf_counter() - started) * 1000,
                    )
                    return track
        logger.info(
            "PLAYBACK_RESOLVED provider=gaana id=%s source=%s url=%s latency_ms=%.1f",
            seokey, source, redact_url(track.get('stream_url')), (time.perf_counter() - started) * 1000,
        )
        return track

    async def resolve_song_playback(self, seokey: str, *, refresh: bool = False):
        """Song details whose stream URL has been verified to serve audio.

        Validated results are cached briefly under a versioned key; unplayable
        results are never cached so the next request retries the provider.
        """
        cache = getattr(self, "cache", None)
        key = playback_cache_key(seokey)
        if cache and hasattr(cache, "get") and not refresh:
            try:
                hit = await cache.get(key)
                if isinstance(hit, list) and hit:
                    return hit
            except Exception:
                pass

        pairs = []
        for raw in await self._song_detail_tracks(seokey):
            formatted = await self.format_json_songs(raw)
            if isinstance(formatted, dict) and "error" not in formatted:
                pairs.append((formatted, raw))
        if not pairs:
            return await self.errors.no_results()

        client = getattr(self, "stream_http_client", None)
        if client is None:
            async with httpx.AsyncClient(follow_redirects=True, timeout=config.STREAM_VALIDATION_TIMEOUT) as owned:
                resolved = [await self._resolve_track(t, r, owned) for t, r in pairs]
        else:
            resolved = [await self._resolve_track(t, r, client) for t, r in pairs]

        if cache and hasattr(cache, "set") and all(t.get('playable') for t in resolved):
            ttl = min(playback_cache_ttl(t.get('stream_url')) for t in resolved)
            if ttl > 0:
                try:
                    await cache.set(key, resolved, ttl)
                except Exception:
                    pass
        return resolved
