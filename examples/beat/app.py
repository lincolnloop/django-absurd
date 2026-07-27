"""nanodjango demo: django-absurd BEAT scheduler.

An in-process beat fires `tick` every minute; the worker (run with --beat) drains
it and logs 'tock ⏰'. Watch Tasks/Runs fill in the admin.

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
    EXTRA_APPS=["django_absurd"],
    DATABASES={
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("PGDATABASE", "postgres"),
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
                "SCHEDULE": {"tick": {"task": "app.tick", "cron": "* * * * *"}},
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
def tick() -> None:
    """Fired every minute by the beat; the worker runs it and logs 'tock ⏰'."""
    logger.info("tock ⏰")


@app.route("/")
def index(request: HttpRequest) -> HttpResponse:
    """This demo has no UI of its own — land on the admin."""
    return redirect("/admin/")


if __name__ == "__main__":  # pragma: no cover
    app.run()
