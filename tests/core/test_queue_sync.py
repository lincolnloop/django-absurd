import datetime as dt
import logging
import typing as t

import psycopg
import pytest
from absurd_sdk import CreateQueueOptions
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.utils import ProgrammingError
from pytest_django.fixtures import Settings

from django_absurd.models import Queue
from django_absurd.queues import get_absurd_client, resolve_absurd_database
from tests import utils

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


def build_tasks_setting(
    queues: dict[str, CreateQueueOptions],
    database: str = "default",
) -> dict[str, dict[str, t.Any]]:
    return utils.make_tasks_settings(queues=queues, database=database)


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
    settings: Settings,
) -> None:
    settings.TASKS = build_tasks_setting({"x": {}}, database="sqlite")
    with pytest.raises(CommandError) as excinfo:
        call_command("absurd_sync_queues")
    assert str(excinfo.value) == (
        "django-absurd requires the psycopg (v3) PostgreSQL backend. "
        "See https://www.psycopg.org/psycopg3/docs/"
    )


@pytest.mark.django_db(databases=["default", "sqlite"], transaction=True)
def test_migrate_screams_on_non_postgres_backend(
    settings: Settings,
) -> None:
    settings.TASKS = build_tasks_setting({}, database="sqlite")
    with pytest.raises(ImproperlyConfigured):
        call_command("migrate", "django_absurd", database="sqlite", verbosity=0)


def test_migrate_provisions_declared_queue(settings: Settings) -> None:
    # post_migrate runs sync_queues, so `migrate` creates the declared queues
    settings.TASKS = build_tasks_setting({"alpha": {}})
    call_command("migrate", "django_absurd", verbosity=0)
    assert Queue.objects.filter(queue_name="alpha").exists()


def test_reconcile_does_not_relabel_an_unrelated_missing_column(
    settings: Settings,
) -> None:
    """A ``ProgrammingError`` from ``reconcile_queue``'s own catalog query that
    isn't the schema-absent shape (``InvalidSchemaName``/``UndefinedTable``)
    surfaces as itself. Relabeling it "run migrate" would send the reader to the
    wrong door.

    Driven the way it could happen in production: an operator alters the catalog
    table's own column out from under ``reconcile_queue``, e.g. mid-migration — a
    case ``reconcile_queue`` never classifies as schema-absent, since the
    exception is a plain ``UndefinedColumn``, not
    ``InvalidSchemaName``/``UndefinedTable``.
    """
    settings.TASKS = build_tasks_setting({"probe": {}})
    call_command("absurd_sync_queues")
    with connection.cursor() as cur:
        cur.execute(
            "alter table absurd.queues rename column queue_name to queue_name_probe"
        )
    try:
        with pytest.raises(ProgrammingError) as excinfo:
            call_command("absurd_sync_queues")
        cause = excinfo.value.__cause__
        assert isinstance(cause, psycopg.errors.UndefinedColumn)
    finally:
        with connection.cursor() as cur:
            cur.execute(
                "alter table absurd.queues rename column queue_name_probe to queue_name"
            )


def test_migrate_tolerates_an_absent_schema(settings: Settings) -> None:
    # The migration is already recorded as applied, so `migrate` replays no DDL
    # but still fires `post_migrate` — which must swallow SchemaNotInstalledError
    # from provisioning rather than blowing up `migrate` itself.
    settings.TASKS = build_tasks_setting({"alpha": {}})
    with utils.hide_absurd_schema():
        call_command("migrate", "django_absurd", verbosity=0)


def test_sync_creates_with_options_and_model_maps(settings: Settings) -> None:
    settings.TASKS = build_tasks_setting(
        {"x": {"storage_mode": "partitioned", "cleanup_ttl": "90 days"}}
    )
    call_command("absurd_sync_queues")
    q = Queue.objects.get(queue_name="x")
    assert q.storage_mode == "partitioned"
    assert q.cleanup_ttl == dt.timedelta(days=90)
    assert table_exists("t_x")


def test_list_shorthand(settings: Settings) -> None:
    settings.TASKS = {"default": {"BACKEND": ABSURD, "QUEUES": ["alpha"]}}
    call_command("absurd_sync_queues")
    assert Queue.objects.filter(queue_name="alpha").exists()


def test_sync_reconciles_changed_option_idempotent(settings: Settings) -> None:
    settings.TASKS = build_tasks_setting({"q": {"cleanup_limit": 100}})
    call_command("absurd_sync_queues")
    settings.TASKS = build_tasks_setting({"q": {"cleanup_limit": 250}})
    call_command("absurd_sync_queues")
    assert Queue.objects.get(queue_name="q").cleanup_limit == 250
    call_command("absurd_sync_queues")
    assert Queue.objects.get(queue_name="q").cleanup_limit == 250


def test_non_destructive(settings: Settings) -> None:
    settings.TASKS = build_tasks_setting({"keep": {}})
    call_command("absurd_sync_queues")
    settings.TASKS = build_tasks_setting({})
    call_command("absurd_sync_queues")
    assert Queue.objects.filter(queue_name="keep").exists()


@pytest.mark.usefixtures("_isolate_queues")
def test_sync_reports_no_queues_when_all_in_sync(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    # A name unused elsewhere in this file: the log assertion below needs this
    # call to genuinely create the queue, regardless of what earlier tests in
    # this file already declared and left behind on the shared reused database.
    settings.TASKS = build_tasks_setting({"freshsync": {}})
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        call_command("absurd_sync_queues")  # creates freshsync
    records = [r for r in caplog.records if r.name == "django_absurd.queues"]
    assert len(records) == 1
    assert (
        records[0].getMessage() == "queues provisioned: created=freshsync reconciled="
    )
    capsys.readouterr()
    call_command("absurd_sync_queues")  # freshsync exists, no drift -> empty result
    assert capsys.readouterr().out == "🗃️ No queues to sync.\n"


def test_sync_prefixes_the_storage_mode_warning(
    capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    settings.TASKS = build_tasks_setting({"driftglyph": {}})
    call_command("absurd_sync_queues")  # create 'driftglyph' unpartitioned
    settings.TASKS = build_tasks_setting(
        {"driftglyph": {"storage_mode": "partitioned"}}
    )
    capsys.readouterr()
    call_command("absurd_sync_queues")
    cap = capsys.readouterr()
    assert cap.out == "🗃️ No queues to sync.\n"
    assert cap.err == (
        "🗃️ Queue 'driftglyph': storage_mode cannot be changed "
        "(existing: 'unpartitioned', declared: 'partitioned'); skipping.\n"
    )


def test_get_absurd_database_resolves_from_backend(settings: Settings) -> None:
    settings.TASKS = build_tasks_setting({}, database="default")
    assert resolve_absurd_database() == "default"
    settings.TASKS = build_tasks_setting({}, database="absurd")
    assert resolve_absurd_database() == "absurd"


def test_sync_command_takes_no_database_flag(settings: Settings) -> None:
    settings.TASKS = build_tasks_setting({})
    with pytest.raises(TypeError):
        call_command("absurd_sync_queues", database="sqlite")


def test_sync_command_reports_nothing_when_no_absurd_backend(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    settings.TASKS = {
        "default": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}
    }
    call_command("absurd_sync_queues")
    assert "No Absurd task backends configured." in capsys.readouterr().out
