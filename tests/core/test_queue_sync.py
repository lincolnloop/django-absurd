import datetime as dt
import typing as t

import psycopg
import pytest
from absurd_sdk import CreateQueueOptions
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connection
from pytest_django.fixtures import SettingsWrapper

from django_absurd.models import Queue
from django_absurd.queues import get_absurd_client, resolve_absurd_database
from tests.utils import make_tasks_settings

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


def build_tasks_setting(
    queues: dict[str, CreateQueueOptions],
    database: str = "default",
) -> dict[str, dict[str, t.Any]]:
    return make_tasks_settings(queues=queues, database=database)


def table_exists(name: str) -> bool:
    with connection.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", [f"absurd.{name}"])
        row = cur.fetchone()
        return bool(row[0]) if row else False


def test_get_absurd_client_uses_psycopg3_connection() -> None:
    get_absurd_client()
    assert isinstance(connection.connection, psycopg.Connection)


@pytest.mark.django_db(databases=["default", "sqlite"], transaction=True)
def test_sync_command_screams_on_non_postgres_backend(
    settings: SettingsWrapper,
) -> None:
    settings.TASKS = build_tasks_setting({"x": {}}, database="sqlite")
    with pytest.raises(ImproperlyConfigured):
        call_command("absurd_sync_queues")


@pytest.mark.django_db(databases=["default", "sqlite"], transaction=True)
def test_migrate_screams_on_non_postgres_backend(
    settings: SettingsWrapper,
) -> None:
    settings.TASKS = build_tasks_setting({}, database="sqlite")
    with pytest.raises(ImproperlyConfigured):
        call_command("migrate", "django_absurd", database="sqlite", verbosity=0)


def test_migrate_provisions_declared_queue(settings: SettingsWrapper) -> None:
    # post_migrate runs sync_queues, so `migrate` creates the declared queues
    settings.TASKS = build_tasks_setting({"alpha": {}})
    call_command("migrate", "django_absurd", verbosity=0)
    assert Queue.objects.filter(queue_name="alpha").exists()


def test_sync_creates_with_options_and_model_maps(settings: SettingsWrapper) -> None:
    settings.TASKS = build_tasks_setting(
        {"x": {"storage_mode": "partitioned", "cleanup_ttl": "90 days"}}
    )
    call_command("absurd_sync_queues")
    q = Queue.objects.get(queue_name="x")
    assert q.storage_mode == "partitioned"
    assert q.cleanup_ttl == dt.timedelta(days=90)
    assert table_exists("t_x")


def test_list_shorthand(settings: SettingsWrapper) -> None:
    settings.TASKS = {"default": {"BACKEND": ABSURD, "QUEUES": ["alpha"]}}
    call_command("absurd_sync_queues")
    assert Queue.objects.filter(queue_name="alpha").exists()


def test_sync_reconciles_changed_option_idempotent(settings: SettingsWrapper) -> None:
    settings.TASKS = build_tasks_setting({"q": {"cleanup_limit": 100}})
    call_command("absurd_sync_queues")
    settings.TASKS = build_tasks_setting({"q": {"cleanup_limit": 250}})
    call_command("absurd_sync_queues")
    assert Queue.objects.get(queue_name="q").cleanup_limit == 250
    call_command("absurd_sync_queues")
    assert Queue.objects.get(queue_name="q").cleanup_limit == 250


def test_non_destructive(settings: SettingsWrapper) -> None:
    settings.TASKS = build_tasks_setting({"keep": {}})
    call_command("absurd_sync_queues")
    settings.TASKS = build_tasks_setting({})
    call_command("absurd_sync_queues")
    assert Queue.objects.filter(queue_name="keep").exists()


def test_sync_reports_no_queues_when_all_in_sync(
    settings: SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.TASKS = build_tasks_setting({"q": {}})
    call_command("absurd_sync_queues")  # creates q
    capsys.readouterr()
    call_command("absurd_sync_queues")  # q exists, no drift -> empty result
    assert "No queues to sync." in capsys.readouterr().out


def test_get_absurd_database_resolves_from_backend(settings: SettingsWrapper) -> None:
    settings.TASKS = build_tasks_setting({}, database="default")
    assert resolve_absurd_database() == "default"
    settings.TASKS = build_tasks_setting({}, database="absurd")
    assert resolve_absurd_database() == "absurd"


def test_sync_command_takes_no_database_flag(settings: SettingsWrapper) -> None:
    settings.TASKS = build_tasks_setting({})
    with pytest.raises(TypeError):
        call_command("absurd_sync_queues", database="sqlite")


def test_sync_command_reports_nothing_when_no_absurd_backend(
    settings: SettingsWrapper,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.TASKS = {
        "default": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}
    }
    call_command("absurd_sync_queues")
    assert "No Absurd task backends configured." in capsys.readouterr().out
