"""Shared helpers for the pg_cron test suite (plain functions — fixtures live in
conftest.py). ``cron.job`` is read through the CENTRAL connection here (the catalog
seam ships no read verbs)."""

import contextlib
import importlib.resources
import os
import time
import typing as t

import psycopg
import psycopg.sql
from django.db import connections

from django_absurd.connection import open_central_connection, resolve_cron_database
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


def is_control_job_present(database: str) -> bool:
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


# --- Runtime no-leak proof: a task-PRODUCING cron bound to a scratch (non-test) DB. ---

# The command the producer job runs INSIDE the scratch database each tick: spawn an
# Absurd task into the scratch DB's own `default` queue. It only succeeds where the
# Absurd schema + `default` queue exist (the provisioned scratch DB) — so a job that
# fired into the wrong database would fail, never reach status='succeeded', and
# wait_for_fire would time out loudly.
PRODUCER_COMMAND = (
    "select absurd.spawn_task('default', 'tests.tasks.produce', '{}'::jsonb)"
)


class Producer(t.NamedTuple):
    jobid: int
    target: str


def scratch_db_name() -> str:
    """A per-xdist-worker-unique NON-test database name for the producer to target.

    Suffixed with ``PYTEST_XDIST_WORKER`` (``main`` when not running under xdist) so
    parallel workers never share a scratch DB; distinct from the ``test_`` test-DB
    name so it is never mistaken for one."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    return f"absurd_scratch_{worker}"


def schedule_producer_cron(target: str) -> Producer:
    """Provision ``target`` (a fresh scratch DB with the Absurd schema + ``default``
    queue) and schedule a ``schedule_in_database`` job, bound to it, that enqueues an
    Absurd task every second. Returns the job's ``(jobid, target)`` handle."""
    provision_scratch_database(target)
    with open_central_connection("default") as cur:
        cur.execute(
            "select cron.schedule_in_database(%s, %s, %s, %s)",
            [build_producer_jobname(target), "1 seconds", PRODUCER_COMMAND, target],
        )
        (jobid,) = t.cast("tuple[int]", cur.fetchone())
    return Producer(jobid=jobid, target=target)


def wait_for_fire(producer: Producer, timeout: float) -> None:
    """Poll ``cron.job_run_details`` on the central connection until the producer job
    has a ``succeeded`` run, up to ``timeout`` seconds. Raise if it never fires — a
    broken launcher must fail loudly, not pass by not-having-waited-long-enough."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with open_central_connection("default") as cur:
            cur.execute(
                "select 1 from cron.job_run_details "
                "where jobid = %s and status = 'succeeded' limit 1",
                [producer.jobid],
            )
            if cur.fetchone() is not None:
                return
        time.sleep(0.5)
    msg = (
        f"producer cron job {producer.jobid} (target {producer.target!r}) never "
        f"succeeded within {timeout}s — the pg_cron launcher did not complete a "
        "cycle, so the no-leak assertion cannot be trusted."
    )
    raise AssertionError(msg)


def absurd_queue_depth(db: str) -> int:
    """The number of tasks in ``db``'s (this test DB's) ``default`` Absurd queue,
    read through the live default connection."""
    with connections["default"].cursor() as cur:
        cur.execute("select count(*) from absurd.t_default")
        (count,) = cur.fetchone()
    return int(count)


def remove_producer(producer: Producer) -> None:
    """Unschedule the producer job on the central catalog AND drop its scratch DB."""
    with open_central_connection("default") as cur:
        cur.execute("select cron.unschedule(%s)", [producer.jobid])
    drop_scratch_database(producer.target)


def provision_scratch_database(target: str) -> None:
    """(Re)create ``target`` fresh and install the Absurd schema + ``default`` queue so
    ``spawn_task`` works there. Dropped-and-recreated so a crash-orphaned scratch DB
    from a prior ``--reuse-db`` run never carries stale state."""
    drop_scratch_database(target)
    with open_admin_connection(resolve_cron_database("default")) as cur:
        cur.execute(
            psycopg.sql.SQL("create database {}").format(psycopg.sql.Identifier(target))
        )
    with open_admin_connection(target) as cur:
        cur.execute(read_absurd_schema_sql())
        cur.execute("select absurd.create_queue('default')")


def drop_scratch_database(target: str) -> None:
    """Drop ``target`` if present, first terminating any backends the pg_cron launcher
    (or a prior run) still holds on it so ``DROP DATABASE`` is not blocked."""
    with open_admin_connection(resolve_cron_database("default")) as cur:
        cur.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity "
            "where datname = %s and pid <> pg_backend_pid()",
            [target],
        )
        cur.execute(
            psycopg.sql.SQL("drop database if exists {}").format(
                psycopg.sql.Identifier(target)
            )
        )


@contextlib.contextmanager
def open_admin_connection(dbname: str) -> t.Iterator[psycopg.Cursor[t.Any]]:
    """A raw autocommit admin cursor on ``dbname`` (CREATE/DROP DATABASE and DDL can't
    run in a transaction), built from the default connection's params."""
    params: dict[str, t.Any] = connections["default"].get_connection_params()
    params.pop("cursor_factory", None)
    params["dbname"] = dbname
    conn = psycopg.connect(**params, autocommit=True)
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def build_producer_jobname(target: str) -> str:
    return f"_dj:{target}:s:producer"


def read_absurd_schema_sql() -> str:
    return (
        importlib.resources.files("django_absurd.migrations")
        .joinpath("0001_initial_0_4_0.sql")
        .read_text(encoding="utf-8")
    )
