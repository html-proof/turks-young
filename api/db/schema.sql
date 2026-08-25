-- Soundwaves user data schema — run once in Supabase SQL editor

CREATE TABLE IF NOT EXISTS users (
  uid          TEXT PRIMARY KEY,
  email        TEXT,
  display_name TEXT,
  photo_url    TEXT,
  provider     TEXT,
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_profiles (
  uid              TEXT PRIMARY KEY REFERENCES users(uid) ON DELETE CASCADE,
  display_name     TEXT    NOT NULL DEFAULT '',
  languages        TEXT[]  NOT NULL DEFAULT ARRAY['English'],
  favorite_genres  TEXT[]  NOT NULL DEFAULT '{}',
  favorite_artists TEXT[]  NOT NULL DEFAULT '{}',
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_favorites (
  uid          TEXT        NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  seokey       TEXT        NOT NULL,
  track        JSONB       NOT NULL,
  favorited_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (uid, seokey)
);

CREATE TABLE IF NOT EXISTS user_history (
  id        BIGSERIAL   PRIMARY KEY,
  uid       TEXT        NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  event     JSONB       NOT NULL,
  played_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS user_history_uid_played_at ON user_history (uid, played_at DESC);

CREATE TABLE IF NOT EXISTS user_signals (
  uid    TEXT  NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  bucket TEXT  NOT NULL,   -- 'artists' | 'genres' | 'languages'
  key    TEXT  NOT NULL,   -- sha256 fingerprint of the value
  value  TEXT  NOT NULL,
  score  FLOAT NOT NULL DEFAULT 0,
  PRIMARY KEY (uid, bucket, key)
);
