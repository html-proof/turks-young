from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_schema_enforces_one_firebase_uid_per_account():
    migration = (ROOT / "supabase/migrations/20260911000015_firebase_identity_link.sql").read_text()
    assert "ALTER COLUMN firebase_uid SET NOT NULL" in migration
    assert "UNIQUE (firebase_uid)" in migration


def test_provisioning_uses_verified_uid_and_is_transactional():
    repository = (ROOT / "api/personalization/repository.py").read_text()
    assert "async with conn.transaction():" in repository
    assert "ON CONFLICT (firebase_uid) DO UPDATE" in repository
    assert "INSERT INTO users (uid, firebase_uid" in repository


def test_authentication_failure_cannot_be_mapped_to_a_guest_account():
    routes = (ROOT / "api/personalization/routes.py").read_text()
    start = routes.index("async def get_user_repository")
    end = routes.index("\n\ndef get_personalization_service", start)
    resolver = routes[start:end]
    assert "createGuest" not in resolver
    assert "Database is unavailable" in resolver
