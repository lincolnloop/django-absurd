import pytest
from django.core.management import call_command
from django.db import connection

from django_absurd.models import ScheduledJob
from tests.models import Payload

pytestmark = pytest.mark.django_db(transaction=True)


def _run(name: str) -> None:
    with connection.cursor() as cur:
        cur.execute("select public.django_absurd_run_scheduled(%s)", [name])


def test_fires_task_from_row() -> None:
    call_command("absurd_sync_queues")
    ScheduledJob.objects.create(
        name="p",
        alias="default",
        task="tests.tasks.create_payload",
        queue="default",
        params={"args": ["tick"], "kwargs": {}},
        options={},
        cron="* * * * *",
    )
    _run("p")
    call_command("absurd_worker", queue="default", burst=True)
    assert Payload.objects.count() == 1


def test_missing_row_is_noop() -> None:
    _run("nope")  # no exception


def test_disabled_row_is_noop() -> None:
    ScheduledJob.objects.create(
        name="off",
        alias="default",
        task="tests.tasks.create_payload",
        queue="default",
        params={"args": ["x"], "kwargs": {}},
        options={},
        cron="* * * * *",
        enabled=False,
    )
    _run("off")
    call_command("absurd_worker", queue="default", burst=True)
    assert Payload.objects.count() == 0
