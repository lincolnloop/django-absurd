import os

from tests.settings import *  # noqa: F403

INSTALLED_APPS = [*INSTALLED_APPS, "django_absurd.pg_cron"]  # noqa: F405

DATABASES["default"]["HOST"] = os.environ.get("PGHOST", "localhost")  # noqa: F405
DATABASES["default"]["PORT"] = os.environ.get("PGPORT_PGCRON", "5434")  # noqa: F405
# TEST db name must equal db_pg_cron's cron.database_name so CREATE EXTENSION works.
DATABASES["default"]["TEST"] = {"NAME": "absurd_test_pg_cron"}  # noqa: F405
