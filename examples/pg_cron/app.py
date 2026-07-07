"""nanodjango demo: django-absurd pg_cron scheduler.

Postgres fires `ping` every minute directly via pg_cron (no beat process); the
worker drains it and logs 'pong 🏓'. The `django_absurd.pg_cron` app's migration
creates the extension. Watch Tasks/Runs in the admin.

    docker compose up
    http://localhost:8000/admin/   Tasks / Runs / … (superuser: admin / admin)
"""

import logging
import os

from django.tasks import task
from nanodjango import Django

app = Django(
    ADMIN_URL="admin/",
    EXTRA_APPS=["django_absurd", "django_absurd.pg_cron"],  # pg_cron app AFTER core
    DATABASES={
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("PGDATABASE", "demo"),
            "USER": os.environ.get("PGUSER", "postgres"),
            "PASSWORD": os.environ.get("PGPASSWORD", "postgres"),
            "HOST": os.environ.get("PGHOST", "localhost"),
            "PORT": os.environ.get("PGPORT", "5432"),
        }
    },
    TASKS={
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {
                "QUEUES": {"default": {}},
                "SCHEDULER": "pg_cron",
                "SCHEDULE": {"ping": {"task": "app.ping", "cron": "* * * * *"}},
            },
        }
    },
    LOGGING={
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {"console": {"class": "logging.StreamHandler"}},
        "loggers": {
            "django_absurd": {"handlers": ["console"], "level": "INFO"},
            "demo": {"handlers": ["console"], "level": "INFO"},
        },
    },
)

logger = logging.getLogger("demo")


@task
def ping() -> None:
    """Fired every minute by pg_cron; the worker runs it and logs 'pong 🏓'."""
    logger.info("pong 🏓")


if __name__ == "__main__":
    app.run()
