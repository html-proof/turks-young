import pytest
from unittest.mock import AsyncMock

from api import endpoints
from api.errors import Errors
from api.playlists.playlists import Playlists


class FakePlaylists(Playlists):
    def __init__(self, response):
        self.api_endpoints = endpoints
        self.errors = Errors()
        self._safe_request = AsyncMock(return_value=response)
        self.get_track_info = AsyncMock(return_value=[{"seokey": "track-one"}])


@pytest.mark.asyncio
async def test_success_error_marker_does_not_reject_playlist():
    playlist = FakePlaylists({
        "error": "SUCCESS",
        "count": 1,
        "tracks": [{"seokey": "track-one"}],
    })

    result = await playlist.get_playlist_info("playlist-one")

    assert result == [{"seokey": "track-one"}]
    playlist.get_track_info.assert_awaited_once_with(["track-one"])


@pytest.mark.asyncio
async def test_real_upstream_error_is_preserved():
    playlist = FakePlaylists({"error": "Playlist unavailable"})

    result = await playlist.get_playlist_info("missing-playlist")

    assert result == {"error": "Playlist unavailable"}
    playlist.get_track_info.assert_not_awaited()
