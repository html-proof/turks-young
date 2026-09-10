import asyncio
import re
from api.provider_search import encoded_query, search_entries

class Songs:
    async def search_songs(self, search_query: str, limit: int) -> list:
        endpoints = self.api_endpoints
        errors = self.errors
        clean_q = search_query.strip()
        result = await self._safe_request("POST", endpoints.search_songs_url + encoded_query(clean_q))
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
                exp_result = await self._safe_request("POST", endpoints.search_songs_url + encoded_query(expansion))
                if not (isinstance(exp_result, dict) and "error" in exp_result):
                    exp_entries = search_entries(exp_result)
                    if exp_entries:
                        entries = exp_entries
                        break

        if len(entries) == 0:
            fallback_res = getattr(self, "_fallback_resolver", None)
            if not fallback_res:
                try:
                    from api.stream_fallback import get_stream_fallback_resolver
                    fallback_res = get_stream_fallback_resolver()
                except Exception:
                    fallback_res = None
            if fallback_res:
                try:
                    fb_tracks = await fallback_res.search_tracks(clean_q, limit)
                    if fb_tracks:
                        return fb_tracks
                except Exception:
                    pass
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
        result = await self._safe_request("POST", endpoints.search_songs_url + encoded_query(clean_q))
        entries = search_entries(result) if not (isinstance(result, dict) and "error" in result) else []
        candidates = []
        for entry in entries[:limit]:
            if isinstance(entry, dict):
                seo = entry.get("seo") or entry.get("seokey") or entry.get("id") or ""
                title = entry.get("title") or entry.get("track_title") or entry.get("name") or ""
                artist = entry.get("artist") or entry.get("artists") or []
                artwork = entry.get("atw") or entry.get("artwork") or entry.get("artwork_large") or ""
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

    async def get_track_info(self, track_id: list) -> list:
        endpoints = self.api_endpoints
        errors = self.errors
        cache = getattr(self, "cache", None)

        track_info = []
        missing_ids = []

        if cache and hasattr(cache, "get"):
            for tid in track_id:
                try:
                    hit = await cache.get(f"songs:info:{tid}")
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
                self._safe_request("POST", endpoints.song_details_url + i)
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
                    if cache and hasattr(cache, "set"):
                        try:
                            from api.core import config
                            ttl = getattr(config, "TTL_SONG", 21600)
                            await cache.set(f"songs:info:{i}", valid_formatted, ttl)
                        except Exception:
                            pass

        if len(track_info) == 0:
            return await errors.no_results()
        return track_info

    async def format_json_songs(self, results: dict) -> dict:
        functions = self.functions
        errors = self.errors
        data = {}

        seokey = results.get('seokey')
        if not seokey:
            return await errors.invalid_seokey()

        data['seokey'] = seokey
        data['album_seokey'] = results.get('albumseokey') or results.get('album_seokey') or ''
        data['track_id'] = str(results.get('track_id') or results.get('id') or '')
        data['title'] = results.get('track_title') or results.get('title') or ''
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
        artwork = results.get('artwork_large') or results.get('artwork_web') or results.get('artwork') or ''
        data['images'] = {'urls': {}}
        data['images']['urls']['large_artwork'] = results.get('artwork_large') or (artwork.replace("size_s.jpg", "size_l.jpg").replace("size_m.jpg", "size_l.jpg") if artwork else '')
        data['images']['urls']['medium_artwork'] = results.get('artwork_web') or (artwork.replace("size_s.jpg", "size_m.jpg") if artwork else '')
        data['images']['urls']['small_artwork'] = results.get('artwork') or artwork
        data['stream_urls'] = {'urls': {}}

        try:
            urls = results.get('urls', {})
            stream_msg = ""
            for quality in ('medium', 'high', 'auto'):
                q_dict = urls.get(quality) if isinstance(urls, dict) else None
                if isinstance(q_dict, dict) and q_dict.get('message'):
                    stream_msg = q_dict['message']
                    break
            if stream_msg:
                base_url = await functions.decryptLink(stream_msg)
                if base_url:
                    data['stream_urls']['urls']['raw'] = base_url
                    data['stream_urls']['urls']['default'] = base_url
                    base_clean = re.sub(r'\b(?:16|64|128|320)\.mp4', '{bitrate}.mp4', base_url)
                    if "{bitrate}.mp4" in base_clean:
                        data['stream_urls']['urls']['very_high_quality'] = base_clean.format(bitrate="128")
                        data['stream_urls']['urls']['high_quality'] = base_clean.format(bitrate="128")
                        data['stream_urls']['urls']['medium_quality'] = base_clean.format(bitrate="128")
                        data['stream_urls']['urls']['low_quality'] = base_clean.format(bitrate="64")
                    else:
                        data['stream_urls']['urls']['very_high_quality'] = base_url
                        data['stream_urls']['urls']['high_quality'] = base_url
                        data['stream_urls']['urls']['medium_quality'] = base_url
                        data['stream_urls']['urls']['low_quality'] = base_url
                else:
                    raise KeyError
            else:
                raise KeyError
        except (KeyError, AttributeError):
            data['stream_urls']['urls']['very_high_quality'] = ""
            data['stream_urls']['urls']['high_quality'] = ""
            data['stream_urls']['urls']['medium_quality'] = ""
            data['stream_urls']['urls']['low_quality'] = ""

        data['stream_url'] = (
            data['stream_urls']['urls'].get('high_quality')
            or data['stream_urls']['urls'].get('medium_quality')
            or data['stream_urls']['urls'].get('default')
            or data['stream_urls']['urls'].get('raw')
            or data['stream_urls']['urls'].get('low_quality')
            or ""
        )

        # Fallback stream resolution if Gaana has no playable audio stream
        if not data['stream_url'] and data.get('title'):
            fallback_res = getattr(self, "_fallback_resolver", None)
            if fallback_res:
                try:
                    fb = await fallback_res.resolve_stream(data['title'], data.get('artists') or '')
                    if fb and fb.get('stream_url'):
                        data['stream_url'] = fb['stream_url']
                        fb_urls = fb.get('stream_urls', {}).get('urls', {})
                        for k, v in fb_urls.items():
                            data['stream_urls']['urls'][k] = v
                except Exception:
                    pass

        return data
