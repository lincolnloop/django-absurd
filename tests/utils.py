import typing as t

import psycopg
import psycopg.sql
from absurd_sdk import Absurd, CreateQueueOptions
from django.core.management import call_command
from django.db import connections

from django_absurd.test import open_test_connection

if t.TYPE_CHECKING:
    from collections.abc import Mapping

ABSURD_BACKEND = "django_absurd.backends.AbsurdBackend"

# tests/tasks.py declares @task(queue_name="other") and @task(queue_name="reports")
# at module level; importing any task from that module validates those queue names
# against the current backend. Any test that imports from tests.tasks (directly or
# transitively) must therefore declare at least "other" and "reports" alongside
# "default" — this is why make_tasks_settings() defaults to all three.
DECLARED_QUEUES: dict[str, CreateQueueOptions] = {
    "default": {},
    "other": {},
    "reports": {},
}


class HasContent(t.Protocol):
    """What parse_html()/rows() actually need — matches both django.http.HttpResponse
    and the test client's private ``_MonkeyPatchedWSGIResponse``."""

    content: bytes


def make_tasks_settings(
    queues: "Mapping[str, CreateQueueOptions] | None" = None,
    schedule: "Mapping[str, dict[str, object]] | None" = None,
    cleanup: dict[str, str] | None = None,
    database: str | None = None,
    default_max_attempts: int | None = None,
) -> dict[str, dict[str, t.Any]]:
    """Build a ``settings.TASKS`` dict for the AbsurdBackend.

    ``queues`` defaults to ``DECLARED_QUEUES`` (default/other/reports); pass an
    override for tests exercising a different catalog (e.g. an undeclared queue).
    """
    options: dict[str, t.Any] = {
        "QUEUES": dict(DECLARED_QUEUES if queues is None else queues),
    }
    if schedule is not None:
        options["SCHEDULE"] = schedule
    if cleanup is not None:
        options["CLEANUP"] = cleanup
    if database is not None:
        options["DATABASE"] = database
    if default_max_attempts is not None:
        options["DEFAULT_MAX_ATTEMPTS"] = default_max_attempts
    return {"default": {"BACKEND": ABSURD_BACKEND, "OPTIONS": options}}


def run_absurd_worker(queue: str = "default", concurrency: int = 1) -> None:
    call_command("absurd_worker", queue=queue, burst=True, concurrency=concurrency)


def claim_one_run(queue: str = "default", *, claim_timeout: int) -> None:
    """Take a lease on one run without executing it.

    Leaves the run ``running`` with a ``claim_expires_at`` ``claim_timeout`` seconds
    out, so advancing durable time past that lease lets the ``$ClaimTimeout`` sweep
    inside the next ``claim_task`` expire and re-arm it.
    """
    params = connections["default"].get_connection_params()
    conn = psycopg.connect(**params, autocommit=True)
    try:
        Absurd(conn, queue_name=queue).claim_tasks(claim_timeout=claim_timeout)
    finally:
        conn.close()


def set_database_fake_now(value: str) -> None:
    """Plant a database-level ``absurd.fake_now``, as a killed frozen test would leave.

    ``ALTER DATABASE`` rejects bind parameters, hence the composed literal. The database
    name comes from the settings dict at runtime so an xdist worker plants it on its own
    test database.
    """
    statement = psycopg.sql.SQL(
        "alter database {name} set absurd.fake_now = {value}"
    ).format(
        name=psycopg.sql.Identifier(connections["default"].settings_dict["NAME"]),
        value=psycopg.sql.Literal(value),
    )
    with open_test_connection("default") as cursor:
        cursor.execute(statement)


def read_database_fake_now() -> str | None:
    """Return the database-level ``absurd.fake_now``, or ``None`` when it is unset.

    Read from the catalog rather than from a session, so it reports what a NEW
    connection would inherit — the thing that outlives a killed run.
    """
    with open_test_connection("default") as cursor:
        cursor.execute(
            "select split_part(cfg, '=', 2) from pg_db_role_setting s "
            "join pg_database d on d.oid = s.setdatabase "
            "cross join unnest(s.setconfig) as cfg "
            "where d.datname = %s and cfg like 'absurd.fake_now=%%'",
            [connections["default"].settings_dict["NAME"]],
        )
        row = cursor.fetchone()
    return None if row is None else str(row[0])


def read_session_fake_now() -> str:
    """``absurd.fake_now`` as Django's own open session sees it — the session-level
    twin of ``read_database_fake_now``, what an ``enqueue()`` stamps a task with. Reads
    back as an empty string once RESET, never as NULL.
    """
    with connections["default"].cursor() as cursor:
        cursor.execute("select current_setting('absurd.fake_now', true)")
        return str(cursor.fetchone()[0])


def reset_database_fake_now() -> None:
    """Clear the database-level ``absurd.fake_now`` — cleanup half of planting one."""
    statement = psycopg.sql.SQL("alter database {name} reset absurd.fake_now").format(
        name=psycopg.sql.Identifier(connections["default"].settings_dict["NAME"])
    )
    with open_test_connection("default") as cursor:
        cursor.execute(statement)
