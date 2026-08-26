-- Base schema: users, profiles, favorites, history, signals, playlists, followed artists, saved albums

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
  uid                  TEXT    PRIMARY KEY REFERENCES users(uid) ON DELETE CASCADE,
  display_name         TEXT    NOT NULL DEFAULT '',
  languages            TEXT[]  NOT NULL DEFAULT '{}',
  language_ids         TEXT[]  NOT NULL DEFAULT '{}',
  favorite_genres      TEXT[]  NOT NULL DEFAULT '{}',
  favorite_artists     TEXT[]  NOT NULL DEFAULT '{}',
  favorite_artist_ids  TEXT[]  NOT NULL DEFAULT '{}',
  onboarding_completed BOOLEAN NOT NULL DEFAULT FALSE,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
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
  bucket TEXT  NOT NULL,
  key    TEXT  NOT NULL,
  value  TEXT  NOT NULL,
  score  FLOAT NOT NULL DEFAULT 0,
  PRIMARY KEY (uid, bucket, key)
);

CREATE TABLE IF NOT EXISTS user_playlists (
  id          UUID PRIMARY KEY,
  uid         TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  name        TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 100),
  description TEXT NOT NULL DEFAULT '',
  is_public   BOOLEAN NOT NULL DEFAULT FALSE,
  tracks      JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS user_playlists_uid_updated_at ON user_playlists (uid, updated_at DESC);

CREATE TABLE IF NOT EXISTS user_followed_artists (
  uid         TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  seokey      TEXT NOT NULL,
  artist      JSONB NOT NULL,
  followed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (uid, seokey)
);

CREATE TABLE IF NOT EXISTS user_saved_albums (
  uid      TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  seokey   TEXT NOT NULL,
  album    JSONB NOT NULL,
  saved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (uid, seokey)
);
