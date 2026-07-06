import typing as t

import pytest
from django.db import connection

from tests.fixtures import (  # noqa: F401
    _enable_db,
    _reset_absurd_queues,
    admin_user,
    staff_user,
)


@pytest.fixture(scope="session")
def ensure_pg_cron(django_db_setup, django_db_blocker):
    """Enable ``pg_cron`` on the test DB for the pg_cron suite.

    Non-autouse; opt-in via ``pytest.mark.usefixtures("ensure_pg_cron")``.
    The pg_cron suite runs on the pg_cron server (``db_pg_cron``), test DB
    ``absurd_test_pg_cron`` — which equals ``cron.database_name`` so ``CREATE
    EXTENSION pg_cron`` is permitted here. A non-pg_cron Postgres hard-errors;
    there is no graceful skip.
    """
    with django_db_blocker.unblock(), connection.cursor() as cur:
        cur.execute("create extension if not exists pg_cron")


@pytest.fixture
def _clear_owned_pg_cron_jobs():
    """Unschedule all ``absurd:%`` pg_cron jobs after the test.

    Opt-in via ``pytest.mark.usefixtures("_clear_owned_pg_cron_jobs")`` on each
    pg_cron test module (alongside ``ensure_pg_cron``). The broader ``absurd:%``
    pattern (not a per-alias prefix) catches all jobs created during a test,
    including those outside ``absurd:settings:<alias>:`` scope.
    """
    yield
    with connection.cursor() as cur:
        cur.execute("select jobid from cron.job where jobname like 'absurd:%'")
        for (jobid,) in cur.fetchall():
            cur.execute("select cron.unschedule(%s)", [jobid])


@pytest.fixture
def owned_cron_jobs() -> t.Callable[[str], list[str]]:
    """Return a callable ``owned_cron_jobs(alias="default") -> list[str]``.

    Queries ``cron.job`` for jobs owned by the given backend alias and returns
    their names sorted. Scoped to ``absurd:settings:<alias>:%``.
    """

    def _owned_cron_jobs(alias: str = "default") -> list[str]:
        with connection.cursor() as cur:
            cur.execute(
                "select jobname from cron.job where jobname like %s order by jobname",
                [f"absurd:settings:{alias}:%"],
            )
            return [row[0] for row in cur.fetchall()]

    return _owned_cron_jobs


@pytest.fixture
def cron_job_rows() -> t.Callable[[str], list[tuple]]:
    """Return a callable ``cron_job_rows(alias="default") -> list[tuple]``.

    Returns ``(jobname, schedule, command, active)`` tuples for jobs owned by
    the given backend alias, sorted by jobname.
    """

    def _cron_job_rows(alias: str = "default") -> list[tuple]:
        with connection.cursor() as cur:
            cur.execute(
                "select jobname, schedule, command, active from cron.job "
                "where jobname like %s order by jobname",
                [f"absurd:settings:{alias}:%"],
            )
            return cur.fetchall()

    return _cron_job_rows
