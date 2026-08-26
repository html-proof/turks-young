-- Music Hub: new tables for Pulse, social, devices, notifications, player session

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
  id          UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
  uid         TEXT          NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  body        VARCHAR(1000) NOT NULL DEFAULT '',
  track       JSONB,
  album       JSONB,
  playlist_id UUID          REFERENCES user_playlists(id) ON DELETE SET NULL,
  created_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ   NOT NULL DEFAULT now()
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
  id         UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
  post_id    UUID          NOT NULL REFERENCES pulse_posts(id) ON DELETE CASCADE,
  uid        TEXT          NOT NULL REFERENCES users(uid)      ON DELETE CASCADE,
  body       VARCHAR(1000) NOT NULL,
  created_at TIMESTAMPTZ   NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pulse_comments_post ON pulse_comments(post_id, created_at);

CREATE TABLE IF NOT EXISTS device_tokens (
  id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  uid          TEXT         NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  token        TEXT         NOT NULL UNIQUE,
  platform     VARCHAR(20)  NOT NULL,
  device_name  VARCHAR(150),
  created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_device_tokens_uid ON device_tokens(uid);

CREATE TABLE IF NOT EXISTS notifications (
  id         UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  uid        TEXT         NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  type       VARCHAR(50)  NOT NULL,
  title      VARCHAR(200) NOT NULL,
  body       VARCHAR(500) NOT NULL,
  data       JSONB        NOT NULL DEFAULT '{}'::jsonb,
  is_read    BOOLEAN      NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ  NOT NULL DEFAULT now()
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
