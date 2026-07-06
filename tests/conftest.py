import typing as t

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.db.utils import OperationalError, ProgrammingError

from django_absurd.queues import get_absurd_client


@pytest.fixture(autouse=True)
def _enable_db(db):
    pass


@pytest.fixture(autouse=True)
def _reset_absurd_queues(_enable_db):
    """Drop all Absurd queues before each test.

    ``transaction=True`` tests create queues whose per-queue tables (DDL) and
    ``managed=False`` registry rows are not rolled back / flushed, so they leak
    across ``--reuse-db`` runs. Reset to zero queues so every test is hermetic.
    """
    try:
        client = get_absurd_client()
        for name in client.list_queues():
            client.drop_queue(name)
    except (OperationalError, ProgrammingError, ImproperlyConfigured):
        pass  # absurd schema not present (unmigrated / schema-absent test)


@pytest.fixture(scope="session")
def ensure_pg_cron(django_db_setup, django_db_blocker):
    """Enable ``pg_cron`` on the test DB for pg_cron-marked tests.

    Non-autouse and opt-in via ``pytest.mark.usefixtures("ensure_pg_cron")`` so
    the default ``uv run pytest`` (which deselects ``pg_cron``) never needs the
    extension. ``CREATE EXTENSION pg_cron`` is only permitted in the DB named by
    ``cron.database_name``; ``tests/settings.py`` pins the test DB to that name.
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


@pytest.fixture
def admin_user(_enable_db):
    return get_user_model().objects.create_superuser("admin", "a@x.com", "pw")


@pytest.fixture
def staff_user(_enable_db):
    return get_user_model().objects.create_user("staff", "s@x.com", "pw", is_staff=True)
