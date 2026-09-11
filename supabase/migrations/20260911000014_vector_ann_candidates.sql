-- Optional hybrid-retrieval layer.  Lexical provider search remains the
-- source of truth; these vectors are only an additional recommendation and
-- semantic-search candidate source.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS music_embeddings (
  canonical_id TEXT NOT NULL,
  entity_type TEXT NOT NULL CHECK (entity_type IN ('track', 'artist', 'album', 'playlist')),
  provider TEXT NOT NULL DEFAULT 'catalog',
  language TEXT,
  playable BOOLEAN NOT NULL DEFAULT TRUE,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  embedding vector(384) NOT NULL,
  embedding_model TEXT NOT NULL DEFAULT 'multilingual-metadata-384',
  embedding_version TEXT NOT NULL DEFAULT 'v1',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (canonical_id, entity_type, embedding_model, embedding_version)
);

CREATE INDEX IF NOT EXISTS music_embeddings_track_filter_idx
  ON music_embeddings (entity_type, playable, language);
CREATE INDEX IF NOT EXISTS music_embeddings_hnsw_cosine_idx
  ON music_embeddings USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- A user vector is an incrementally updated, aggregated taste signal. Raw
-- playback URLs are intentionally never stored here or in music_embeddings.
CREATE TABLE IF NOT EXISTS user_taste_vectors (
  user_id TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  embedding vector(384) NOT NULL,
  embedding_model TEXT NOT NULL DEFAULT 'multilingual-metadata-384',
  embedding_version TEXT NOT NULL DEFAULT 'v1',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, embedding_model, embedding_version)
);

