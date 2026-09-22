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


@pytest.mark.asyncio
async def test_compilation_container_does_not_overwrite_track_album_seokey():
    """Tracks inside a compilation must keep their own album identity.

    Regression for: tapping Album on "Badass (From Leo)" while it appeared
    inside "Celebrating Thalapathy Vijay" opened that compilation instead of
    the Leo soundtrack.  The root cause was get_album_tracks stamping the
    compilation's seokey onto every track that lacked its own albumseokey.
    """
    class Catalog(Albums, Songs):
        pass

    catalog = Catalog()
    catalog.functions = Functions()
    catalog.errors = Errors()

    # Tracks as Gaana returns them inside a compilation — no albumseokey.
    raw = [
        dict(track_id='1', seokey='badass', title='Badass (From "Leo")',
             duration='230', artist='Anirudh Ravichander'),
    ]
    tracks = await catalog.get_album_tracks(
        'celebrating-thalapathy-vijay',
        raw_tracks=raw,
        album_meta={'title': 'Celebrating Thalapathy Vijay',
                    'seokey': 'celebrating-thalapathy-vijay'},
    )
    assert len(tracks) == 1
    track = tracks[0]
    # The compilation seokey must NOT be present on the track.
    assert track.get('album_seokey', '') != 'celebrating-thalapathy-vijay', (
        "Compilation seokey was incorrectly stamped onto a track that has its "
        "own canonical album (Leo)."
    )


@pytest.mark.asyncio
async def test_real_album_tracks_still_receive_album_seokey():
    """Tracks from a genuine album page correctly inherit the album seokey."""
    class Catalog(Albums, Songs):
        pass

    catalog = Catalog()
    catalog.functions = Functions()
    catalog.errors = Errors()

    raw = [
        dict(track_id='1', seokey='badass', title='Badass',
             duration='230', artist='Anirudh Ravichander'),
    ]
    tracks = await catalog.get_album_tracks(
        'leo-original-motion-picture-soundtrack',
        raw_tracks=raw,
        album_meta={'title': 'Leo (Original Motion Picture Soundtrack)',
                    'seokey': 'leo-original-motion-picture-soundtrack'},
    )
    assert len(tracks) == 1
    track = tracks[0]
    # For a real album the seokey SHOULD be stamped when the track lacks one.
    assert track.get('album_seokey') == 'leo-original-motion-picture-soundtrack', (
        "Genuine album seokey was not propagated to a track that had none."
    )
