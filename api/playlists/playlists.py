from api.provider_search import encoded_query, search_entries

class Playlists:
    async def search_playlists(self, search_query: str, limit: int) -> list:
        result = await self._safe_request(
            "POST", self.api_endpoints.search_playlists_url + encoded_query(search_query)
        )
        if isinstance(result, dict) and "error" in result:
            return result
        entries = search_entries(result)
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
