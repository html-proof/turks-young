from api.provider_search import encoded_query, search_entries

class Playlists:
    async def search_playlists(self, search_query: str, limit: int) -> list:
        clean_q = search_query.strip()
        result = await self._safe_request(
            "POST", self.api_endpoints.search_playlists_url + encoded_query(clean_q)
        )
        entries = search_entries(result) if not (isinstance(result, dict) and "error" in result) else []

        if not entries:
            clean_lower = clean_q.lower()
            tokens = set(clean_lower.split())
            expansions = [
                f"{clean_q} {suffix}"
                for suffix in ("hits", "playlist", "songs")
                if suffix not in tokens and not clean_lower.endswith(suffix)
            ]
            for expansion in expansions[:2]:
                exp_result = await self._safe_request("POST", self.api_endpoints.search_playlists_url + encoded_query(expansion))
                if not (isinstance(exp_result, dict) and "error" in exp_result):
                    exp_entries = search_entries(exp_result)
                    if exp_entries:
                        entries = exp_entries
                        break

        if len(entries) == 0:
            return await self.errors.no_results()
        playlists = []
        for entry in entries[:limit]:
            if not isinstance(entry, dict):
                continue
            seokey = entry.get("seo") or entry.get("seokey")
            if not seokey:
                continue
            artwork = entry.get("atw") or entry.get("artwork") or ""
            playlists.append({
                "seokey": seokey,
                "title": entry.get("title") or entry.get("name") or "",
                "language": entry.get("language") or "",
                "images": {"urls": {
                    "large_artwork": artwork.replace("size_s", "size_l"),
                    "medium_artwork": artwork.replace("size_s", "size_m"),
                    "small_artwork": artwork,
                }},
            })
        if not playlists:
            return await self.errors.no_results()
        return playlists

    async def get_playlist_info(self, playlist_id: str) -> dict:
        endpoints = self.api_endpoints
        errors = self.errors
        result = await self._safe_request("POST", endpoints.playlist_details_url + playlist_id)
        upstream_error = result.get("error") if isinstance(result, dict) else None
        if upstream_error not in (None, "", "SUCCESS"):
            return result
        track_count = result.get('count')
        if not track_count:
            return await errors.no_results()
        track_ids = []
        tracks = result.get('tracks', [])
        for i in range(min(int(track_count), len(tracks))):
            seo = tracks[i].get('seokey') if isinstance(tracks[i], dict) else None
            if seo:
                track_ids.append(seo)
        if len(track_ids) == 0:
            return await errors.no_results()
        track_data = await self.get_track_info(track_ids)
        return track_data
