"""End-to-end test: sync a schedule via pg_cron, fire the wrapper directly,
drain the queue, and assert the task result is persisted.
"""

import os
import time

import pytest
from django.core.management import call_command
from django.db import connection

from django_absurd.backends import get_absurd_backends
from django_absurd.pg_cron.reconcile import sync_crons
from tests.models import Payload

pytestmark = pytest.mark.django_db(transaction=True)

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


@pytest.mark.skipif(
    not os.environ.get("ABSURD_PGCRON_LIVE"),
    reason="slow (~1min): waits for the real pg_cron launcher to fire; set "
    "ABSURD_PGCRON_LIVE=1 to run",
)
def test_e2e_pg_cron_launcher_fires_wrapper_and_spawns(settings):
    """Let the real pg_cron launcher fire the stored job — not the test session
    calling the wrapper directly. This is the only path that runs the wrapper as
    the job's own role under its own search_path (the SET search_path=pg_catalog
    in the wrapper definition is load-bearing precisely there). Assert the launch
    succeeded and actually spawned the task, then drain it."""
    settings.TASKS = TASKS_PG_CRON

    call_command("absurd_sync_queues")
    sync_crons(get_absurd_backends()["default"])
    jobname = "absurd:settings:default:e"

    deadline = time.monotonic() + 100
    status = None
    while time.monotonic() < deadline:
        with connection.cursor() as cur:
            cur.execute(
                "select d.status from cron.job_run_details d "
                "join cron.job j using (jobid) where j.jobname = %s "
                "and d.status in ('succeeded', 'failed') order by d.runid desc limit 1",
                [jobname],
            )
            row = cur.fetchone()
        if row is not None:
            status = row[0]
            break
        time.sleep(2)

    assert status == "succeeded", (
        f"launcher run status was {status!r}, expected succeeded"
    )

    call_command("absurd_worker", queue="default", burst=True)
    assert Payload.objects.filter(data="e2e").exists(), (
        "launcher-spawned task did not produce its Payload row"
    )
