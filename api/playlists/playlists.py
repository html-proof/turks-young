from api.provider_search import encoded_id, encoded_query, search_entries

class Playlists:
    async def search_playlists(self, search_query: str, limit: int) -> list:
        clean_q = search_query.strip()
        result = await self._safe_request(
            "GET", self.api_endpoints.search_playlists_url + encoded_query(clean_q)
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
                exp_result = await self._safe_request("GET", self.api_endpoints.search_playlists_url + encoded_query(expansion))
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
            artwork = entry.get("atw") or entry.get("aw") or entry.get("artwork") or ""
            playlists.append({
                "seokey": seokey,
                "title": entry.get("title") or entry.get("ti") or entry.get("name") or "",
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
        result = await self._safe_request("POST", endpoints.playlist_details_url + encoded_id(playlist_id))
        upstream_error = result.get("error") if isinstance(result, dict) else None
        if upstream_error not in (None, "", "SUCCESS"):
            return result
        tracks = result.get('tracks') or result.get('songs') or []
        if not isinstance(tracks, list) or not tracks:
            return await errors.no_results()

        # Newer playlist responses often omit `seokey` while still providing
        # complete embedded track data and a numeric `track_id`. The previous
        # parser discarded every such entry, leaving the app at
        # "0 soundtracks" forever.
        formatted_tracks = []
        unresolved_ids = []
        for raw in tracks:
            if not isinstance(raw, dict):
                continue
            effective_id = str(
                raw.get('seokey')
                or raw.get('seo')
                or raw.get('track_id')
                or raw.get('id')
                or ''
            ).strip()
            if not effective_id:
                continue

            candidate = dict(raw)
            candidate.setdefault('seokey', effective_id)
            title = candidate.get('track_title') or candidate.get('title') or candidate.get('name')
            if title:
                formatted = await self.format_json_songs(candidate, resolve_stream=False)
                if isinstance(formatted, dict) and 'error' not in formatted:
                    formatted_tracks.append(formatted)
                    continue
            unresolved_ids.append(effective_id)

        if unresolved_ids:
            resolved = await self.get_track_info(unresolved_ids)
            if isinstance(resolved, list):
                formatted_tracks.extend(
                    track for track in resolved
                    if isinstance(track, dict) and 'error' not in track
                )

        if not formatted_tracks:
            return await errors.no_results()
        return formatted_tracks
