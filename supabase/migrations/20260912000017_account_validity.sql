BEGIN;
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_account_status_check;
ALTER TABLE users ADD CONSTRAINT users_account_status_check
  CHECK (account_status IN ('active','disabled','suspended','deletion_pending','deleted'));

-- A one-way UID fingerprint prevents an old Firebase identity resurrecting a
-- removed database account. No profile, token, or email is retained here.
CREATE TABLE account_identity_ledger (identity_hash TEXT PRIMARY KEY);
INSERT INTO account_identity_ledger SELECT encode(sha256(convert_to(firebase_uid,'UTF8')),'hex') FROM users;
CREATE TABLE account_lifecycle_epoch (installed_at TIMESTAMPTZ NOT NULL DEFAULT now());
INSERT INTO account_lifecycle_epoch DEFAULT VALUES;
CREATE TABLE account_deletion_jobs (
  firebase_uid TEXT PRIMARY KEY,
  user_id TEXT,
  requested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE account_identity_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE account_lifecycle_epoch ENABLE ROW LEVEL SECURITY;
ALTER TABLE account_deletion_jobs ENABLE ROW LEVEL SECURITY;

CREATE FUNCTION remember_account_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO account_identity_ledger VALUES(encode(sha256(convert_to(NEW.firebase_uid,'UTF8')),'hex'));
  RETURN NEW;
END $$;
CREATE TRIGGER remember_account_identity AFTER INSERT ON users
  FOR EACH ROW EXECUTE FUNCTION remember_account_identity();

CREATE FUNCTION enqueue_deleted_account() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO account_deletion_jobs(firebase_uid,user_id) VALUES(OLD.firebase_uid,OLD.uid)
    ON CONFLICT(firebase_uid) DO NOTHING;
  RETURN OLD;
END $$;
CREATE TRIGGER enqueue_deleted_account BEFORE DELETE ON users
  FOR EACH ROW EXECUTE FUNCTION enqueue_deleted_account();

-- This pre-existing table omitted a foreign key. Normalize legacy Firebase IDs
-- and remove already orphaned settings before adding the ownership constraint.
UPDATE user_audio_settings s SET user_uuid=u.uid FROM users u
 WHERE s.user_uuid=u.firebase_uid AND s.user_uuid<>u.uid
 AND NOT EXISTS(SELECT 1 FROM user_audio_settings s2 WHERE s2.user_uuid=u.uid);
DELETE FROM user_audio_settings s WHERE NOT EXISTS(SELECT 1 FROM users u WHERE u.uid=s.user_uuid);
ALTER TABLE user_audio_settings ADD CONSTRAINT user_audio_settings_owner_fk
 FOREIGN KEY(user_uuid) REFERENCES users(uid) ON DELETE CASCADE ON UPDATE CASCADE;

-- Protect all direct user-owned tables (including worker writes). FOR SHARE
-- conflicts with status updates, serializing writes against deletion's barrier.
CREATE FUNCTION require_active_account_write() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE owner_uid TEXT; owner_status TEXT;
BEGIN
  owner_uid := to_jsonb(NEW)->>TG_ARGV[0];
  SELECT account_status INTO owner_status FROM users WHERE uid=owner_uid FOR SHARE;
  IF owner_status IS DISTINCT FROM 'active' THEN
    RAISE EXCEPTION 'ACCOUNT_INVALID' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN
    SELECT c.conrelid::regclass AS tbl, a.attname AS col
    FROM pg_constraint c JOIN pg_attribute a
      ON a.attrelid=c.conrelid AND a.attnum=c.conkey[1]
    WHERE c.contype='f' AND c.confrelid='users'::regclass
  LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE INSERT OR UPDATE ON %s FOR EACH ROW EXECUTE FUNCTION require_active_account_write(%L)',
      'active_owner_'||r.col, r.tbl, r.col);
  END LOOP;
END $$;
-- Resume cleanup of accounts soft-deleted by older releases.
INSERT INTO account_deletion_jobs(firebase_uid,user_id)
 SELECT firebase_uid,uid FROM users WHERE account_status='deleted'
 ON CONFLICT DO NOTHING;
COMMIT;
