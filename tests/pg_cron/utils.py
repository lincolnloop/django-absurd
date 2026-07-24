"""Shared helpers for the pg_cron test suite (plain functions — fixtures live in
conftest.py). ``cron.job`` is read through the CENTRAL connection here (the catalog
seam ships no read verbs)."""

import typing as t

from django_absurd.connection import open_central_connection
from tests.utils import make_tasks_settings


def build_pg_cron_tasks(
    schedule: dict[str, dict[str, object]],
    pg_cron_on_test_db: bool = True,
) -> dict[str, dict[str, t.Any]]:
    tasks = make_tasks_settings(schedule=schedule)
    tasks["default"]["OPTIONS"]["SYNC_SCHEDULES_ON_TEST_DB"] = True
    tasks["default"]["OPTIONS"]["PG_CRON_ON_TEST_DB"] = pg_cron_on_test_db
    return tasks


def fetch_cron_job(jobname: str) -> tuple[str, bool] | None:
    """The ``(database, active)`` for one pg_cron job read from the CENTRAL connection,
    or None if it isn't scheduled. The single-job test-side ``cron.job`` reader."""
    with open_central_connection("default") as cur:
        cur.execute(
            "select database, active from cron.job where jobname = %s", [jobname]
        )
        return t.cast("tuple[str, bool] | None", cur.fetchone())


def fetch_managed_jobs(
    database: str, source: str | None = None
) -> list[tuple[str, str, str, bool]]:
    """The ``(jobname, schedule, command, active)`` rows for every django-absurd job
    bound to ``database`` (db-namespaced ``_dj:<database>:`` prefix), read from the
    CENTRAL connection. Pass source to narrow to one lane."""
    prefix = f"_dj:{database}:{source}:" if source is not None else f"_dj:{database}:"
    with open_central_connection("default") as cur:
        cur.execute(
            "select jobname, schedule, command, active from cron.job "
            "where database = %s and starts_with(jobname, %s) order by jobname",
            [database, prefix],
        )
        return t.cast("list[tuple[str, str, str, bool]]", cur.fetchall())
