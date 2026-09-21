import asyncio

from api.provider_search import encoded_id


class Discovery:
    """Methods for Gaana's newer discovery endpoints."""

    async def get_similar_albums(self, album_id: str, limit: int) -> list:
        result = await self._safe_request(
            "GET", self.api_endpoints.similar_albums_url + encoded_id(album_id),
            headers=self._gaana_headers(),
        )
        if isinstance(result, dict) and "error" in result:
            return result
        records = self._entity_records(result)
        if not records:
            return await self.errors.no_results()
        albums = await asyncio.gather(*[
            self.format_discovered_album(record) for record in records[:limit]
        ])
        albums = [album for album in albums if "error" not in album]
        return albums or await self.errors.no_results()

    async def get_artist_tracks(self, artist_id: str, limit: int, page: int) -> dict:
        result = await self._safe_request(
            "GET",
            self.api_endpoints.artist_tracks_url + encoded_id(artist_id),
            params={
                "sortBy": "popularity", "sortOrder": 0, "request_type": "web",
                "pkc": "true", "st": "hls", "song_type": "new",
                "limit": f"{(page - 1) * limit},{limit}",
            },
            headers=self._gaana_headers(),
        )
        if isinstance(result, dict) and "error" in result:
            return result
        records = self._track_records(result)
        if not records:
            return await self.errors.no_results()
        records = [self._format_artist_track_record(record) for record in records]
        seokeys = [record.get("seokey") for record in records if isinstance(record, dict)]
        if seokeys and hasattr(self, "get_track_info"):
            tracks = await self.get_track_info(seokeys[:limit])
        else:
            tracks = await asyncio.gather(*[
                self.format_json_songs(record) for record in records[:limit]
            ])
        if isinstance(tracks, dict) and "error" in tracks:
            return tracks
        return {"tracks": tracks, "total": result.get("total", len(records))}

    async def format_discovered_album(self, record: dict) -> dict:
        if "album" in record:
            return await self.format_json_albums(record)
        album = record.get("entity") if isinstance(record.get("entity"), dict) else record
        return await self.format_json_albums({"album": {
            "seokey": album.get("seokey") or album.get("seo"),
            "album_id": album.get("album_id") or album.get("entity_id", ""),
            "title": album.get("title") or album.get("name", ""),
            "artist": album.get("artist") or [],
            "duration": album.get("duration", ""),
            "language": album.get("language", ""),
            "recordlevel": album.get("recordlevel", ""),
            "trackcount": album.get("trackcount", ""),
            "release_date": album.get("release_date", ""),
            "al_play_ct": album.get("al_play_ct", ""),
            "favorite_count": album.get("favorite_count", ""),
            "artwork": album.get("artwork") or album.get("atw", ""),
        }})

    @staticmethod
    def _gaana_headers() -> dict:
        return {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            "Accept": "application/json",
            "Origin": "https://gaana.com",
            "Referer": "https://gaana.com/",
            "gaanaAppVersion": "gaanaAndroid-8.60.2",
            "deviceId": "website",
            "deviceType": "GaanaWapApp",
        }

    @staticmethod
    def _entity_records(result: dict) -> list:
        if not isinstance(result, dict):
            return []
        return result.get("entities") or result.get("album") or result.get("albums") or result.get("data") or []

    @staticmethod
    def _track_records(result: dict) -> list:
        if not isinstance(result, dict):
            return []
        return result.get("tracks") or result.get("entities") or result.get("data") or []

    @staticmethod
    def _format_artist_track_record(record: dict) -> dict:
        values = {
            item.get("key"): item.get("value")
            for item in record.get("entity_info", [])
            if isinstance(item, dict) and item.get("key")
        }
        stream = values.get("stream_url") or {}
        medium = stream.get("medium") if isinstance(stream, dict) else {}
        album = values.get("album") or [{}]
        album = album[0] if isinstance(album, list) and album else {}
        return {
            "seokey": record.get("seokey", ""),
            "track_id": record.get("entity_id", ""),
            "track_title": record.get("name", ""),
            "artist": values.get("artist") or [],
            "albumseokey": album.get("album_seokey", ""),
            "album_title": album.get("name", ""),
            "album_id": album.get("album_id", ""),
            "language": record.get("language", ""),
            "artwork_large": record.get("artwork_medium", ""),
            "artwork_web": record.get("atw", ""),
            "artwork": record.get("artwork", ""),
            "duration": values.get("duration", ""),
            "parental_warning": values.get("parental_warning", 0),
            "play_ct": values.get("play_ct", ""),
            "urls": {"medium": medium} if isinstance(medium, dict) else {},
        }

    @staticmethod
    def _is_seokey_only(record: dict) -> bool:
        return set(record) <= {"seokey", "seo"} and bool(record.get("seokey") or record.get("seo"))
