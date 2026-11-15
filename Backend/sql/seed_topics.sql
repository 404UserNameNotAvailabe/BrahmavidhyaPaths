-- The team's 25 topics (Vishay), in the sheet's order. Run once after the
-- schema is live; safe to re-run (existing names are left alone).
--
--   psql "$DBURL" -f sql/seed_topics.sql
--
-- Read from a PDF print of the sheet: check every spelling against the
-- sheet's Vishay dropdown before running.

INSERT INTO topics (name, sort_order) VALUES
    ('આત્મનિષ્ઠા', 1),
    ('કથાવાર્તા', 2),
    ('ગુણગ્રહણ / અભાવ-અવગુણ', 3),
    ('ચરિત્રો', 4),
    ('તપ', 5),
    ('દાસભાવ', 6),
    ('ધર્મ-નિયમ', 7),
    ('નિષ્ઠા', 8),
    ('પ્રગટ', 9),
    ('પ્રાપ્તિનો વિચાર', 10),
    ('ભક્તિ', 11),
    ('મંદિર', 12),
    ('માન-અપમાન', 13),
    ('મહિમા', 14),
    ('રાજીપો', 15),
    ('રવિસભા', 16),
    ('લગ્ન', 17),
    ('સત્સંગ', 18),
    ('સંપ', 19),
    ('સમજણ', 20),
    ('સમર્પણ', 21),
    ('સરળતા', 22),
    ('સેવા', 23),
    ('સુખ-દુઃખ', 24),
    ('સ્વભાવ', 25)
ON CONFLICT (name) DO NOTHING;
