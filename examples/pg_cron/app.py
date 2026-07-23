"""nanodjango demo: django-absurd pg_cron scheduler.

Postgres fires `ping` every minute directly via pg_cron (no beat process); the
worker drains it, logs 'pong 🏓', and returns 'pong' as the task result. The
`django_absurd.pg_cron` app's migration creates the extension. Watch Tasks/Runs
in the admin.

    docker compose up
    http://localhost:8000/   → the admin (Tasks / Runs / …); login admin / admin
"""

import logging
import os

from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
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
def ping(message: str = "pong") -> str:
    """Fired every minute by pg_cron; the worker logs the message and returns it."""
    logger.info("%s 🏓", message)
    return message


@app.route("/")
def index(request: HttpRequest) -> HttpResponse:
    """This demo has no UI of its own — land on the admin."""
    return redirect("/admin/")


if __name__ == "__main__":  # pragma: no cover
    app.run()
