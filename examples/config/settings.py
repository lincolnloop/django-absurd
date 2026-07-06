"""Settings for the django-absurd scheduler demo.

Minimal, single-purpose: enough Django to run migrations, the admin, and
Absurd workers. django-absurd is the Django Tasks backend. Two backends share
the default database: ``"default"`` uses ``SCHEDULER="pg_cron"`` (no beat
process), and ``"beat"`` uses ``SCHEDULER="beat"`` (co-located worker+beat).
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "insecure-demo-key-do-not-use-in-production"  # noqa: S105
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_absurd",
    "django_absurd.pg_cron",  # opt-in pg_cron scheduler app (after "django_absurd")
    "demo",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# HARD REQUIREMENT: PostgreSQL via the psycopg (v3) backend. The Absurd SDK
# reuses Django's connection and needs psycopg3 — sqlite / psycopg2 won't work.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("PGDATABASE", "demo"),
        "USER": os.environ.get("PGUSER", "postgres"),
        "PASSWORD": os.environ.get("PGPASSWORD", "postgres"),
        "HOST": os.environ.get("PGHOST", "localhost"),
        "PORT": os.environ.get("PGPORT", "5432"),
    },
}

TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "OPTIONS": {
            "QUEUES": {"default": {}},
            # Database-side scheduler: Postgres fires the schedule directly via
            # pg_cron — no beat process. On each `migrate`, the
            # django_absurd.pg_cron app's post_migrate handler reconciles
            # SCHEDULE into pg_cron jobs.
            "SCHEDULER": "pg_cron",
            "SCHEDULE": {
                "ping": {"task": "demo.tasks.ping", "cron": "* * * * *"},
            },
        },
    },
    "beat": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "OPTIONS": {
            "QUEUES": {"beat": {}},
            # Beat scheduler: a co-located worker+beat process fires the
            # schedule from Python — no pg_cron required.
            "SCHEDULER": "beat",
            "SCHEDULE": {
                "tick": {
                    "task": "demo.tasks.tick",
                    "cron": "* * * * *",
                    "queue": "beat",
                },
            },
        },
    },
}

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True

# Surface django-absurd's per-task worker logging and the demo task output.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "django_absurd": {"handlers": ["console"], "level": "INFO"},
        "demo": {"handlers": ["console"], "level": "INFO"},
    },
}
