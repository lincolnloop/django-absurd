import json

import pytest
from django.core.management import call_command
from django.db import connection

from django_absurd.pg_cron.models import ScheduledTask
from tests.models import Payload
from tests.tasks import capped, on_reports

pytestmark = pytest.mark.django_db(transaction=True)


def test_capped_task_returns_sum():
    assert capped.func(3, 4) == 7


def test_on_reports_task_returns_string():
    assert on_reports.func() == "on_reports"


def fire_wrapper(source: str, alias: str, name: str) -> None:
    with connection.cursor() as cur:
        cur.execute(
            "select public.django_absurd_run_scheduled(%s, %s, %s)",
            [source, alias, name],
        )


def test_fires_task_from_row() -> None:
    call_command("absurd_sync_queues")
    ScheduledTask.objects.create(
        source="settings",
        name="p",
        alias="default",
        task="tests.tasks.create_payload",
        queue="default",
        args=["tick"],
        kwargs={},
        cron="* * * * *",
    )
    fire_wrapper("settings", "default", "p")
    call_command("absurd_worker", queue="default", burst=True)
    assert Payload.objects.count() == 1


def test_missing_row_is_noop() -> None:
    fire_wrapper("settings", "default", "nope")  # no exception


def test_disabled_row_is_noop() -> None:
    ScheduledTask.objects.create(
        source="settings",
        name="off",
        alias="default",
        task="tests.tasks.create_payload",
        queue="default",
        args=["x"],
        kwargs={},
        cron="* * * * *",
        enabled=False,
    )
    fire_wrapper("settings", "default", "off")
    call_command("absurd_worker", queue="default", burst=True)
    assert Payload.objects.count() == 0


def test_disambiguation_by_alias() -> None:
    """Same name across two aliases fires only the targeted row."""
    call_command("absurd_sync_queues")
    ScheduledTask.objects.create(
        source="settings",
        name="n",
        alias="default",
        task="tests.tasks.create_payload",
        queue="default",
        args=["from-default"],
        kwargs={},
        cron="* * * * *",
    )
    ScheduledTask.objects.create(
        source="settings",
        name="n",
        alias="other",
        task="tests.tasks.create_payload",
        queue="default",
        args=["from-other"],
        kwargs={},
        cron="* * * * *",
    )
    fire_wrapper("settings", "default", "n")
    call_command("absurd_worker", queue="default", burst=True)
    payloads = list(Payload.objects.values_list("data", flat=True))
    assert payloads == ["from-default"]


def test_wrapper_reassembles_options_from_columns() -> None:
    """Wrapper fn builds params/options jsonb from named columns server-side.

    max_attempts isn't observable via worker side effects so we inspect the
    spawned task row directly via the absurd.tasks_view (params is a jsonb blob).
    """
    call_command("absurd_sync_queues")
    ScheduledTask.objects.create(
        source="settings",
        name="opts",
        alias="default",
        task="tests.tasks.capped",
        queue="default",
        args=[1, 2],
        kwargs={},
        max_attempts=5,
        cron="* * * * *",
    )
    fire_wrapper("settings", "default", "opts")
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
