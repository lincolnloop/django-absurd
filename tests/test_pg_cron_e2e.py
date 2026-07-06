"""End-to-end test: sync a schedule via pg_cron, fire the wrapper directly,
drain the queue, and assert the task result is persisted.

Marked ``pg_cron`` (deselected by default). Run with::

    uv run pytest -m pg_cron tests/test_pg_cron_e2e.py
"""

import pytest
from django.core.management import call_command
from django.db import connection

from django_absurd.backends import get_absurd_backends
from django_absurd.pg_cron.reconcile import sync_crons
from tests.models import Payload

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.pg_cron,
    pytest.mark.usefixtures("ensure_pg_cron", "_clear_owned_pg_cron_jobs"),
]

ABSURD = "django_absurd.backends.AbsurdBackend"

TASKS_PG_CRON = {
    "default": {
        "BACKEND": ABSURD,
        "OPTIONS": {
            "QUEUES": {"default": {}, "other": {}, "reports": {}},
            "SCHEDULER": "pg_cron",
            "SCHEDULE": {
                "e": {
                    "task": "tests.tasks.create_payload",
                    "cron": "* * * * *",
                    "args": ["e2e"],
                },
            },
        },
    }
}


def test_e2e_sync_fire_worker_assert_payload(settings):
    """Sync schedule into pg_cron, fire wrapper directly, drain queue, assert row."""
    settings.TASKS = TASKS_PG_CRON

    call_command("absurd_sync_queues")
    sync_crons(get_absurd_backends()["default"])

    with connection.cursor() as cur:
        cur.execute(
            "select public.django_absurd_run_scheduled(%s, %s, %s)",
            ["settings", "default", "e"],
        )

    call_command("absurd_worker", queue="default", burst=True)

    payload = Payload.objects.filter(data="e2e").first()
    assert payload is not None, "Payload row with data='e2e' was not created"
