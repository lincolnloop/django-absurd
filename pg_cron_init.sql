-- Runs ONCE, on a FRESH data volume, via /docker-entrypoint-initdb.d. The initdb
-- scripts run against POSTGRES_DB (the test image leaves it as `postgres`; the demo
-- sets it to `demo`), so we \connect the CENTRAL pg_cron metadata DB explicitly —
-- `postgres`, which every service starts with `-c cron.database_name=postgres`. The
-- extension lives ONLY here; app/test databases hold no extension and schedule
-- cross-database via cron.schedule_in_database(..., current_database()).
--
-- This IS the operator recipe: on a managed cluster substitute your scheduling role
-- for `postgres` below. For this test/dev image the role IS `postgres` (the cluster
-- owner), so the GRANTs are no-ops — kept so the file documents the full set of
-- privileges a non-owner scheduling role needs.
\connect postgres

CREATE EXTENSION IF NOT EXISTS pg_cron;

GRANT USAGE ON SCHEMA cron TO postgres;

GRANT EXECUTE ON FUNCTION
    cron.schedule_in_database(text, text, text, text, text, boolean)
    TO postgres;

-- REQUIRED even though schedule_in_database can create jobs: its `active` argument
-- only takes effect on INSERT, so disabling an already-scheduled job needs alter_job.
GRANT EXECUTE ON FUNCTION
    cron.alter_job(bigint, text, text, text, text, boolean)
    TO postgres;
