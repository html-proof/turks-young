from api.catalog.normalize import song


def test_verified_slug_title_repair():
    for title in ("Pranaya 5", ""):
        result = song({"seokey": "pranaya-5", "title": title})
        assert result["title"] == "Pranayanila (Version, 2)"
        assert result["id"] == "pranaya-5"


def test_real_numbers_and_other_recordings_are_preserved():
    assert song({"seokey": "kuttanadan-2", "title": "Kuttanadan 2"})["title"] == "Kuttanadan 2"
    assert song({"seokey": "another-record", "title": "Pranaya 5"})["title"] == "Pranaya 5"
    assert song({"seokey": "pranaya-5", "title": "Pranayanila (Version, 2)"})["title"] == "Pranayanila (Version, 2)"
