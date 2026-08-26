ALTER TABLE user_profiles ALTER COLUMN languages SET DEFAULT '{}';
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS language_ids TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS favorite_artist_ids TEXT[] NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS recent_searches (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  uid         TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  query       VARCHAR(200) NOT NULL,
  result_type VARCHAR(30),
  item        JSONB,
  searched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_recent_searches_uid
  ON recent_searches(uid, searched_at DESC);
