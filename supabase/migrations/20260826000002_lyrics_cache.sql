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
