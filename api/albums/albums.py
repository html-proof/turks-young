import asyncio
from api.provider_search import encoded_id, encoded_query, search_entries


class Albums:
    async def search_albums(self, search_query: str, limit: int) -> list:
        endpoints = self.api_endpoints
        errors = self.errors
        clean_q = search_query.strip()
        result = await self._safe_request("POST", endpoints.search_albums_url + encoded_query(clean_q))
        entries = search_entries(result) if not (isinstance(result, dict) and "error" in result) else []

        from api.core.circuit_breaker import CBState
        if not entries and self._circuit_breaker.state == CBState.CLOSED:
            clean_lower = clean_q.lower()
            tokens = set(clean_lower.split())
            expansions = [
                f"{clean_q} {suffix}"
                for suffix in ("movie", "soundtrack", "album", "songs")
                if suffix not in tokens and not clean_lower.endswith(suffix)
            ]
            for expansion in expansions[:2]:
                exp_result = await self._safe_request("POST", endpoints.search_albums_url + encoded_query(expansion))
                if not (isinstance(exp_result, dict) and "error" in exp_result):
                    exp_entries = search_entries(exp_result)
                    if exp_entries:
                        entries = exp_entries
                        break

        album_ids = []
        for i in range(min(limit, len(entries))):
            seo = entries[i].get('seo') or entries[i].get('seokey')
            if seo and str(seo).lower() != 'undefined':
                title = str(entries[i].get('ti') or entries[i].get('title') or '').lower()
                if title != 'undefined':
                    album_ids.append(str(seo))

        album_info = []
        if album_ids:
            fetch_ids = album_ids[:min(limit, 10)]
            try:
                album_info = await asyncio.wait_for(self.get_album_info(fetch_ids, False), timeout=1.8)
            except Exception:
                album_info = []

        if not album_info or (isinstance(album_info, dict) and "error" in album_info):
            fallback_res = getattr(self, "_fallback_resolver", None)
            if not fallback_res:
                try:
                    from api.stream_fallback import get_stream_fallback_resolver
                    fallback_res = get_stream_fallback_resolver()
                except Exception:
                    fallback_res = None
            if fallback_res:
                try:
                    fb_albums = await fallback_res.search_albums(clean_q, limit)
                    if fb_albums:
                        return fb_albums
                except Exception:
                    pass
            return await errors.no_results()

        return album_info

    async def get_album_info(self, album_id: list, info: bool, fetch_missing_tracks: bool = True) -> list:
        endpoints = self.api_endpoints
        errors = self.errors

        async def _fetch_one(aid: str) -> dict:
            i_str = str(aid).strip()
            if not i_str:
                return {}
            if i_str.isdigit():
                res = await self._safe_request("POST", f"https://gaana.com/apiv2?country=IN&type=albumDetail&id={i_str}")
                if isinstance(res, dict) and (res.get("album") or res.get("tracks")):
                    return res
                res2 = await self._safe_request("POST", f"https://gaana.com/apiv2?country=IN&type=albumDetail&album_id={i_str}")
                if isinstance(res2, dict) and (res2.get("album") or res2.get("tracks")):
                    return res2
                return res if isinstance(res, dict) else {}
            res = await self._safe_request("POST", endpoints.album_details_url + encoded_id(i_str))
            if isinstance(res, dict) and (res.get("album") or res.get("tracks")):
                alb_meta = res.get("album") if isinstance(res.get("album"), dict) else {}
                if str(alb_meta.get("title")).lower() != "undefined" and str(alb_meta.get("seokey")).lower() != "undefined":
                    return res
            return {}

        results = await asyncio.gather(*[_fetch_one(i) for i in album_id])
        album_info = []
        for result in results:
            if isinstance(result, dict) and "error" in result:
                continue
            album_info.append(await self.format_json_albums(result, info=info, fetch_missing_tracks=fetch_missing_tracks))
        if len(album_info) == 0:
            return await errors.no_results()
        return album_info

    async def get_album_tracks(self, album_id: str, raw_tracks: list = None, album_meta: dict = None, fetch_missing: bool = True) -> list:
        if raw_tracks is None:
            endpoints = self.api_endpoints
            i_str = str(album_id).strip()
            url = f"https://gaana.com/apiv2?country=IN&type=albumDetail&id={i_str}" if i_str.isdigit() else (endpoints.album_details_url + encoded_id(i_str))
            result = await self._safe_request("POST", url)
            if isinstance(result, dict) and "error" in result:
                return result
            raw_tracks = result.get('tracks') or (result.get('album', {}).get('tracks') if isinstance(result.get('album'), dict) else None) or []
            if not album_meta and isinstance(result.get('album'), dict):
                album_meta = result['album']

        if isinstance(raw_tracks, list) and raw_tracks:
            formatted_tracks = []
            for t in raw_tracks:
                if isinstance(t, dict):
                    if album_meta:
                        if not t.get('album_title') and album_meta.get('title'):
                            t['album_title'] = album_meta['title']
                        if not t.get('albumseokey') and album_meta.get('seokey'):
                            t['albumseokey'] = album_meta['seokey']
                        if not t.get('artwork') and album_meta.get('artwork'):
                            t['artwork'] = album_meta['artwork']

                    # Use track_id as a seokey fallback so tracks are not
                    # silently dropped when Gaana omits the seokey field.
                    effective_seokey = t.get('seokey') or ''
                    if not effective_seokey:
                        fallback_id = str(t.get('track_id') or t.get('id') or '').strip()
                        if fallback_id:
                            t = dict(t)  # don't mutate the original
                            t['seokey'] = fallback_id
                            effective_seokey = fallback_id

                    if effective_seokey:
                        # Listing an album must not perform a serial external
                        # audio search for every track. Playback resolves a
                        # missing stream when the user selects that track.
                        formatted = await self.format_json_songs(t, resolve_stream=False)
                        if isinstance(formatted, dict) and 'error' not in formatted:
                            formatted_tracks.append(formatted)
                        else:
                            title = t.get('track_title') or t.get('title') or t.get('name') or ''
                            if title:
                                formatted_tracks.append({
                                    'track_id': str(t.get('track_id') or t.get('id') or ''),
                                    'seokey': effective_seokey,
                                    'title': title,
                                    'album': album_meta.get('title') if album_meta else '',
                                    'album_seokey': album_meta.get('seokey') if album_meta else '',
                                    'duration': str(t.get('duration') or ''),
                                    'artist': t.get('artist') or t.get('artists') or '',
                                    'artwork': album_meta.get('artwork') if album_meta else '',
                                })
                    elif not fetch_missing:
                        title = t.get('track_title') or t.get('title') or t.get('name') or ''
                        if title:
                            formatted_tracks.append({
                                'track_id': str(t.get('track_id') or t.get('id') or ''),
                                'seokey': str(t.get('track_id') or t.get('id') or ''),
                                'title': title,
                                'album': album_meta.get('title') if album_meta else '',
                                'album_seokey': album_meta.get('seokey') if album_meta else '',
                                'duration': str(t.get('duration') or ''),
                                'artist': t.get('artist') or t.get('artists') or '',
                                'artwork': album_meta.get('artwork') if album_meta else '',
                            })
            if formatted_tracks:
                return formatted_tracks

        if not fetch_missing:
            return []

        track_seokeys = []
        for i in raw_tracks:
            seo = i.get('seokey') or i.get('seo') if isinstance(i, dict) else None
            if seo:
                track_seokeys.append(seo)
        if track_seokeys:
            result = await self.get_track_info(track_seokeys)
            if isinstance(result, list):
                return result
        return []

    async def format_json_albums(self, results: dict, info: bool = False, fetch_missing_tracks: bool = True) -> dict:
        functions = self.functions
        errors = self.errors
        data = {}

        album = results.get('album')
        if not album:
            return await errors.no_results()

        title = str(album.get('title') or album.get('name') or '').strip()
        seokey = str(album.get('seokey') or album.get('seo') or '').strip()
        if title.lower() == 'undefined' and (not seokey or seokey.lower() == 'undefined'):
            return await errors.no_results()

        if not seokey or seokey.lower() == 'undefined':
            seokey = str(album.get('album_id') or results.get('album_id') or '').strip()
        if not seokey:
            return await errors.no_results()

        data['seokey'] = seokey
        data['album_id'] = str(album.get('album_id') or results.get('album_id') or '')
        data['title'] = title
        try:
            # Resolve names and IDs from the same provider collection. Mixing
            # album-level names with first-track IDs silently associated the
            # wrong profile with several compilation albums.
            album_artists = album.get('artist') or []
            data['artists'] = await functions.findArtistNames(album_artists)
            data['artist_seokeys'] = await functions.findArtistSeoKeys(album_artists)
            data['artist_ids'] = await functions.findArtistIds(album_artists)
        except (KeyError, IndexError):
            data['artists'] = ""
            data['artist_seokeys'] = ""
            data['artist_ids'] = ""
        data['duration'] = album.get('duration', '')
        data['is_explicit'] = await functions.isExplicit(
            album.get('parental_warning', 0)
        )
        data['language'] = album.get('language', '')
        data['label'] = album.get('recordlevel', '')
        data['track_count'] = album.get('trackcount', '')
        data['release_date'] = album.get('release_date', '')
        data['play_count'] = album.get('al_play_ct', '')
        data['favorite_count'] = album.get('favorite_count', '')
        data['album_url'] = f"https://gaana.com/album/{data['seokey']}"
        artwork = album.get('artwork', '')
        data['images'] = {'urls': {}}
        data['images']['urls']['large_artwork'] = artwork.replace("size_s.jpg", "size_l.jpg") if artwork else ''
        data['images']['urls']['medium_artwork'] = artwork.replace("size_s.jpg", "size_m.jpg") if artwork else ''
        data['images']['urls']['small_artwork'] = artwork

        if info:
            raw_tracks = results.get('tracks') or (album.get('tracks') if isinstance(album, dict) else None) or []
            data['tracks'] = await self.get_album_tracks(data['seokey'], raw_tracks=raw_tracks, album_meta=album, fetch_missing=fetch_missing_tracks)
        return data
