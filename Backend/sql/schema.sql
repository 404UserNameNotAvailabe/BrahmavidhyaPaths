-- Brahmavidya archive — authoritative schema (single source of truth).
-- Run once against a fresh database:
--     psql "$DBURL" -f sql/schema.sql
--
-- Derived columns (normalized_text, year) are computed BY POSTGRES so they can
-- never drift from message_text / message_date or from the app's own logic.

CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- trigram similarity (fuzzy match)
CREATE EXTENSION IF NOT EXISTS vector;    -- pgvector (semantic match)

-- Admin accounts + opaque session tokens (DB-backed, revocable).
CREATE TABLE IF NOT EXISTS users (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username      text UNIQUE NOT NULL CHECK (btrim(username) <> ''),
    password_hash text NOT NULL,
    role          text NOT NULL DEFAULT 'admin',
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    token       text PRIMARY KEY,
    user_id     bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sessions_expires ON sessions (expires_at);

-- Audit trail of who did what (logins, user + content changes).
CREATE TABLE IF NOT EXISTS audit_log (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at        timestamptz NOT NULL DEFAULT now(),
    username  text,
    action    text NOT NULL,
    detail    text
);
CREATE INDEX IF NOT EXISTS ix_audit_at ON audit_log (at DESC);

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

    -- Archive grouping (editorial): free tags. Not used by duplication.
    tags            text[] NOT NULL DEFAULT '{}',

    -- Who the vachan is attributed to (Shriji Maharaj, Pramukh Swami, …).
    source          text,
    -- Original published source (http/https URL) of the vachan.
    link            text,
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
ALTER TABLE messages ADD COLUMN IF NOT EXISTS tags text[] NOT NULL DEFAULT '{}';
ALTER TABLE messages ADD COLUMN IF NOT EXISTS source text;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS is_favorite boolean NOT NULL DEFAULT false;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS link text;

-- Category was merged into tags. Fold any category into tags first so nothing
-- is lost, then drop the column + its vocabulary table.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'messages' AND column_name = 'category') THEN
        UPDATE messages SET tags = array_prepend(category, tags)
        WHERE category IS NOT NULL AND btrim(category) <> ''
          AND NOT category = ANY(tags);
        ALTER TABLE messages DROP COLUMN category;
    END IF;
END $$;
DROP TABLE IF EXISTS categories;

-- Every vachan is dated inside Dhanurmas: 16 Dec → 14 Jan.
DO $$
BEGIN
    ALTER TABLE messages ADD CONSTRAINT ck_messages_dhanurmas CHECK (
        message_date IS NULL
        OR (extract(month FROM message_date) = 12 AND extract(day FROM message_date) >= 16)
        OR (extract(month FROM message_date) = 1  AND extract(day FROM message_date) <= 14)
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Original link must be a full http(s) URL.
DO $$
BEGIN
    ALTER TABLE messages ADD CONSTRAINT ck_messages_link CHECK (link IS NULL OR link ~* '^https?://\S+$');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
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
