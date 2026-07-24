"""Shared helpers for the pg_cron test suite (plain functions — fixtures live in
conftest.py). ``cron.job`` is read through the CENTRAL connection here (the catalog
seam ships no read verbs)."""

import typing as t

from django.db import connections

from django_absurd.connection import open_central_connection
from tests.utils import make_tasks_settings


def fetch_live_database() -> str:
    """The LIVE default-connection database name (a test run's mirrored name) — the
    ``<db>`` segment catalog jobnames are namespaced by."""
    return str(connections["default"].settings_dict["NAME"])


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


def schedule_control_job_in_other_database(database: str) -> None:
    """Register a ``_dj:``-namespaced control job bound to ANOTHER database in the
    shared central catalog, so a scoped flush of the live database can be shown never to
    reach it (the flush scopes by ``database = <live>``, not just the ``_dj:`` prefix).
    A raw ``cron.job`` INSERT — ``cron.schedule_in_database`` rejects a non-existent
    target — and ``active = false`` so the launcher never tries to connect to it."""
    with open_central_connection("default") as cur:
        cur.execute(
            "insert into cron.job (schedule, command, database, jobname, active) "
            "values (%s, %s, %s, %s, false)",
            ["5 seconds", "select 1", database, build_control_jobname(database)],
        )


def control_job_still_present(database: str) -> bool:
    """Whether the control job bound to ``database`` is still in the central catalog."""
    with open_central_connection("default") as cur:
        cur.execute(
            "select 1 from cron.job where jobname = %s and database = %s",
            [build_control_jobname(database), database],
        )
        return cur.fetchone() is not None


def remove_control_job(database: str) -> None:
    """Unschedule the control job bound to ``database`` (test hygiene: it targets a DB
    outside any per-database flush, so no auto-cleanup reaches it)."""
    with open_central_connection("default") as cur:
        cur.execute(
            "select jobid from cron.job where jobname = %s",
            [build_control_jobname(database)],
        )
        for (jobid,) in cur.fetchall():
            cur.execute("select cron.unschedule(%s)", [jobid])


def build_control_jobname(database: str) -> str:
    return f"_dj:{database}:s:control"
