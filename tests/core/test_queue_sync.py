import datetime as dt
import io
import logging
import threading
import typing as t
from concurrent import futures

import psycopg
import pytest
from absurd_sdk import CreateQueueOptions
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections
from django.db.utils import OperationalError, ProgrammingError
from pytest_django import Settings

from django_absurd.models import Queue
from django_absurd.queues import (
    PROVISION_LOCK_KEY,
    get_absurd_client,
    resolve_absurd_database,
)
from django_absurd.test import AbsurdTestRuntime
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


def list_absent_queue_tables(queue_name: str) -> list[str]:
    # Spelled out rather than imported from django_absurd.queues: a repair that puts
    # back only the table the prefix tuple happens to start with has to fail here.
    return [
        name
        for name in (
            f"t_{queue_name}",
            f"r_{queue_name}",
            f"c_{queue_name}",
            f"e_{queue_name}",
            f"w_{queue_name}",
        )
        if not table_exists(name)
    ]


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
    # from provisioning rather than blowing up `migrate` itself. This is the
    # adoption path: `migrate --fake django_absurd` against a schema installed out
    # of band, and the only failure `migrate` forgives.
    settings.TASKS = build_tasks_setting({"alpha": {}})
    with utils.hide_absurd_schema():
        call_command("migrate", "django_absurd", verbosity=0)


def test_sync_check_fails_when_it_cannot_look(settings: Settings) -> None:
    # --check must never answer "in sync" when it could not read the catalog at all.
    settings.TASKS = build_tasks_setting({"alpha": {}})
    with utils.hide_absurd_schema(), pytest.raises(CommandError) as excinfo:
        call_command("absurd_sync_queues", "--check")
    assert str(excinfo.value) == (
        "Absurd schema is not installed. Run: manage.py migrate"
    )


def test_migrate_fails_when_provisioning_errors(settings: Settings) -> None:
    # Anything other than an absent schema means this deploy provisioned nothing and
    # nothing at runtime will fix it, so `migrate` must not report success. Driven the
    # way it could happen for real: the catalog's own column altered out from under
    # the receiver, which classifies as a plain UndefinedColumn rather than as
    # schema-absent.
    settings.TASKS = build_tasks_setting({"alpha": {}})
    call_command("absurd_sync_queues")
    with connection.cursor() as cur:
        cur.execute(
            "alter table absurd.queues rename column queue_name to queue_name_gone"
        )
    try:
        with pytest.raises(ProgrammingError) as excinfo:
            call_command("migrate", "django_absurd", verbosity=0)
        assert isinstance(excinfo.value.__cause__, psycopg.errors.UndefinedColumn)
    finally:
        with connection.cursor() as cur:
            cur.execute(
                "alter table absurd.queues rename column queue_name_gone to queue_name"
            )


def test_migrate_says_so_when_it_cannot_provision(settings: Settings) -> None:
    # The receiver swallows so a faked or adopted migration still completes, but a
    # silent swallow leaves nothing provisioned and nothing said — and no runtime
    # path repairs that any more.
    settings.TASKS = build_tasks_setting({"alpha": {}})
    buf = io.StringIO()
    with utils.hide_absurd_schema():
        call_command("migrate", "django_absurd", verbosity=1, stdout=buf)
    assert buf.getvalue().endswith(
        "Provisioning Absurd queues (default):\n"
        "  Not provisioned: Absurd schema is not installed. "
        "Run: manage.py migrate\n"
    )


@pytest.mark.usefixtures("_isolate_queues")
def test_migrate_reports_a_queue_it_repaired(settings: Settings) -> None:
    settings.TASKS = build_tasks_setting({"mended": {}})
    call_command("absurd_sync_queues")
    with connection.cursor() as cur:
        cur.execute(
            "drop table absurd.t_mended, absurd.r_mended, absurd.c_mended, "
            "absurd.e_mended, absurd.w_mended cascade"
        )
    buf = io.StringIO()
    call_command("migrate", "django_absurd", verbosity=1, stdout=buf)
    assert "  Repaired 'mended'" in buf.getvalue()


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


