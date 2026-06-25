from datetime import timedelta

import psycopg
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connection

from django_absurd.models import Queue
from django_absurd.queues import get_absurd_client, resolve_absurd_database

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


def build_tasks_setting(queues, database="default"):
    return {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {"DATABASE": database, "QUEUES": queues},
        }
    }


def table_exists(name):
    with connection.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", [f"absurd.{name}"])
        return cur.fetchone()[0]


def test_get_absurd_client_uses_psycopg3_connection():
    get_absurd_client()
    assert isinstance(connection.connection, psycopg.Connection)


@pytest.mark.django_db(databases=["default", "sqlite"], transaction=True)
def test_sync_command_screams_on_non_postgres_backend(settings):
    settings.TASKS = build_tasks_setting({"x": {}}, database="sqlite")
    with pytest.raises(ImproperlyConfigured):
        call_command("absurd_sync_queues")


@pytest.mark.django_db(databases=["default", "sqlite"], transaction=True)
def test_migrate_screams_on_non_postgres_backend(settings):
    settings.TASKS = build_tasks_setting({}, database="sqlite")
    with pytest.raises(ImproperlyConfigured):
        call_command("migrate", "django_absurd", database="sqlite", verbosity=0)


def test_migrate_creates_no_queue(settings):
    settings.TASKS = build_tasks_setting({"alpha": {}})
    call_command("migrate", "django_absurd", verbosity=0)
    assert not Queue.objects.filter(queue_name="alpha").exists()


def test_sync_creates_with_options_and_model_maps(settings):
    settings.TASKS = build_tasks_setting(
        {"x": {"storage_mode": "partitioned", "cleanup_ttl": "90 days"}}
    )
    call_command("absurd_sync_queues")
    q = Queue.objects.get(queue_name="x")
    assert q.storage_mode == "partitioned"
    assert q.cleanup_ttl == timedelta(days=90)
    assert table_exists("t_x")


def test_list_shorthand(settings):
    settings.TASKS = {"default": {"BACKEND": ABSURD, "QUEUES": ["alpha"]}}
    call_command("absurd_sync_queues")
    assert Queue.objects.filter(queue_name="alpha").exists()


def test_sync_reconciles_changed_option_idempotent(settings):
    settings.TASKS = build_tasks_setting({"q": {"cleanup_limit": 100}})
    call_command("absurd_sync_queues")
    settings.TASKS = build_tasks_setting({"q": {"cleanup_limit": 250}})
    call_command("absurd_sync_queues")
    assert Queue.objects.get(queue_name="q").cleanup_limit == 250
    call_command("absurd_sync_queues")
    assert Queue.objects.get(queue_name="q").cleanup_limit == 250


def test_non_destructive(settings):
    settings.TASKS = build_tasks_setting({"keep": {}})
    call_command("absurd_sync_queues")
    settings.TASKS = build_tasks_setting({})
    call_command("absurd_sync_queues")
    assert Queue.objects.filter(queue_name="keep").exists()


def test_sync_reports_no_queues_when_all_in_sync(settings, capsys):
    settings.TASKS = build_tasks_setting({"q": {}})
    call_command("absurd_sync_queues")  # creates q
    capsys.readouterr()
    call_command("absurd_sync_queues")  # q exists, no drift -> empty result
    assert "No queues to sync." in capsys.readouterr().out


def test_get_absurd_database_resolves_from_backend(settings):
    settings.TASKS = build_tasks_setting({}, database="default")
    assert resolve_absurd_database() == "default"
    settings.TASKS = build_tasks_setting({}, database="absurd")
    assert resolve_absurd_database() == "absurd"


def test_sync_command_takes_no_database_flag(settings):
    settings.TASKS = build_tasks_setting({})
    with pytest.raises(TypeError):
        call_command("absurd_sync_queues", database="sqlite")


def test_sync_command_reports_nothing_when_no_absurd_backend(settings, capsys):
    settings.TASKS = {
        "default": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}
    }
    call_command("absurd_sync_queues")
    assert "No Absurd task backends configured." in capsys.readouterr().out
