import asyncio
from api.provider_search import encoded_query, search_entries

class Songs:
    async def search_songs(self, search_query: str, limit: int) -> list:
        endpoints = self.api_endpoints
        errors = self.errors
        clean_q = search_query.strip()
        result = await self._safe_request("POST", endpoints.search_songs_url + encoded_query(clean_q))
        entries = search_entries(result) if not (isinstance(result, dict) and "error" in result) else []

        if not entries:
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

    async def get_track_info(self, track_id: list) -> list:
        endpoints = self.api_endpoints
        errors = self.errors
        results = await asyncio.gather(*[
            self._safe_request("POST", endpoints.song_details_url + i)
            for i in track_id
        ])
        track_info = []
        for result in results:
            if isinstance(result, dict) and "error" in result:
                continue
            tracks = result.get('tracks')
            if not tracks:
                continue
            track_info.extend(await asyncio.gather(*[self.format_json_songs(t) for t in tracks]))
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
        artist_detail = results.get('artist_detail')
        data['artist_image'] = (
            artist_detail[0].get('atw', '')
            if artist_detail and isinstance(artist_detail, list) and len(artist_detail) > 0
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
                    data['stream_urls']['urls']['very_high_quality'] = (
                        base_url.replace("64.mp4", "320.mp4").replace("128.mp4", "320.mp4")
                    )
                    data['stream_urls']['urls']['high_quality'] = (
                        base_url.replace("64.mp4", "128.mp4")
                    )
                    data['stream_urls']['urls']['medium_quality'] = base_url
                    data['stream_urls']['urls']['low_quality'] = (
                        base_url.replace("64.mp4", "16.mp4").replace("128.mp4", "16.mp4")
                    )
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
            data['stream_urls']['urls'].get('very_high_quality')
            or data['stream_urls']['urls'].get('high_quality')
            or data['stream_urls']['urls'].get('medium_quality')
            or data['stream_urls']['urls'].get('low_quality')
            or ""
        )

        return data
