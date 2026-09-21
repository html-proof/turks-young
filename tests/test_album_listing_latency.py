import pytest

from api.albums.albums import Albums
from api.songs.songs import Songs
from api.functions import Functions
from api.errors import Errors


@pytest.mark.asyncio
async def test_album_tracks_do_not_wait_for_audio_resolution():
    class Catalog(Albums, Songs):
        pass

    catalog = Catalog()
    catalog.functions = Functions()
    catalog.errors = Errors()
    raw = [dict(track_id=str(i), seokey=f'song-{i}', title=f'Song {i}',
                duration='180', artist='Artist') for i in range(6)]
    tracks = await catalog.get_album_tracks(
        'pichaikaran', raw_tracks=raw,
        album_meta={'title': 'Pichaikaran', 'seokey': 'pichaikaran'},
    )
    assert len(tracks) == 6
    assert [t['title'] for t in tracks] == [f'Song {i}' for i in range(6)]
