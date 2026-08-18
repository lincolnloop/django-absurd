import pytest
from django.core.management import call_command
from django.db import connections
from django.tasks import task
from pytest_django import Settings

from django_absurd.models import Queue
from django_absurd.routers import AbsurdRouter
from django_absurd.test import AbsurdTestRuntime

pytestmark = [
    pytest.mark.django_db(databases=["default", "absurd"]),
    pytest.mark.usefixtures("_isolate_queues"),
]

ABSURD = "django_absurd.backends.AbsurdBackend"


@task
def sum_numbers(a: int, b: int) -> int:
    return a + b


def absurd_schema_present(alias: str) -> bool:
    with connections[alias].cursor() as cur:
        cur.execute("SELECT to_regnamespace('absurd') IS NOT NULL")
        row = cur.fetchone()
        return bool(row[0]) if row else False


def test_orm_routes_to_alias() -> None:
    assert Queue.objects.db == "absurd"
    assert list(Queue.objects.all()) == []


def test_schema_provisioned_on_alias_not_default() -> None:
    assert absurd_schema_present("absurd") is True
    assert absurd_schema_present("default") is False


def test_allow_migrate_contract() -> None:
    router = AbsurdRouter()
    assert router.allow_migrate("absurd", "django_absurd") is True
    assert router.allow_migrate("default", "django_absurd") is False
    assert router.allow_migrate("absurd", "django_absurd_pg_cron") is True
    assert router.allow_migrate("default", "django_absurd_pg_cron") is False
    assert router.allow_migrate("absurd", "auth") is None


def test_db_for_read_write_route_django_absurd() -> None:
    router = AbsurdRouter()
    assert router.db_for_read(Queue) == "absurd"
    assert router.db_for_write(Queue) == "absurd"


def test_sync_command_honors_alias(
    settings: Settings,
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {"DATABASE": "absurd", "QUEUES": {"routed": {}}},
        }
    }
    call_command("absurd_sync_queues")
    assert Queue.objects.get(queue_name="routed").queue_name == "routed"


@pytest.mark.django_db(databases=["absurd", "default"], transaction=True)
def test_roundtrip_drains_on_the_non_default_alias(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    # the fixture resolves the Absurd alias itself (resolve_absurd_database), so a
    # drain/get_result here must land on "absurd", never the router's "default"
    assert dj_absurd.alias == "absurd"
    dj_absurd.sync_queues()  # _isolate_queues dropped the catalog on the way in
    result = sum_numbers.enqueue(1, 2)
    assert [run.result for run in dj_absurd.drain()] == [3]
    snapshot = dj_absurd.get_result(result.id)
    assert snapshot.result == 3
