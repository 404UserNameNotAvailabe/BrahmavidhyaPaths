-- Dry-run schema.sql against the live DB and ROLL BACK (AGENTS.md: test every
-- schema change this way before merging). Nothing is kept.
--
--   cd Backend && psql "$DBURL" -f sql/dryrun_schema.sql
--
-- Expect: v2_marker = t after pass 1, rows = distinct_keys = 270, every index
-- valid, the filenode unchanged between pass 1 and pass 2 (no second rewrite),
-- the duplicate INSERT returning 0 rows, and v2_marker_live = f at the end.

\set ON_ERROR_STOP 1
SET lock_timeout = 5000;
BEGIN;

\echo '--- before'
SELECT pg_relation_filenode('messages') AS filenode_before;

\echo '--- pass 1'
\i sql/schema.sql

\echo '--- after pass 1'
SELECT pg_get_expr(d.adbin, d.adrelid) LIKE '%।॥%' AS v2_marker
FROM pg_attrdef d
JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
WHERE d.adrelid = 'messages'::regclass AND a.attname = 'normalized_text';
SELECT pg_relation_filenode('messages') AS filenode_after_pass1;
SELECT count(*) AS rows, count(DISTINCT normalized_text) AS distinct_keys FROM messages;
SELECT indexrelid::regclass AS index, indisvalid AS valid FROM pg_index WHERE indrelid = 'messages'::regclass ORDER BY 1;
SELECT id, normalized_text FROM messages WHERE id IN (361, 533) ORDER BY id;

\echo '--- pass 2 (must NOT rewrite: filenode same as pass 1)'
\i sql/schema.sql
SELECT pg_relation_filenode('messages') AS filenode_after_pass2;

\echo '--- 2016-12-16 vachan without its punctuation: must conflict (0 rows)'
INSERT INTO messages (message_text) VALUES ('સંપ રાખે તે બુદ્ધિમાન છે નહિ તો બુદ્ધુમાન')
ON CONFLICT (normalized_text) DO NOTHING RETURNING id;

ROLLBACK;

\echo '--- rolled back; live column is still v1:'
SELECT pg_get_expr(d.adbin, d.adrelid) LIKE '%।॥%' AS v2_marker_live
FROM pg_attrdef d
JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
WHERE d.adrelid = 'messages'::regclass AND a.attname = 'normalized_text';
