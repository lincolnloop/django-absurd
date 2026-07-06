import pytest
from django.core.management import call_command
from django.db import connection

from django_absurd.pg_cron.models import ScheduledJob
from tests.models import Payload
from tests.tasks import capped, on_reports

pytestmark = pytest.mark.django_db(transaction=True)


def test_capped_task_returns_sum():
    assert capped.func(3, 4) == 7


def test_on_reports_task_returns_string():
    assert on_reports.func() == "on_reports"


def _run(source: str, alias: str, name: str) -> None:
    with connection.cursor() as cur:
        cur.execute(
            "select public.django_absurd_run_scheduled(%s, %s, %s)",
            [source, alias, name],
        )


def test_fires_task_from_row() -> None:
    call_command("absurd_sync_queues")
    ScheduledJob.objects.create(
        source="settings",
        name="p",
        alias="default",
        task="tests.tasks.create_payload",
        queue="default",
        params={"args": ["tick"], "kwargs": {}},
        options={},
        cron="* * * * *",
    )
    _run("settings", "default", "p")
    call_command("absurd_worker", queue="default", burst=True)
    assert Payload.objects.count() == 1


def test_missing_row_is_noop() -> None:
    _run("settings", "default", "nope")  # no exception


def test_disabled_row_is_noop() -> None:
    ScheduledJob.objects.create(
        source="settings",
        name="off",
        alias="default",
        task="tests.tasks.create_payload",
        queue="default",
        params={"args": ["x"], "kwargs": {}},
        options={},
        cron="* * * * *",
        enabled=False,
    )
    _run("settings", "default", "off")
    call_command("absurd_worker", queue="default", burst=True)
    assert Payload.objects.count() == 0


def test_disambiguation_by_alias() -> None:
    """Same name across two aliases fires only the targeted row."""
    call_command("absurd_sync_queues")
    ScheduledJob.objects.create(
        source="settings",
        name="n",
        alias="default",
        task="tests.tasks.create_payload",
        queue="default",
        params={"args": ["from-default"], "kwargs": {}},
        options={},
        cron="* * * * *",
    )
    ScheduledJob.objects.create(
        source="settings",
        name="n",
        alias="other",
        task="tests.tasks.create_payload",
        queue="default",
        params={"args": ["from-other"], "kwargs": {}},
        options={},
        cron="* * * * *",
    )
    _run("settings", "default", "n")
    call_command("absurd_worker", queue="default", burst=True)
    payloads = list(Payload.objects.values_list("data", flat=True))
    assert payloads == ["from-default"]
