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
  languages        TEXT[]  NOT NULL DEFAULT '{}',
  language_ids     TEXT[]  NOT NULL DEFAULT '{}',
  favorite_genres  TEXT[]  NOT NULL DEFAULT '{}',
  favorite_artists TEXT[]  NOT NULL DEFAULT '{}',
  favorite_artist_ids TEXT[] NOT NULL DEFAULT '{}',
  onboarding_completed BOOLEAN NOT NULL DEFAULT FALSE,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE user_profiles
  ADD COLUMN IF NOT EXISTS onboarding_completed BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE user_profiles
  ADD COLUMN IF NOT EXISTS language_ids TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE user_profiles
  ADD COLUMN IF NOT EXISTS favorite_artist_ids TEXT[] NOT NULL DEFAULT '{}';

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
CREATE INDEX IF NOT EXISTS user_playlists_uid_updated_at
  ON user_playlists (uid, updated_at DESC);

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

ALTER TABLE user_profiles
  ADD COLUMN IF NOT EXISTS onboarding_step TEXT NOT NULL DEFAULT 'language';

CREATE TABLE IF NOT EXISTS user_follows (
  follower_uid  TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  following_uid TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (follower_uid, following_uid),
  CHECK (follower_uid <> following_uid)
);

CREATE TABLE IF NOT EXISTS pulse_posts (
  id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  uid         TEXT        NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  body        VARCHAR(1000) NOT NULL DEFAULT '',
  track       JSONB,
  album       JSONB,
  playlist_id UUID        REFERENCES user_playlists(id) ON DELETE SET NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pulse_posts_created ON pulse_posts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pulse_posts_uid     ON pulse_posts(uid, created_at DESC);

CREATE TABLE IF NOT EXISTS pulse_likes (
  post_id    UUID NOT NULL REFERENCES pulse_posts(id) ON DELETE CASCADE,
  uid        TEXT NOT NULL REFERENCES users(uid)      ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (post_id, uid)
);

CREATE TABLE IF NOT EXISTS pulse_comments (
  id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  post_id    UUID        NOT NULL REFERENCES pulse_posts(id) ON DELETE CASCADE,
  uid        TEXT        NOT NULL REFERENCES users(uid)      ON DELETE CASCADE,
  body       VARCHAR(1000) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pulse_comments_post ON pulse_comments(post_id, created_at);

CREATE TABLE IF NOT EXISTS device_tokens (
  id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  uid          TEXT        NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  token        TEXT        NOT NULL UNIQUE,
  platform     VARCHAR(20) NOT NULL,
  device_name  VARCHAR(150),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_device_tokens_uid ON device_tokens(uid);

CREATE TABLE IF NOT EXISTS notifications (
  id         UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
  uid        TEXT          NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  type       VARCHAR(50)   NOT NULL,
  title      VARCHAR(200)  NOT NULL,
  body       VARCHAR(500)  NOT NULL,
  data       JSONB         NOT NULL DEFAULT '{}'::jsonb,
  is_read    BOOLEAN       NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(uid, is_read, created_at DESC);

CREATE TABLE IF NOT EXISTS player_sessions (
  uid         TEXT    PRIMARY KEY REFERENCES users(uid) ON DELETE CASCADE,
  track       JSONB,
  queue       JSONB   NOT NULL DEFAULT '[]'::jsonb,
  position_ms BIGINT  NOT NULL DEFAULT 0,
  playing     BOOLEAN NOT NULL DEFAULT FALSE,
  device_id   TEXT,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lyrics_cache (
  track_id           TEXT PRIMARY KEY,
  provider           VARCHAR(50) NOT NULL,
  provider_lyrics_id TEXT,
  synced_lyrics      TEXT,
  plain_lyrics       TEXT,
  instrumental      BOOLEAN NOT NULL DEFAULT FALSE,
  status             VARCHAR(30) NOT NULL DEFAULT 'available'
                     CHECK (status IN ('available', 'not_found', 'instrumental')),
  fetched_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
