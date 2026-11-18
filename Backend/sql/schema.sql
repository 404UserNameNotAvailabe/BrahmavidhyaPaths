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

-- Settings a superadmin changes from the UI (menu visibility, Google Cloud).
-- One JSON value per key; never holds a secret (the service-account key
-- lives in the key file only).
CREATE TABLE IF NOT EXISTS app_settings (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text
);

CREATE TABLE IF NOT EXISTS messages (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- The archived Gujarati message.
    message_text    text NOT NULL CHECK (btrim(message_text) <> ''),

    -- Normalized form for exact-dup detection + trigram similarity:
    -- punctuation → space, collapse whitespace, trim, lowercase.
    -- Mirrors matching.normalize_text() — the character class must match
    -- matching.NORM_PUNCT exactly (same expression in the upgrade below).
    normalized_text text GENERATED ALWAYS AS (lower(btrim(regexp_replace(
                      regexp_replace(message_text, $re$[]\\.,!?;:'"“”‘’(){}[|/=+*&%#@~`^<>_–—…।॥-]$re$, ' ', 'g'),
                      '\s+', ' ', 'g')))) STORED,

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

-- normalized_text v2 (Nov 2026): punctuation no longer counts, so
-- "સંપ રાખે." and "સંપ રાખે" are the same vachan. Upgrades a table created with
-- the old expression (whitespace + case only). Guarded on the deparsed
-- expression ('।॥' appears only in v2) so the table is rewritten once, not on
-- every start. Needs PostgreSQL 17+ (SET EXPRESSION).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_attrdef d
        JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
        WHERE d.adrelid = 'messages'::regclass
          AND a.attname = 'normalized_text'
          AND pg_get_expr(d.adbin, d.adrelid) LIKE '%।॥%'
    ) THEN
        ALTER TABLE messages ALTER COLUMN normalized_text SET EXPRESSION AS (lower(btrim(regexp_replace(
            regexp_replace(message_text, $re$[]\\.,!?;:'"“”‘’(){}[|/=+*&%#@~`^<>_–—…।॥-]$re$, ' ', 'g'),
            '\s+', ' ', 'g'))));
    END IF;
END $$;

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

-- ===========================================================================
-- Candidates (Nov 2026): a pool of not-yet-approved sentences, each checked
-- against the archive by words, meaning and an AI judge. Kept apart from
-- `messages` so the archive stays clean. Seed data lives in
-- sql/seed_topics.sql, never here.
-- ===========================================================================

-- Controlled list of topics (Vishay). A topic name is also a tag value.
CREATE TABLE IF NOT EXISTS topics (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text UNIQUE NOT NULL CHECK (btrim(name) <> ''),
    sort_order  int NOT NULL DEFAULT 0,
    is_active   boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Lets the Archive show which rows still need a person to confirm topics.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS tags_reviewed boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS candidates (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    text             text NOT NULL CHECK (btrim(text) <> ''),
    -- Same duplicate key as messages.normalized_text (v2, punctuation-free).
    normalized_text  text GENERATED ALWAYS AS (lower(btrim(regexp_replace(
                       regexp_replace(text, $re$[]\\.,!?;:'"“”‘’(){}[|/=+*&%#@~`^<>_–—…।॥-]$re$, ' ', 'g'),
                       '\s+', ' ', 'g')))) STORED,
    match_key        text NOT NULL,              -- candidate_rules.match_key(text), set by the API
    lang             text,                       -- gu | hi | en | mixed
    topics           text[] NOT NULL DEFAULT '{}',  -- current topics, first = primary
    sheet_topics     text[] NOT NULL DEFAULT '{}',  -- as imported
    ai_topics        text[] NOT NULL DEFAULT '{}',  -- as assigned by the judge
    topics_confirmed boolean NOT NULL DEFAULT false,
    ref              text,                       -- scripture / book reference from the sheet
    collection       text,                       -- e.g. 'Unused sentences'
    sheet_row        int,                        -- the sheet's No column
    duplicate_of     bigint REFERENCES candidates(id) ON DELETE SET NULL,
    status           text NOT NULL DEFAULT 'pending',
    status_note      text,
    status_by        text,
    status_at        timestamptz,
    target_season    int,                        -- season start year once approved
    plan_date        date,                       -- later phase (season plan)
    verdict          text,                       -- given | same | similar | new
    verdict_reason   text,
    check_state      text NOT NULL DEFAULT 'queued',  -- queued | running | done | failed
    check_error      text,
    checked_at       timestamptz,
    judge_model      text,                       -- which model judged (null = words only)
    embedding        vector(768),
    message_id       bigint REFERENCES messages(id) ON DELETE SET NULL,  -- set when given
    created_by       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

DO $$ BEGIN
    ALTER TABLE candidates ADD CONSTRAINT ck_candidates_status
        CHECK (status IN ('pending','shortlisted','approved','rejected','given'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE candidates ADD CONSTRAINT ck_candidates_verdict
        CHECK (verdict IS NULL OR verdict IN ('given','same','similar','new'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE candidates ADD CONSTRAINT ck_candidates_check_state
        CHECK (check_state IN ('queued','running','done','failed'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE UNIQUE INDEX IF NOT EXISTS ux_candidates_normalized ON candidates (normalized_text);
CREATE INDEX IF NOT EXISTS ix_candidates_match_key   ON candidates (match_key);
CREATE INDEX IF NOT EXISTS ix_candidates_status      ON candidates (status);
CREATE INDEX IF NOT EXISTS ix_candidates_verdict     ON candidates (verdict);
CREATE INDEX IF NOT EXISTS ix_candidates_check_state ON candidates (check_state, id);
CREATE INDEX IF NOT EXISTS ix_candidates_checked_at  ON candidates (checked_at);
CREATE INDEX IF NOT EXISTS ix_candidates_topics      ON candidates USING gin (topics);

DROP TRIGGER IF EXISTS trg_candidates_updated_at ON candidates;
CREATE TRIGGER trg_candidates_updated_at
    BEFORE UPDATE ON candidates
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- One row per (candidate, archive path) worth showing (up to 5).
CREATE TABLE IF NOT EXISTS candidate_matches (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    candidate_id        bigint NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    message_id          bigint NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    rank                int NOT NULL,
    word_kind           text NOT NULL,   -- exact | contained | strong | some | weak | none
    overlap_pct         int NOT NULL,
    shared_phrase_words int NOT NULL,
    semantic_sim        real,            -- ordering/debugging only, never shown
    judge               text,            -- same_sentence | same_message | similar | different
    judge_reason        text,
    UNIQUE (candidate_id, message_id)
);
CREATE INDEX IF NOT EXISTS ix_candidate_matches_candidate ON candidate_matches (candidate_id, rank);

-- Season plan: at most one path per day (approved = planned, given = done).
CREATE UNIQUE INDEX IF NOT EXISTS ux_candidates_plan_date ON candidates (plan_date)
    WHERE plan_date IS NOT NULL AND status IN ('approved', 'given');
-- True when Mark as given inserted the archive row (rather than linking to one
-- that already existed): only then may Undo remove it again.
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS given_created_message boolean NOT NULL DEFAULT false;