def test_sync_reconciles_changed_option_idempotent(
    capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    # Two mutable opts, so the drift scan is exercised both ways: cleanup_limit
    # unchanged (loop continues) and cleanup_ttl changed via parse_interval.
    settings.TASKS = build_tasks_setting(
        {"q": {"cleanup_limit": 100, "cleanup_ttl": "30 days"}}
    )
    call_command("absurd_sync_queues")
    settings.TASKS = build_tasks_setting(
        {"q": {"cleanup_limit": 100, "cleanup_ttl": "60 days"}}
    )
    capsys.readouterr()
    call_command("absurd_sync_queues")
    assert capsys.readouterr().out == "🗃️ Reconciled: q\n"
    assert Queue.objects.get(queue_name="q").cleanup_ttl == dt.timedelta(days=60)
    settings.TASKS = build_tasks_setting(
        {"q": {"cleanup_limit": 250, "cleanup_ttl": "60 days"}}
    )
    capsys.readouterr()
    call_command("absurd_sync_queues")
    assert capsys.readouterr().out == "🗃️ Reconciled: q\n"
    assert Queue.objects.get(queue_name="q").cleanup_limit == 250
    capsys.readouterr()
    call_command("absurd_sync_queues")
    # The report is the only witness of the no-drift direction: set_queue_policy run
    # unconditionally would leave every column below byte-identical anyway.
    assert capsys.readouterr().out == "🗃️ No queues to sync.\n"
    assert Queue.objects.get(queue_name="q").cleanup_limit == 250
    assert Queue.objects.get(queue_name="q").cleanup_ttl == dt.timedelta(days=60)


@pytest.mark.usefixtures("_isolate_queues")
def test_sync_recreates_the_tables_of_a_surviving_catalog_row(
    capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    # The state QueueNotProvisionedError sends an operator here to fix: a manual drop
    # or a partial restore leaves the catalog row behind, so a row-gated reconcile
    # would report "no changes" and repair nothing.
    settings.TASKS = build_tasks_setting({"partial": {}})
    call_command("absurd_sync_queues")
    with connection.cursor() as cur:
        cur.execute(
            "drop table absurd.t_partial, absurd.r_partial, absurd.c_partial, "
            "absurd.e_partial, absurd.w_partial cascade"
        )
    assert list_absent_queue_tables("partial") == [
        "t_partial",
        "r_partial",
        "c_partial",
        "e_partial",
        "w_partial",
    ]
    capsys.readouterr()
    call_command("absurd_sync_queues")
    assert capsys.readouterr().out == "🗃️ Repaired: partial\n"
    assert list_absent_queue_tables("partial") == []


@pytest.mark.usefixtures("_isolate_queues")
def test_sync_recreates_the_tables_of_a_surviving_partitioned_catalog_row(
    capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    # Unpartitioned is the one mode where handing create_queue the existing
    # storage_mode decides nothing, so the repair is only really proven here: dropping
    # the mode would put t_ back as a plain table and the queue would silently stop
    # being partitioned.
    settings.TASKS = build_tasks_setting({"partsurv": {"storage_mode": "partitioned"}})
    call_command("absurd_sync_queues")
    with connection.cursor() as cur:
        cur.execute(
            "drop table absurd.t_partsurv, absurd.r_partsurv, absurd.c_partsurv, "
            "absurd.e_partsurv, absurd.w_partsurv cascade"
        )
    # A partitioned queue's sixth table, which find_missing_queue_tables deliberately
    # never probes; leaving it keeps the repair driven by the five that are probed.
    assert table_exists("i_partsurv")
    capsys.readouterr()
    call_command("absurd_sync_queues")
    assert capsys.readouterr().out == "🗃️ Repaired: partsurv\n"
    assert list_absent_queue_tables("partsurv") == []
    assert Queue.objects.get(queue_name="partsurv").storage_mode == "partitioned"
    with connection.cursor() as cur:
        cur.execute(
            "select relkind from pg_class where oid = 'absurd.t_partsurv'::regclass"
        )
        assert cur.fetchone() == ("p",)


@pytest.mark.usefixtures("_isolate_queues")
def test_sync_leaves_a_provisioned_partitioned_queue_alone(
    dj_absurd: AbsurdTestRuntime, settings: Settings
) -> None:
    # Once the clock passes the pre-created window, rows land in the default partition,
    # and ensure_partitions can no longer create the weeks they belong to — Postgres
    # refuses. Provisioning must not reach that DDL for a queue already provisioned.
    settings.TASKS = build_tasks_setting({"parts": {"storage_mode": "partitioned"}})
    call_command("absurd_sync_queues")
    with dj_absurd.freeze_time() as frozen_time:
        frozen_time.shift(dt.timedelta(days=60))
        with connection.cursor() as cur:
            # The partition key is a uuidv7 range over task_id, not enqueue_at.
            cur.execute(
                "insert into absurd.t_parts "
                "(task_id, task_name, params, state, enqueue_at) values "
                "(absurd.uuidv7_floor(now() + interval '60 days'), 'late', "
                "'{}'::jsonb, 'pending', now() + interval '60 days')"
            )
        call_command("absurd_sync_queues")
    assert Queue.objects.get(queue_name="parts").storage_mode == "partitioned"


@pytest.mark.usefixtures("_isolate_queues")
def test_sync_check_reports_pending_work_and_writes_nothing(
    capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    settings.TASKS = build_tasks_setting({"pending": {}})
    with pytest.raises(CommandError) as excinfo:
        call_command("absurd_sync_queues", "--check")
    assert str(excinfo.value) == (
        "Queues are not in sync. Run: manage.py absurd_sync_queues"
    )
    assert capsys.readouterr().out == "🗃️ Would create: pending\n"
    assert Queue.objects.filter(queue_name="pending").exists() is False


@pytest.mark.usefixtures("_isolate_queues")
def test_sync_check_is_quiet_once_everything_is_provisioned(
    capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    settings.TASKS = build_tasks_setting({"settled": {}})
    call_command("absurd_sync_queues")
    capsys.readouterr()
    call_command("absurd_sync_queues", "--check")
    assert capsys.readouterr().out == "🗃️ No queues to sync.\n"


@pytest.mark.usefixtures("_isolate_queues")
def test_sync_check_names_what_the_real_run_then_does(
    capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    # The dry run reads the same three predicates the write path does; this is what
    # keeps the two from drifting apart.
    settings.TASKS = build_tasks_setting({"agree": {"cleanup_limit": 100}})
    call_command("absurd_sync_queues")
    settings.TASKS = build_tasks_setting({"agree": {"cleanup_limit": 250}})
    capsys.readouterr()
    with pytest.raises(CommandError):
        call_command("absurd_sync_queues", "--check")
    planned = capsys.readouterr().out
    call_command("absurd_sync_queues")
    assert planned == capsys.readouterr().out.replace("Reconciled:", "Would reconcile:")


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
        records[0].getMessage()
        == 'queues provisioned: created="freshsync" reconciled="" repaired=""'
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


def test_sync_command_waits_for_a_concurrent_provisioner(settings: Settings) -> None:
    # The lock is taken before any provisioning work, not just around the view
    # rebuild: the command dies waiting for it with its queue still uncreated.
    settings.TASKS = build_tasks_setting({"locked": {}})
    holder = psycopg.connect(**utils.get_absurd_connection_params(), autocommit=True)
    try:
        holder.execute("SELECT pg_advisory_lock(%s)", [PROVISION_LOCK_KEY])
        with connection.cursor() as cur:
            cur.execute("SET lock_timeout = '250ms'")
        try:
            with pytest.raises(OperationalError) as excinfo:
                call_command("absurd_sync_queues", stdout=io.StringIO())
        finally:
            with connection.cursor() as cur:
                cur.execute("RESET lock_timeout")
    finally:
        holder.close()
    assert str(excinfo.value) == "canceling statement due to lock timeout"
    assert not Queue.objects.filter(queue_name="locked").exists()


def test_concurrent_sync_survives_the_admin_views_being_absent() -> None:
    # https://github.com/lincolnloop/django-absurd/issues/195 — DROP VIEW IF EXISTS
    # takes no lock on a view that isn't there, so unserialized provisioners reach
    # CREATE VIEW together and the losers collide on the catalog's unique index.
    provisioners = 4
    barrier = threading.Barrier(provisioners)

    def sync_queues_concurrently() -> None:
        try:
            barrier.wait()
            call_command("absurd_sync_queues", stdout=io.StringIO())
        finally:
            connections.close_all()  # this thread's own connection

    with connection.cursor() as cur:
        # Absurd's own install SQL creates no views, so django-absurd's five admin
        # views are the whole population of the schema — the count assertions fail
        # loudly if a rename ever makes this statement drop less than all of them.
        cur.execute(
            "DROP VIEW IF EXISTS absurd.tasks_view, absurd.runs_view, "
            "absurd.checkpoints_view, absurd.waits_view, absurd.events_view CASCADE"
        )
        cur.execute("SELECT count(*) FROM pg_views WHERE schemaname = 'absurd'")
        assert cur.fetchone() == (0,)

    with futures.ThreadPoolExecutor(provisioners) as pool:
        # .result() re-raises in this thread: an exception inside a bare Thread
        # target only prints, leaving the test green.
        for future in [
            pool.submit(sync_queues_concurrently) for _ in range(provisioners)
        ]:
            future.result()

    with connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_views WHERE schemaname = 'absurd'")
        assert cur.fetchone() == (5,)
