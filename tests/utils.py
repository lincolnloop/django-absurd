import typing as t
import uuid

import psycopg
from absurd_sdk import Absurd, CreateQueueOptions, TaskResultSnapshot
from django.core.management import call_command
from django.db import connections

from django_absurd.connection import register_jsonb_loader

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


def get_task_result(
    task_id: str | uuid.UUID, queue: str = "default"
) -> TaskResultSnapshot | None:
    # task_id is either a TaskResult.id ("queue:uuid" string) or a raw SpawnResult
    # ["task_id"] — the latter is typed str by absurd_sdk's stub, but psycopg
    # deserializes the uuid column to a real uuid.UUID at runtime.
    raw_task_id = str(task_id).rsplit(":", 1)[-1]
    params = connections["default"].get_connection_params()
    conn = psycopg.connect(**params, autocommit=True)
    try:
        register_jsonb_loader(conn)
        return Absurd(conn).fetch_task_result(raw_task_id, queue)
    finally:
        conn.close()
