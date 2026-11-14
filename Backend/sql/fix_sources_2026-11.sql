-- Clear 5 wrong `source` values (Nov 2026).
--
-- The old source auto-detect took any trailing "– text" as the speaker, so the
-- end of these vachans was stored as their source. The vachan text itself is
-- untouched. Real attributions (પ્રમુખસ્વામી મહારાજ, પ્ર. સ્વા. મ, …) are kept.
--
--   id  | date       | wrong source
--   511 | 2022-12-16 | अक्षरधाम
--   544 | 2023-12-19 | આપણું જીવન
--   551 | 2023-12-26 | સંતનો સમાગમ
--   579 | 2024-12-24 | બધામાં દિવ્યભાવ!
--   585 | 2024-12-30 | મોક્ષની કમાણી
--
-- Guarded by id AND current value, so it only touches rows still holding the
-- wrong text. It is all-or-nothing: it updates exactly 5 rows (or 0 if already
-- applied) and otherwise raises and rolls back. Safe to re-run.
--
--   psql "$DBURL" -f sql/fix_sources_2026-11.sql

BEGIN;

DO $$
DECLARE
    n int;
BEGIN
    UPDATE messages SET source = NULL
    WHERE (id, source) IN (
        (511, 'अक्षरधाम'),
        (544, 'આપણું જીવન'),
        (551, 'સંતનો સમાગમ'),
        (579, 'બધામાં દિવ્યભાવ!'),
        (585, 'મોક્ષની કમાણી')
    );
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n NOT IN (0, 5) THEN
        RAISE EXCEPTION 'Expected 5 rows (or 0 if already applied), got % — rolled back', n;
    END IF;
    RAISE NOTICE 'Cleared source on % row(s)', n;
END $$;

COMMIT;
