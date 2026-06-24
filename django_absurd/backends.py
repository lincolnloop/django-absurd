import typing as t

import psycopg.errors
import psycopg.sql
from absurd_sdk import CreateQueueOptions
from django.core.exceptions import ImproperlyConfigured
from django.db import connections, transaction
from django.db.utils import ProgrammingError
from django.tasks import TaskResult, TaskResultStatus
from django.tasks.backends.base import BaseTaskBackend
from django.tasks.base import TaskError
from django.tasks.exceptions import TaskResultDoesNotExist
from django.utils import timezone
from django.utils.module_loading import import_string

from django_absurd.connection import build_absurd_client, register_jsonb_loader

if t.TYPE_CHECKING:
    from django.tasks.base import Task


class AbsurdBackendOptions(t.TypedDict, total=False):
    DATABASE: str
    DEFAULT_MAX_ATTEMPTS: int
    QUEUES: dict[str, CreateQueueOptions]


class AbsurdBackend(BaseTaskBackend):
    supports_get_result = True
    supports_async_task = True
    supports_defer = False
    supports_priority = False

    def __init__(self, alias: str, params: dict[str, t.Any]) -> None:
        self.has_top_level_queues: bool = "QUEUES" in params
        super().__init__(alias, params)
        if "QUEUES" in self.options:
            self.queues = set(self.options["QUEUES"])  # type: ignore[assignment]
        self.database: str = self.options.get("DATABASE", "default")
        self.default_max_attempts: int = self.options.get("DEFAULT_MAX_ATTEMPTS", 5)

    def enqueue(
        self, task: "Task", args: list[t.Any], kwargs: dict[str, t.Any]
    ) -> TaskResult:
        self.validate_task(task)
        client = build_absurd_client(self.database)
        spawn_params = kwargs.pop("absurd_spawn_params", None)
        defaults = getattr(task.func, "absurd_default_params", None)
        merged = build_merged_spawn_options(defaults, spawn_params)
        max_attempts: int = merged.pop("max_attempts", self.default_max_attempts)
        try:
            # Savepoint so a misconfig DB error (below) rolls back only the spawn,
            # leaving an enclosing transaction.atomic() block usable.
            with transaction.atomic(using=self.database, savepoint=True):
                spawn_result = client.spawn(
                    task.module_path,
                    {"args": list(args), "kwargs": dict(kwargs)},
                    queue=task.queue_name,
                    max_attempts=max_attempts,
                    **merged,
                )
        except (
            psycopg.errors.UndefinedTable,
            psycopg.errors.UndefinedFunction,
            psycopg.errors.InvalidSchemaName,
        ):
            msg = (
                f"Queue '{task.queue_name}' is not provisioned in Absurd. "
                "Run manage.py absurd_sync_queues (and manage.py migrate if the "
                "absurd schema is absent)."
            )
            raise ImproperlyConfigured(msg) from None
        return TaskResult(
            task=task,
            id=f"{task.queue_name}:{spawn_result['task_id']}",
            status=TaskResultStatus.READY,
            enqueued_at=timezone.now(),
            started_at=None,
            finished_at=None,
            last_attempted_at=None,
            args=list(args),
            kwargs=dict(kwargs),
            backend=self.alias,
            errors=[],
            worker_ids=[],
        )

    def get_result(self, result_id: str) -> TaskResult:
        queue, task_id = decode_result_id(result_id)
        if queue not in self.queues:
            raise TaskResultDoesNotExist(result_id)
        connection = connections[self.database]
        connection.ensure_connection()
        sql = psycopg.sql.SQL(
            "SELECT t.task_name, t.params, t.enqueue_at, t.first_started_at,"
            " t.state, t.completed_payload, t.cancelled_at,"
            " lr.started_at AS run_started, lr.completed_at, lr.failed_at,"
            " lr.failure_reason,"
            " (SELECT array_agg(r.claimed_by ORDER BY r.attempt)"
            "  FROM {r} r"
            "  WHERE r.task_id = t.task_id AND r.claimed_by IS NOT NULL) AS worker_ids"
            " FROM {t} t"
            " LEFT JOIN {r} lr ON lr.run_id = t.last_attempt_run"
            " WHERE t.task_id = %s"
        ).format(
            t=psycopg.sql.Identifier("absurd", f"t_{queue}"),
            r=psycopg.sql.Identifier("absurd", f"r_{queue}"),
        )
        rendered = sql.as_string(connection.connection)
        try:
            with (
                transaction.atomic(using=self.database, savepoint=True),
                connection.cursor() as cursor,
            ):
                register_jsonb_loader(cursor)
                cursor.execute(rendered, [task_id])
                row = cursor.fetchone()
        except ProgrammingError:
            raise TaskResultDoesNotExist(result_id) from None
        if row is None:
            raise TaskResultDoesNotExist(result_id)
        return build_task_result(self, result_id, queue, row)


def decode_result_id(result_id: str) -> tuple[str, str]:
    parts = result_id.rsplit(":", 1)
    if len(parts) != 2:
        raise TaskResultDoesNotExist(result_id)
    return parts[0], parts[1]


STATE_TO_STATUS: dict[str, TaskResultStatus] = {
    "pending": TaskResultStatus.READY,
    "running": TaskResultStatus.RUNNING,
    "sleeping": TaskResultStatus.RUNNING,
    "completed": TaskResultStatus.SUCCESSFUL,
    "failed": TaskResultStatus.FAILED,
    "cancelled": TaskResultStatus.FAILED,
}


def map_state_to_status(state: str) -> TaskResultStatus:
    return STATE_TO_STATUS.get(state, TaskResultStatus.READY)


def build_task_result(
    backend: "AbsurdBackend",
    result_id: str,
    queue: str,
    row: t.Any,
) -> TaskResult:
    (
        task_name,
        params,
        enqueue_at,
        first_started_at,
        state,
        completed_payload,
        cancelled_at,
        run_started,
        completed_at,
        failed_at,
        failure_reason,
        worker_ids_array,
    ) = row
    try:
        task_obj = import_string(task_name)
    except ImportError:
        msg = f"task '{task_name}' is no longer importable"
        raise ImproperlyConfigured(msg) from None
    if task_obj.queue_name != queue:
        task_obj = task_obj.using(queue_name=queue)
    status = map_state_to_status(state)
    errors: list[TaskError] = []
    if state == "failed" and failure_reason:
        errors = [
            TaskError(
                exception_class_path=failure_reason.get("name", ""),
                traceback=failure_reason.get("traceback")
                or failure_reason.get("message", ""),
            )
        ]
    finished_at = completed_at or failed_at or cancelled_at
    worker_ids: list[str] = worker_ids_array or []
    result: TaskResult = TaskResult(
        task=task_obj,
        id=result_id,
        status=status,
        enqueued_at=enqueue_at,
        started_at=first_started_at,
        finished_at=finished_at,
        last_attempted_at=run_started,
        args=params["args"],
        kwargs=params["kwargs"],
        backend=backend.alias,
        errors=errors,
        worker_ids=worker_ids,
    )
    if state == "completed":
        object.__setattr__(result, "_return_value", completed_payload)
    return result


def build_merged_spawn_options(defaults: t.Any, per_call: t.Any) -> dict[str, t.Any]:
    merged: dict[str, t.Any] = {}
    if defaults is not None:
        merged.update(defaults.to_kwargs())
    if per_call is not None:
        merged.update(per_call.to_kwargs())
    return merged
