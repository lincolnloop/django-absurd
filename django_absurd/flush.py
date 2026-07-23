"""Shared flush logic for tearing down Absurd state between tests.

Backs both the automatic test cleanup (``django_absurd.test.install_absurd_cleanup``,
which wraps ``TransactionTestCase._post_teardown``) and the ``absurd_flush`` management
command — a plain, always-Django-dependent module.
"""

import psycopg.sql
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.db.utils import OperationalError, ProgrammingError

from django_absurd.backends import PG_CRON_APP_NAME
from django_absurd.queues import get_absurd_client, resolve_absurd_database


def flush_absurd_state(*, drop_schema: bool = False) -> None:
    """Reset Absurd state: drop or truncate every queue's tables, then (if
    ``django_absurd.pg_cron`` is installed) clear its scheduled-task state.

    ``drop_schema=True`` drops each queue's schema (catalog row + tables) and
    blanket-clears pg_cron (``cron.job`` + ``cron.job_run_details`` + the
    ``ScheduledTask`` table); ``drop_schema=False`` (the default) truncates queue
    tables' rows only and scopes the pg_cron clear to django-absurd's own jobs via
    ``teardown_crons``, never touching ``cron.job_run_details``. Both steps are
    independently no-ops on an unmigrated/absent schema.
    """
    clear_queues(drop_schema=drop_schema)

    if apps.is_installed(PG_CRON_APP_NAME):
        try:
            if drop_schema:
                drop_pg_cron_state()
            else:
                teardown_owned_pg_cron_jobs()
        except (OperationalError, ProgrammingError, ImproperlyConfigured):
            pass  # pg_cron schema not present (unmigrated / schema-absent)


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
    # Blanket clear — "the test DB is ours": unschedule every cron.job, unlike
    # drop_schema=False's scoped teardown_crons(include_admin=True).
    database = resolve_absurd_database()
    with connections[database].cursor() as cur:
        cur.execute("select cron.unschedule(jobid) from cron.job")
        cur.execute(
            psycopg.sql.SQL("TRUNCATE {table} CASCADE").format(
                table=psycopg.sql.Identifier("django_absurd_scheduledtask")
            )
        )
        cur.execute("TRUNCATE cron.job_run_details")
