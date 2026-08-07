-- Runs once, when Postgres initialises an empty data directory.
--
-- btree_gist lets a GiST index hold scalar columns (room_id) alongside a
-- range (the session's time span). That combination is what makes the
-- no-double-booking rule expressible as a database constraint:
--
--   EXCLUDE USING gist (room_id WITH =, during WITH &&)
--
-- Enforcing it in the database rather than in application code means two
-- simultaneous bookings cannot both pass a check and both commit.
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- The test database is created fresh by pytest and does not inherit
-- extensions from ona_dev, so it needs its own. Django's test runner copies
-- from template1, so installing it there covers every future test database.
\c template1
CREATE EXTENSION IF NOT EXISTS btree_gist;
