-- Create the pg_cron extension.
--
-- This script runs automatically at first startup of the Postgres container
-- (docker-entrypoint-initdb.d is run as superuser during initial DB creation).
-- It demonstrates the operator-side installation step: pg_cron requires
-- shared_preload_libraries (set in compose.yaml command) and superuser
-- privileges, so it cannot be installed by an ordinary application role via
-- a Django migration.  In a production setup, an operator runs this once, or
-- it is provided via the managed-Postgres parameter group / extension list.
CREATE EXTENSION IF NOT EXISTS pg_cron;
