"""Shared flush logic for tearing down Absurd state between tests.

Backs both the automatic test cleanup (``django_absurd.test.install_absurd_cleanup``,
which wraps ``TransactionTestCase._post_teardown``) and the ``absurd_flush`` management
command — a plain, always-Django-dependent module.

Both in-function imports below reach ``django_absurd.pg_cron``, the OPTIONAL app
gated by ``apps.is_installed``; ``pg_cron.reconcile`` also imports models, which
would make this module settings-dependent — and ``django_absurd.test`` imports it
at module level during pytest bootstrap (see that module's import-safety note).
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
from django_absurd.exceptions import SchemaNotInstalledError
from django_absurd.queues import (
    QUEUE_TABLE_PREFIXES,
    get_absurd_client,
    list_provisioned_queues,
    resolve_absurd_database,
)


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
    """Clear ``alias``'s database-level ``absurd.fake_now`` — the one implementation
    of that statement, shared by the fixture's clock release, the post-test flush,
    and the session-start sweep.

    The GUC outlives the process that set it and every NEW session inherits it.
    Targeted ``RESET`` of the one parameter, never ``RESET ALL`` (would clobber
    unrelated database-level settings); ``ALTER DATABASE`` rejects bind parameters,
    hence the composed identifier, read at runtime for xdist's per-worker databases.
    Dedicated autocommit connection, mirroring the freeze that set it: on Django's
    connection inside a test transaction the ``ALTER`` rolls back with the test,
    stranding exactly the GUC this clears.
    """
    statement = psycopg.sql.SQL("alter database {name} reset absurd.fake_now").format(
        name=psycopg.sql.Identifier(connections[alias].settings_dict["NAME"])
    )
    params: dict[str, t.Any] = connections[alias].get_connection_params()
    params.pop("cursor_factory", None)
    # ``context`` kept (unlike ``open_test_connection``): this connection is
    # write-only, so its relabeling timestamptz loader can never fire.
    with connections[alias].wrap_database_errors:
        conn = psycopg.connect(**params, autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(statement)
        finally:
            conn.close()


def clear_queues(*, drop_schema: bool) -> None:
    """Drop (``drop_schema=True``) or truncate (``drop_schema=False``) every queue's
    tables. Queue-only — never touches pg_cron. No-op on an unreachable database, an
    unmigrated/absent schema, or a partial one — a queue whose catalog row survives
    but one of its own tables does not.
    """
    try:
        names = list_provisioned_queues()
        client = get_absurd_client()
        for name in names:
            if drop_schema:
                client.drop_queue(name)
            else:
                truncate_queue_tables(name)
    except (
        OperationalError,
        ProgrammingError,
        ImproperlyConfigured,
        SchemaNotInstalledError,
    ):
        return  # absurd schema not present (unmigrated / schema-absent)


def teardown_owned_pg_cron_jobs() -> int:
    """Unschedule every pg_cron job django-absurd owns and delete every schedule row,
    admin-authored included. Returns the number of rows deleted.

    Shared by the post-test flush and ``absurd_flush``.
    """
    # The existing, already-tested teardown_crons(include_admin=True), never a
    # hand-rolled parallel implementation.
    from django_absurd.pg_cron.reconcile import teardown_crons  # noqa: PLC0415

    return teardown_crons(include_admin=True)


def truncate_queue_tables(queue: str) -> None:
    tables = [
        psycopg.sql.Identifier("absurd", f"{prefix}_{queue}")
        for prefix in QUEUE_TABLE_PREFIXES
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
