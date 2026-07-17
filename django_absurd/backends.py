import typing as t

import psycopg.errors
from absurd_sdk import CreateQueueOptions
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.db.utils import ProgrammingError
from django.tasks import TaskResult, TaskResultStatus, task_backends
from django.tasks.backends.base import BaseTaskBackend
from django.tasks.base import TaskError
from django.tasks.exceptions import TaskResultDoesNotExist
from django.utils import timezone
from django.utils.module_loading import import_string

from django_absurd.admin_views import ADMIN_ENTITY_SPECS, build_queue_table_model
from django_absurd.connection import build_absurd_client

if t.TYPE_CHECKING:
    from django.tasks.base import Task


class AbsurdBackendOptions(t.TypedDict, total=False):
    DATABASE: str
    DEFAULT_MAX_ATTEMPTS: int
    QUEUES: dict[str, CreateQueueOptions]
    ENABLE_ADMIN: bool
    ADMIN_SITE: tuple[str, ...]
    SCHEDULER: str
    SCHEDULE: dict[str, t.Any]
    CLEANUP: dict[str, t.Any]


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
        self.scheduler: str = self.options.get("SCHEDULER", "beat")

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
            declared = get_declared_queues(self)
            # validate_task() rejects an undeclared queue (InvalidTask) when the
            # backend declares queues. This guards the empty-QUEUES config (where
            # that check is skipped) and the declared[...] access below from KeyError.
            if task.queue_name not in declared:
                msg = (
                    f"Queue '{task.queue_name}' is not declared in TASKS QUEUES. "
                    "Add it to the QUEUES list in your TASKS backend settings."
                )
                raise ImproperlyConfigured(msg) from None
            try:
                client.create_queue(task.queue_name, **declared[task.queue_name])
            except (
                psycopg.errors.UndefinedFunction,
                psycopg.errors.InvalidSchemaName,
            ):
                msg = "Absurd schema is not installed. Run: manage.py migrate"
                raise ImproperlyConfigured(msg) from None
            with transaction.atomic(using=self.database, savepoint=True):
                spawn_result = client.spawn(
                    task.module_path,
                    {"args": list(args), "kwargs": dict(kwargs)},
                    queue=task.queue_name,
                    max_attempts=max_attempts,
                    **merged,
                )
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
        task_row, run_row, worker_ids = fetch_task_rows(
            self.database, queue, task_id, result_id
        )
        return build_task_result(self, result_id, task_row, run_row, worker_ids)


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


def fetch_task_rows(
    database: str,
    queue: str,
    task_id: str,
    result_id: str,
) -> tuple[t.Any, t.Any, list[str]]:
    tasks_spec = next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
    runs_spec = next(s for s in ADMIN_ENTITY_SPECS if s.name == "runs")
    task_table = t.cast("t.Any", build_queue_table_model(tasks_spec, queue))
    run_table = t.cast("t.Any", build_queue_table_model(runs_spec, queue))
    try:
        with transaction.atomic(using=database, savepoint=True):
            task_row = task_table.objects.using(database).filter(pk=task_id).first()
    except ProgrammingError:
        raise TaskResultDoesNotExist(result_id) from None
    if task_row is None:
        raise TaskResultDoesNotExist(result_id)
    run_row = None
    if task_row.last_attempt_run is not None:
        try:
            with transaction.atomic(using=database, savepoint=True):
                run_row = (
                    run_table.objects.using(database)
                    .filter(pk=task_row.last_attempt_run)
                    .first()
                )
        except ProgrammingError:
            raise TaskResultDoesNotExist(result_id) from None
    try:
        with transaction.atomic(using=database, savepoint=True):
            worker_ids = list(
                run_table.objects.using(database)
                .filter(task_id=task_id, claimed_by__isnull=False)
                .order_by("attempt")
                .values_list("claimed_by", flat=True)
            )
    except ProgrammingError:
        raise TaskResultDoesNotExist(result_id) from None
    return task_row, run_row, worker_ids


def build_task_result(
    backend: "AbsurdBackend",
    result_id: str,
    task_row: t.Any,
    run_row: t.Any,
    worker_ids_list: list[str],
) -> TaskResult:
    queue, _ = decode_result_id(result_id)
    task_name: str = task_row.task_name
    params: dict[str, t.Any] = task_row.params
    enqueue_at = task_row.enqueue_at
    first_started_at = task_row.first_started_at
    state: str = task_row.state
    completed_payload = task_row.completed_payload
    cancelled_at = task_row.cancelled_at
    run_started = run_row.started_at if run_row is not None else None
    completed_at = run_row.completed_at if run_row is not None else None
    failed_at = run_row.failed_at if run_row is not None else None
    failure_reason = run_row.failure_reason if run_row is not None else None
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
    worker_ids: list[str] = worker_ids_list or []
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


def get_declared_queues(backend: "AbsurdBackend") -> dict[str, dict]:
    if "QUEUES" in backend.options:
        return dict(backend.options["QUEUES"])
    return {name: {} for name in backend.queues}


def get_absurd_backends() -> dict[str, "AbsurdBackend"]:
    return {
        alias: be
        for alias in task_backends
        if isinstance((be := task_backends[alias]), AbsurdBackend)
    }


def get_pg_cron_backends() -> dict[str, "AbsurdBackend"]:
    """The configured Absurd backends whose scheduler is pg_cron, keyed by alias."""
    return {
        alias: be
        for alias, be in get_absurd_backends().items()
        if be.scheduler == "pg_cron"
    }


def build_merged_spawn_options(defaults: t.Any, per_call: t.Any) -> dict[str, t.Any]:
    merged: dict[str, t.Any] = {}
    if defaults is not None:
        merged.update(defaults.to_kwargs())
    if per_call is not None:
        merged.update(per_call.to_kwargs())
    return merged
