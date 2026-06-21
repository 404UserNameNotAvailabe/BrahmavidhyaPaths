-- Brahmavidya archive — authoritative schema (single source of truth).
-- Run once against a fresh database:
--     psql "$DBURL" -f sql/schema.sql
--
-- Derived columns (normalized_text, year) are computed BY POSTGRES so they can
-- never drift from message_text / message_date or from the app's own logic.

CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- trigram similarity (fuzzy match)
CREATE EXTENSION IF NOT EXISTS vector;    -- pgvector (semantic match)

-- Managed category vocabulary (broad editorial grouping for the archive).
-- The name is the key, so messages.category stores the name directly and the
-- FK keeps it valid. This is archive metadata — NOT used by duplication.
CREATE TABLE IF NOT EXISTS categories (
    name        text PRIMARY KEY CHECK (btrim(name) <> ''),
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- The archived Gujarati message.
    message_text    text NOT NULL CHECK (btrim(message_text) <> ''),

    -- Normalized form for exact-dup detection + trigram similarity.
    -- Mirrors matching.normalize_text(): lower(collapse-ws(trim(text))).
    normalized_text text GENERATED ALWAYS AS
                      (lower(regexp_replace(btrim(message_text), '\s+', ' ', 'g'))) STORED,

    -- The only real metadata the corpus has.
    message_date    date,
    year            int GENERATED ALWAYS AS (extract(year FROM message_date)::int) STORED,

    -- Dhanurmas season (start year). The season spans Dec→Jan across two
    -- English years, so December belongs to that year's season and January
    -- to the previous year's. e.g. Dec 2016 and Jan 2017 → season 2016.
    season          int GENERATED ALWAYS AS (
                        CASE WHEN extract(month FROM message_date) >= 7
                             THEN extract(year FROM message_date)::int
                             ELSE extract(year FROM message_date)::int - 1 END
                    ) STORED,

    -- Archive categorization (editorial). One broad category + many free tags.
    -- category is a name from the managed `categories` list (app-enforced).
    category        text,
    tags            text[] NOT NULL DEFAULT '{}',

    -- Who the vachan is attributed to (Shriji Maharaj, Pramukh Swami, …).
    source          text,
    -- Admin-starred important vachans.
    is_favorite     boolean NOT NULL DEFAULT false,

    -- Gemini embedding; dimension must match EMBED_DIM (768).
    embedding       vector(768),

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Upgrade path: add columns that may be missing on a `messages` table created
-- before they existed. Idempotent, so running schema.sql again is always safe
-- (the app runs it on every startup to keep the DB in sync with the code).
ALTER TABLE messages ADD COLUMN IF NOT EXISTS category text;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS tags text[] NOT NULL DEFAULT '{}';
ALTER TABLE messages ADD COLUMN IF NOT EXISTS source text;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS is_favorite boolean NOT NULL DEFAULT false;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS season int GENERATED ALWAYS AS (
    CASE WHEN extract(month FROM message_date) >= 7
         THEN extract(year FROM message_date)::int
         ELSE extract(year FROM message_date)::int - 1 END
) STORED;

-- No two messages with the same normalized text.
CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_normalized
    ON messages (normalized_text);

-- Trigram index for fast similarity() scans.
CREATE INDEX IF NOT EXISTS ix_messages_normalized_trgm
    ON messages USING gin (normalized_text gin_trgm_ops);

-- HNSW cosine index for the semantic layer.
CREATE INDEX IF NOT EXISTS ix_messages_embedding_hnsw
    ON messages USING hnsw (embedding vector_cosine_ops);

-- Archive ordering / filtering.
CREATE INDEX IF NOT EXISTS ix_messages_date ON messages (message_date DESC);
CREATE INDEX IF NOT EXISTS ix_messages_category ON messages (category);
CREATE INDEX IF NOT EXISTS ix_messages_tags ON messages USING gin (tags);
CREATE INDEX IF NOT EXISTS ix_messages_source ON messages (source);
CREATE INDEX IF NOT EXISTS ix_messages_favorite ON messages (is_favorite) WHERE is_favorite;
CREATE INDEX IF NOT EXISTS ix_messages_season ON messages (season);

-- Keep updated_at fresh on every UPDATE (e.g. embedding backfill).
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_messages_updated_at ON messages;
CREATE TRIGGER trg_messages_updated_at
    BEFORE UPDATE ON messages
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
