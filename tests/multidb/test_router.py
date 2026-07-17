import typing as t

import pytest
from django.core.management import call_command
from django.db import connections

if t.TYPE_CHECKING:
    import pytest_django.fixtures

from django_absurd.models import Queue
from django_absurd.routers import AbsurdRouter

pytestmark = pytest.mark.django_db(databases=["default", "absurd"])

ABSURD = "django_absurd.backends.AbsurdBackend"


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
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {"DATABASE": "absurd", "QUEUES": {"routed": {}}},
        }
    }
    call_command("absurd_sync_queues")
    assert Queue.objects.get(queue_name="routed").queue_name == "routed"
