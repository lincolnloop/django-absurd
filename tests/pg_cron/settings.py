import os

from tests.settings import *  # noqa: F403

INSTALLED_APPS = [*INSTALLED_APPS, "django_absurd.pg_cron"]  # noqa: F405

DATABASES["default"]["HOST"] = os.environ.get("PGHOST", "localhost")  # noqa: F405
DATABASES["default"]["PORT"] = os.environ.get("PGPORT_PGCRON", "5434")  # noqa: F405
# TEST db name must equal db_pg_cron's cron.database_name so CREATE EXTENSION works.
DATABASES["default"]["TEST"] = {"NAME": "absurd_test_pg_cron"}  # noqa: F405

# A second alias on the SAME physical DB (identical TEST NAME → the test runner
# mirrors it, so it isn't created/migrated twice). Its only job is to exercise the
# cross-database guard: a ScheduledTask write via this alias has using != the absurd
# database.
DATABASES["replica"] = dict(DATABASES["default"])  # noqa: F405
