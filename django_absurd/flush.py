"""Shared flush logic for tearing down Absurd state between tests.

Backs both the automatic test cleanup (``django_absurd.test.install_absurd_cleanup``,
which wraps ``TransactionTestCase._post_teardown``) and the ``absurd_flush`` management
command — a plain, always-Django-dependent module.

Both in-function imports below reach into ``django_absurd.pg_cron``, the OPTIONAL app,
and stay in-function on purpose: core must not import it at module level, since the
``apps.is_installed(PG_CRON_APP_NAME)`` guard is what decides whether it is in play at
all. ``pg_cron.reconcile`` additionally imports ``pg_cron.models``, so a module-level
import of it would make THIS module settings-dependent — and this module is imported at
module level by ``django_absurd.test``, which pytest's plugin bootstrap imports on every
run in every venv (see that module's import-safety note).
"""

import contextlib
import typing as t

import psycopg
import psycopg.sql
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.db.utils import OperationalError, ProgrammingError

from django_absurd.backends import PG_CRON_APP_NAME
from django_absurd.queues import get_absurd_client, resolve_absurd_database


def flush_absurd_state(*, drop_schema: bool = False) -> None:
    """Reset Absurd state: release a stranded durable clock, drop or truncate every
    queue's tables, then (if ``django_absurd.pg_cron`` is installed) clear its
    scheduled-task state.

    ``drop_schema=True`` drops each queue's schema (catalog row + tables) and clears
    this app database's pg_cron state (its ``cron.job`` jobs + its
    ``cron.job_run_details`` + the ``ScheduledTask`` table), scoped to the app database
    — never a blanket clear of the shared central catalog. ``drop_schema=False`` (the
    default) truncates queue tables' rows only and scopes the pg_cron clear to
    django-absurd's own jobs via ``teardown_crons``, never touching
    ``cron.job_run_details``. Both steps are independently no-ops on an unmigrated /
    absent schema.
    """
    # Tolerant like the steps below: an unreachable database or an unconfigured Absurd
    # backend means there is no clock to release.
    with contextlib.suppress(OperationalError, ProgrammingError, ImproperlyConfigured):
        reset_fake_now(resolve_absurd_database())

    clear_queues(drop_schema=drop_schema)

    if apps.is_installed(PG_CRON_APP_NAME):
        try:
            if drop_schema:
                drop_pg_cron_state()
            else:
                teardown_owned_pg_cron_jobs()
        except (OperationalError, ProgrammingError, ImproperlyConfigured):
            pass  # pg_cron schema not present (unmigrated / schema-absent)


def reset_fake_now(alias: str) -> None:
    """Clear ``alias``'s database-level ``absurd.fake_now`` — the one implementation of
    that statement, shared by the ``dj_absurd`` fixture's clock release, the post-test
    flush, and the session-start sweep.

    The GUC outlives the process that set it and every NEW session inherits it, so an
    unreleased one silently moves durable time for the whole reused test database.

    Targeted ``RESET`` of the one parameter, never ``RESET ALL`` — that would clobber
    unrelated database-level settings. ``ALTER DATABASE`` rejects bind parameters, hence
    the composed identifier, read at runtime so xdist's per-worker databases each get
    their own.

    On a DEDICATED autocommit connection, mirroring the freeze that set it
    (``django_absurd.test.AbsurdTestRuntime._write_fake_now``): a caller can be inside
    an open transaction — a test that froze the clock without
    ``django_db(transaction=True)`` — where an ``ALTER DATABASE`` on Django's own
    connection is rolled back with the test, stranding exactly the GUC this clears.
    """
    statement = psycopg.sql.SQL("alter database {name} reset absurd.fake_now").format(
        name=psycopg.sql.Identifier(connections[alias].settings_dict["NAME"])
    )
    params: dict[str, t.Any] = connections[alias].get_connection_params()
    params.pop("cursor_factory", None)
    # ``context`` (Django's adapters) is kept, unlike in
    # ``django_absurd.test.open_test_connection``, which drops it because its
    # timestamptz loader relabels rather than converts. This connection is write-only —
    # one ``ALTER DATABASE ... RESET`` — so no timestamptz is ever read back through it.
    with connections[alias].wrap_database_errors:
        conn = psycopg.connect(**params, autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(statement)
        finally:
            conn.close()


def clear_queues(*, drop_schema: bool) -> None:
    """Drop (``drop_schema=True``) or truncate (``drop_schema=False``) every queue's
    tables. Queue-only — never touches pg_cron. No-op on an unmigrated/absent schema.
    """
    try:
        client = get_absurd_client()
        for name in client.list_queues():
            if drop_schema:
                client.drop_queue(name)
            else:
                truncate_queue_tables(name)
    except (OperationalError, ProgrammingError, ImproperlyConfigured):
        pass  # absurd schema not present (unmigrated / schema-absent)


def teardown_owned_pg_cron_jobs() -> None:
    # Scoped clear (drop_schema=False) — the existing, already-tested
    # teardown_crons(include_admin=True), never a hand-rolled parallel implementation.
    # In-function: optional app, AND reconcile imports pg_cron.models (see docstring).
    from django_absurd.pg_cron.reconcile import teardown_crons  # noqa: PLC0415

    teardown_crons(include_admin=True)


def truncate_queue_tables(queue: str) -> None:
    tables = [
        psycopg.sql.Identifier("absurd", f"{prefix}_{queue}")
        for prefix in ("t", "r", "c", "e", "w")
    ]
    with connections[resolve_absurd_database()].cursor() as cur:
        # `i_<queue>` only exists for a `partitioned` queue (see
        # `absurd.create_queue`'s own conditional `create table ...
        # 'i_' || p_queue_name` branch) — a plain TRUNCATE has no IF EXISTS, so check
        # first, mirroring `drop_queue`'s own tolerance of a missing table.
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", [f"absurd.i_{queue}"])
        if cur.fetchone()[0]:
            tables.append(psycopg.sql.Identifier("absurd", f"i_{queue}"))
        cur.execute(
            psycopg.sql.SQL("TRUNCATE {tables} CASCADE").format(
                tables=psycopg.sql.SQL(", ").join(tables)
            )
        )


def drop_pg_cron_state() -> None:
    # In-function: optional app (see module docstring). catalog itself is settings-free.
    from django_absurd.pg_cron import catalog  # noqa: PLC0415

    # Scoped clear — this app database's own jobs + run history in the shared central
    # catalog (never a blanket TRUNCATE/unschedule) — plus a TRUNCATE of the app-DB
    # ScheduledTask row table.
    database = resolve_absurd_database()
    catalog.flush_database_jobs(database)
    with connections[database].cursor() as cur:
        cur.execute(
            psycopg.sql.SQL("TRUNCATE {table} CASCADE").format(
                table=psycopg.sql.Identifier("django_absurd_scheduledtask")
            )
        )
