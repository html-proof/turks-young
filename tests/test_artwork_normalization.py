from api.catalog.artwork import artwork_candidates, normalize_url


def test_array_and_nested_images():
    assert artwork_candidates({"image": [
        {"quality": "50x50", "url": "null"},
        {"quality": "150x150", "url": "https://cdn.test/s.jpg"},
        {"quality": "500x500", "url": "//cdn.test/l.jpg"},
    ], "album": {"artwork": {"large": "https://cdn.test/a.jpg"}}}) == [
        "https://cdn.test/l.jpg", "https://cdn.test/s.jpg", "https://cdn.test/a.jpg"]


def test_default_cover_does_not_mask_real_album():
    assert artwork_candidates({
        "image_url": "https://c.saavncdn.com/000/default-album-500x500.jpg",
        "album": {"cover_url": "https://cdn.test/real.jpg"},
    }) == ["https://cdn.test/real.jpg"]


def test_signed_url_and_explicit_relative_origin():
    signed = "https://cdn.test/150x150.jpg?w=150&signature=a%2B123"
    assert normalize_url(signed) == signed
    assert normalize_url("/cover.jpg") is None
    assert normalize_url("/cover.jpg", "https://cdn.test") == "https://cdn.test/cover.jpg"


def test_invalid_metadata():
    for invalid in (None, "", "null", "undefined", "#", {}, "https://x.test/error.html"):
        assert normalize_url(invalid) is None


def test_listening_snapshot_preserves_album_art_before_flattening():
    from api.personalization.models import TrackSnapshot
    track = TrackSnapshot.model_validate({
        "seokey": "song-123", "title": "Example",
        "album": {"name": "Album", "image": [
            {"quality": "500x500", "url": "https://cdn.test/album.jpg"}]},
    })
    assert track.image_url == "https://cdn.test/album.jpg"
    assert artwork_candidates(track.model_dump()) == ["https://cdn.test/album.jpg"]


def test_mix_uses_real_member_art_instead_of_missing_first():
    from api.personalization.mix_generator import _mix_cover
    assert _mix_cover([{"image_url": None},
        {"album": {"cover": "https://cdn.test/album.jpg"}}]) == "https://cdn.test/album.jpg"
