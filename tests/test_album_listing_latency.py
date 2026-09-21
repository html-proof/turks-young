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


@pytest.mark.asyncio
async def test_album_formatter_accepts_songs_array_payloads():
    class Catalog(Albums, Songs):
        pass

    catalog = Catalog()
    catalog.functions = Functions()
    catalog.errors = Errors()
    payload = {
        'album': {
            'title': 'Pichaikaran',
            'seokey': 'pichaikaran',
            'artwork': 'https://example.com/cover.jpg',
            'songs': [
                {'track_id': '1', 'seokey': 'song-1', 'track_title': 'Song 1', 'artist': 'Artist'},
                {'track_id': '2', 'seokey': 'song-2', 'track_title': 'Song 2', 'artist': 'Artist'},
            ],
        }
    }

    result = await catalog.format_json_albums(payload, info=True)
    assert result['title'] == 'Pichaikaran'
    assert len(result['tracks']) == 2
    assert [t['title'] for t in result['tracks']] == ['Song 1', 'Song 2']
