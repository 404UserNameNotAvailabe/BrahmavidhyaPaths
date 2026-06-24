-- Add categorization to a `messages` table that was created before the
-- category/tags feature. Idempotent; preserves existing rows + embeddings.
-- Run once:  psql "$DBURL" -f sql/migrate_categories.sql
-- (Fresh databases get all this from schema.sql and don't need it.)

CREATE TABLE IF NOT EXISTS categories (
    name        text PRIMARY KEY CHECK (btrim(name) <> ''),
    created_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS category text
        REFERENCES categories(name) ON UPDATE CASCADE ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS tags text[] NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS ix_messages_category ON messages (category);
CREATE INDEX IF NOT EXISTS ix_messages_tags ON messages USING gin (tags);
