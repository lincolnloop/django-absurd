import os
import typing as t

from tests.settings import *  # noqa: F403

INSTALLED_APPS = [*INSTALLED_APPS, "django_absurd.pg_cron"]  # noqa: F405

DATABASES["default"]["HOST"] = os.environ.get("PGHOST", "localhost")  # noqa: F405
DATABASES["default"]["PORT"] = os.environ.get("PGPORT_PGCRON", "5434")  # noqa: F405
# An ORDINARY test DB — NOT db_pg_cron's cron.database_name (the central `postgres`
# DB). The app/test DB holds no extension; jobs are scheduled cross-database into it
# from the central catalog, so the topology here is truly central != app.
DATABASES["default"]["TEST"] = {"NAME": "test_absurd_pg_cron"}  # noqa: F405

# A second alias on the SAME physical DB (identical TEST NAME → the test runner
# mirrors it, so it isn't created/migrated twice). Its only job is to exercise the
# cross-database guard: a ScheduledTask write via this alias has using != the absurd
# database.
DATABASES["replica"] = dict(DATABASES["default"])  # noqa: F405

# Suite base opt-in: this IS the pg_cron server suite, so scheduling on the test DB is
# the norm. Setting it on the BASE (not a per-test override) means the function-scoped
# ``settings`` fixture restores TASKS to this opt-in after each test, so the
# ``_post_teardown`` → ``flush_absurd_state`` → ``teardown_crons`` cleanup hook reads
# ``is_pg_cron_inert = False`` and its scoped sweep actually runs (an opted-in test's
# ``_dj:`` jobs are cleaned rather than leaking across ``--reuse-db``). Only the pg_cron
# axis; the migrate-auto-load axis (SYNC_SCHEDULES_ON_TEST_DB) is deliberately not set.
absurd_backend = t.cast("dict[str, t.Any]", TASKS["default"])  # noqa: F405
absurd_backend.setdefault("OPTIONS", {})["PG_CRON_ON_TEST_DB"] = True
