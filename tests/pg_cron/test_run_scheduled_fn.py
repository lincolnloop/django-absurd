import json

import pytest
from django.core.management import call_command
from django.db import connection

from django_absurd.pg_cron.models import ScheduledTask
from tests.models import Payload
from tests.tasks import capped, on_reports

pytestmark = pytest.mark.django_db(transaction=True)


def test_capped_task_returns_sum() -> None:
    assert capped.func(3, 4) == 7


def test_on_reports_task_returns_string() -> None:
    assert on_reports.func() == "on_reports"


def fire_wrapper(source: str, name: str) -> None:
    with connection.cursor() as cur:
        cur.execute(
            "select public.django_absurd_run_scheduled(%s, %s)",
            [source, name],
        )


def test_fires_task_from_row() -> None:
    call_command("absurd_sync_queues")
    ScheduledTask.objects.create(
        source="s",
        name="p",
        task="tests.tasks.create_payload",
        queue="default",
        args=["tick"],
        kwargs={},
        cron="* * * * *",
    )
    fire_wrapper("s", "p")
    call_command("absurd_worker", queue="default", burst=True)
    assert Payload.objects.count() == 1


def test_missing_row_is_noop() -> None:
    fire_wrapper("s", "nope")  # no exception


def test_disabled_row_is_noop() -> None:
    ScheduledTask.objects.create(
        source="s",
        name="off",
        task="tests.tasks.create_payload",
        queue="default",
        args=["x"],
        kwargs={},
        cron="* * * * *",
        enabled=False,
    )
    fire_wrapper("s", "off")
    call_command("absurd_worker", queue="default", burst=True)
    assert Payload.objects.count() == 0


def test_wrapper_rebuilds_retry_strategy_from_columns() -> None:
    call_command("absurd_sync_queues")
    ScheduledTask.objects.create(
        source="s",
        name="retry_opts",
        task="tests.tasks.capped",
        queue="default",
        args=[1, 2],
        kwargs={},
        retry_kind="fixed",
        retry_base_seconds=1.5,
        cron="* * * * *",
    )
    fire_wrapper("s", "retry_opts")
    with connection.cursor() as cur:
        cur.execute(
            "SELECT retry_strategy::text FROM absurd.tasks_view WHERE queue = %s",
            ["default"],
        )
        row = cur.fetchone()
    assert row is not None
    assert json.loads(row[0]) == {"kind": "fixed", "base_seconds": 1.5}


def test_wrapper_rebuilds_cancellation_from_columns() -> None:
    call_command("absurd_sync_queues")
    ScheduledTask.objects.create(
        source="s",
        name="cancel_opts",
        task="tests.tasks.capped",
        queue="default",
        args=[1, 2],
        kwargs={},
        cancellation_max_duration=30,
        cron="* * * * *",
    )
    fire_wrapper("s", "cancel_opts")
    with connection.cursor() as cur:
        cur.execute(
            "SELECT cancellation::text FROM absurd.tasks_view WHERE queue = %s",
            ["default"],
        )
        row = cur.fetchone()
    assert row is not None
    assert json.loads(row[0]) == {"max_duration": 30}


def test_wrapper_reassembles_options_from_columns() -> None:
    """Wrapper fn builds params/options jsonb from named columns server-side.

    max_attempts isn't observable via worker side effects so we inspect the
    spawned task row directly via the absurd.tasks_view (params is a jsonb blob).
    """
    call_command("absurd_sync_queues")
    ScheduledTask.objects.create(
        source="s",
        name="opts",
        task="tests.tasks.capped",
        queue="default",
        args=[1, 2],
        kwargs={},
        max_attempts=5,
        cron="* * * * *",
    )
    fire_wrapper("s", "opts")
    with connection.cursor() as cur:
        cur.execute(
            "SELECT params::text, max_attempts FROM absurd.tasks_view WHERE queue = %s",
            ["default"],
        )
        row = cur.fetchone()
    assert row is not None
    params_text, max_attempts = row
    assert json.loads(params_text) == {"args": [1, 2], "kwargs": {}}
    assert max_attempts == 5
