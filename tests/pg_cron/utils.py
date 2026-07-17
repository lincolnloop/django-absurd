"""Shared helpers for the pg_cron test suite (plain functions — fixtures live in
conftest.py; pg_cron catalog queries live on ``ScheduledTask.pg_cron``)."""

import typing as t

ABSURD_BACKEND: str = "django_absurd.backends.AbsurdBackend"
DECLARED_QUEUES: dict[str, dict[str, t.Any]] = {
    "default": {},
    "other": {},
    "reports": {},
}


def build_pg_cron_tasks(schedule: dict[str, t.Any]) -> dict[str, t.Any]:
    return {
        "default": {
            "BACKEND": ABSURD_BACKEND,
            "OPTIONS": {
                "QUEUES": DECLARED_QUEUES,
                "SCHEDULER": "pg_cron",
                "SCHEDULE": schedule,
            },
        }
    }


def build_beat_tasks(schedule: dict[str, t.Any]) -> dict[str, t.Any]:
    return {
        "default": {
            "BACKEND": ABSURD_BACKEND,
            "OPTIONS": {
                "QUEUES": DECLARED_QUEUES,
                "SCHEDULER": "beat",
                "SCHEDULE": schedule,
            },
        }
    }
