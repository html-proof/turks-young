-- Persistent provider-backed artist catalog relationships used by onboarding.
CREATE TABLE IF NOT EXISTS artist_languages (
  artist_id TEXT NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
  language_id TEXT NOT NULL REFERENCES languages(id) ON DELETE CASCADE,
  source TEXT NOT NULL DEFAULT 'provider',
  confidence DOUBLE PRECISION NOT NULL DEFAULT 1,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (artist_id, language_id)
);

CREATE INDEX IF NOT EXISTS artist_languages_language_idx
  ON artist_languages(language_id, artist_id);
CREATE INDEX IF NOT EXISTS artists_name_search_idx
  ON artists USING gin (to_tsvector('simple', name));
