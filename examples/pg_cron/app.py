"""nanodjango demo: django-absurd pg_cron scheduler.

Postgres fires `ping` every minute directly via pg_cron (no beat process); the
worker drains it, logs 'pong 🏓', and returns 'pong' as the task result. The
extension lives on the central `postgres` database (installed by the compose db
image, not a migration); `demo` holds no extension and is scheduled into
cross-database. Watch Tasks/Runs in the admin.

    docker compose up
    http://localhost:8000/   → the admin (Tasks / Runs / …); login admin / admin
"""

import logging
import os

import dj_database_url
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.tasks import task
from nanodjango import Django

app = Django(
    ADMIN_URL="admin/",
    EXTRA_APPS=["django_absurd", "django_absurd.pg_cron"],  # pg_cron app AFTER core
    # Managed platforms inject DATABASE_URL and rotate the credentials inside it, so
    # PG* vars would go stale. The `demo` database is this example's own, not the
    # central pg_cron catalog.
    DATABASES={
        "default": dj_database_url.parse(
            os.environ.get(
                "DATABASE_URL", "postgres://postgres:postgres@localhost:5432/demo"
            )
        )
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
