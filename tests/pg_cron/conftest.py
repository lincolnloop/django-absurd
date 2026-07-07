import typing as t

import pytest
from django.db import connection

from tests.fixtures import (  # noqa: F401
    _enable_db,
    _reset_absurd_queues,
    admin_user,
    staff_user,
)


@pytest.fixture(scope="session", autouse=True)
def ensure_pg_cron(django_db_setup, django_db_blocker):
    """Enable ``pg_cron`` on the test DB for the pg_cron suite."""
    with django_db_blocker.unblock(), connection.cursor() as cur:
        cur.execute("create extension if not exists pg_cron")


@pytest.fixture(autouse=True)
def _clear_owned_pg_cron_jobs(request):
    """Unschedule all ``absurd:%`` pg_cron jobs after the test.

    The broader ``absurd:%`` pattern (not a per-alias prefix) catches all jobs
    created during a test, including those outside ``absurd:settings:<alias>:`` scope.
    Skips cleanup for tests that never opened a DB connection.
    """
    yield
    if not request.node.get_closest_marker("django_db"):
        return
    with connection.cursor() as cur:
        cur.execute("select jobid from cron.job where jobname like 'absurd:%'")
        for (jobid,) in cur.fetchall():
            cur.execute("select cron.unschedule(%s)", [jobid])


@pytest.fixture
def get_managed_cron_jobs() -> t.Callable[[str], list[tuple]]:
    """Return a callable ``get_managed_cron_jobs(alias="default") -> list[tuple]``.

    Returns ``(jobname, schedule, command, active)`` tuples for jobs owned by
    the given backend alias, sorted by jobname. Scoped to
    ``absurd:settings:<alias>:%``.
    """

    def _get_managed_cron_jobs(alias: str = "default") -> list[tuple]:
        with connection.cursor() as cur:
            cur.execute(
                "select jobname, schedule, command, active from cron.job "
                "where jobname like %s order by jobname",
                [f"absurd:settings:{alias}:%"],
            )
            return cur.fetchall()

    return _get_managed_cron_jobs
