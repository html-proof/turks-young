-- Firebase identity is the only authoritative login link.  Keep `uid` as the
-- stable application identity because all personal-data tables reference it.
-- Existing deployments using Firebase UIDs as `users.uid` are backfilled
-- without changing any history, likes, playlists, or preferences.

ALTER TABLE users ADD COLUMN IF NOT EXISTS firebase_uid TEXT;

UPDATE users
SET firebase_uid = uid
WHERE firebase_uid IS NULL;

ALTER TABLE users
  ALTER COLUMN firebase_uid SET NOT NULL;

ALTER TABLE users
  DROP CONSTRAINT IF EXISTS users_firebase_uid_key;

ALTER TABLE users
  ADD CONSTRAINT users_firebase_uid_key UNIQUE (firebase_uid);

CREATE INDEX IF NOT EXISTS users_firebase_uid_idx ON users(firebase_uid);
