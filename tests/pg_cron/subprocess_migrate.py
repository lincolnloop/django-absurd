"""Standalone entrypoint for test_sync_schedules_on_migrate.py's subprocess-based
real-DB test. Run as `python tests/pg_cron/subprocess_migrate.py` in a genuinely
separate process — its own fresh PgCronConfig.ready() never sees a test-DB swap, so
is_test_db correctly evaluates False there. Connection params and
SYNC_SCHEDULES_ON_MIGRATE come from environment variables (never string-interpolated
into source) to avoid embedding untrusted values in generated code."""

import os

import django
from django.conf import settings
from django.core.management import call_command

options: dict[str, object] = {
    "QUEUES": {"default": {}},
    "SCHEDULE": {
        "nightly": {"task": "tests.pg_cron.tasks.add", "cron": "0 2 * * *"},
    },
}
sync_on_migrate = os.environ.get("SYNC_SCHEDULES_ON_MIGRATE")
if sync_on_migrate is not None:
    options["SYNC_SCHEDULES_ON_MIGRATE"] = sync_on_migrate == "1"

settings.configure(
    DATABASES={
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["SUBPROCESS_MIGRATE_DBNAME"],
            "USER": os.environ.get("SUBPROCESS_MIGRATE_USER", ""),
            "PASSWORD": os.environ.get("SUBPROCESS_MIGRATE_PASSWORD", ""),
            "HOST": os.environ.get("SUBPROCESS_MIGRATE_HOST", "localhost"),
            "PORT": os.environ.get("SUBPROCESS_MIGRATE_PORT", ""),
        }
    },
    INSTALLED_APPS=["django_absurd", "django_absurd.pg_cron"],
    TASKS={
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": options,
        }
    },
    DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
    USE_TZ=True,
)
django.setup()
call_command("migrate", verbosity=0)
