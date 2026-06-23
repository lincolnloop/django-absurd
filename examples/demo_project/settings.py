"""Minimal Django project wiring django-absurd as the TASKS backend."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "django-absurd-example-insecure-key"  # noqa: S105 -- test app only
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django_absurd",
    "demo",
]

# Absurd is Postgres-native and requires the psycopg (v3) backend — Django selects
# psycopg3 automatically for this ENGINE when the `psycopg` package is installed.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("PGDATABASE", "postgres"),
        "USER": os.environ.get("PGUSER", "postgres"),
        "PASSWORD": os.environ.get("PGPASSWORD", "postgres"),
        "HOST": os.environ.get("PGHOST", "localhost"),
        "PORT": os.environ.get("PGPORT", "5434"),
    },
}

# Routes the django_absurd app to its DATABASE (here, "default"). Harmless on a
# single-DB setup; required if you run Absurd on a separate alias.
DATABASE_ROUTERS = ["django_absurd.routers.AbsurdRouter"]

# The Django Tasks backend. Queues + their policies are declared here and synced to
# Postgres with `manage.py absurd_sync_queues`.
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "QUEUES": ["default"],
        "OPTIONS": {"DATABASE": "default"},
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True

# Surface the worker's per-task logging on the console so `absurd_worker` shows
# what ran.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"django_absurd": {"handlers": ["console"], "level": "INFO"}},
}
