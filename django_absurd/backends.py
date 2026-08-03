import datetime as dt
import typing as t
import uuid

import psycopg.errors
from absurd_sdk import CreateQueueOptions, JsonObject, JsonValue
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.db.utils import ProgrammingError
from django.tasks import TaskResult, TaskResultStatus, task_backends
from django.tasks.backends.base import BaseTaskBackend
from django.tasks.base import TaskError
from django.tasks.exceptions import TaskResultDoesNotExist
from django.tasks.signals import task_enqueued
from django.utils import timezone
from django.utils.module_loading import import_string

from django_absurd import dispatch
from django_absurd.admin_views import ADMIN_ENTITY_SPECS, build_queue_table_model
from django_absurd.connection import build_absurd_client
from django_absurd.deferred import DEFER_NAME_SUFFIX
from django_absurd.exceptions import QueueNotDeclaredError
from django_absurd.tasks import AbsurdTask, SpawnKwargs, build_merged_spawn_options

if t.TYPE_CHECKING:
    from django.tasks.base import Task

PG_CRON_APP_NAME = "django_absurd.pg_cron"


class TaskParams(t.TypedDict):
    """The positional/keyword args a task was enqueued with (JSON-serializable)."""

    args: list[t.Any]
    kwargs: dict[str, t.Any]


class FailureReason(t.TypedDict):
    """A failed run's serialized error, shaped by absurd_sdk's _serialize_error.

    ``message`` is always present; ``name``/``traceback`` are only set when the
    original error was an Exception (vs. e.g. a plain cancellation).
    """

    message: str
    name: t.NotRequired[str]
    traceback: t.NotRequired[str | None]


class TaskModel(t.Protocol):
    """The fields read off a per-queue Absurd task model instance.

    These models are built dynamically per queue (build_queue_table_model), so
    django-stubs cannot type their fields; this Protocol names the subset we read.

    Optional here means NULLABLE IN ABSURD'S OWN SCHEMA, not merely nullable on the
    dynamic model: build_queue_table_model declares every non-pk column null=True
    because it cannot know better, while t_<queue> itself constrains task_id,
    task_name, params, enqueue_at, state and attempts NOT NULL.
    """

    task_id: uuid.UUID
    task_name: str
    params: TaskParams
    enqueue_at: dt.datetime
    first_started_at: dt.datetime | None
    state: str
    attempts: int
    completed_payload: JsonValue
    cancelled_at: dt.datetime | None
    headers: JsonObject
    last_attempt_run: uuid.UUID | None


class RunModel(t.Protocol):
    """The fields read off a per-queue Absurd run model instance.

    Optional means nullable in ``r_<queue>`` itself, as on ``TaskModel``; ``state`` is
    the one field below that Absurd constrains NOT NULL.

    ``available_at`` is deliberately absent, and every read of this model defers it: an
    indefinite ``await_event`` parks a run at Postgres's ``'infinity'``, which psycopg
    refuses to decode.
    """

    state: str
    started_at: dt.datetime | None
    completed_at: dt.datetime | None
    failed_at: dt.datetime | None
    result: JsonValue
    failure_reason: FailureReason | None


class AbsurdBackendOptions(t.TypedDict, total=False):
    DATABASE: str
    DEFAULT_MAX_ATTEMPTS: int
    QUEUES: dict[str, CreateQueueOptions]
    ENABLE_ADMIN: bool
    ADMIN_SITE: tuple[str, ...]
    SCHEDULE: dict[str, dict[str, object]]
    CLEANUP: dict[str, str]
    PG_CRON_ON_TEST_DB: bool


class AbsurdBackend(BaseTaskBackend):
    task_class = AbsurdTask
    supports_get_result = True
    supports_async_task = True
    supports_defer = True
    supports_priority = False

    def __init__(self, alias: str, params: dict[str, t.Any]) -> None:
        self.has_top_level_queues: bool = "QUEUES" in params
        super().__init__(alias, params)
        if "QUEUES" in self.options:
            self.queues = set(self.options["QUEUES"])  # type: ignore[assignment]
        self.database: str = self.options.get("DATABASE", "default")
        self.default_max_attempts: int = self.options.get("DEFAULT_MAX_ATTEMPTS", 5)
        self.scheduler: str = (
            "pg_cron" if apps.is_installed(PG_CRON_APP_NAME) else "beat"
        )

    def enqueue(
        self, task: "Task[t.Any, t.Any]", args: list[t.Any], kwargs: dict[str, t.Any]
    ) -> "TaskResult[t.Any, t.Any]":
        self.validate_task(task)
        client = build_absurd_client(self.database)
        # The else covers a task DEFINED on a non-Absurd backend: it keeps
        # task_class = Task, and .using(backend=...) preserves that class, so its
        # decorator defaults are still only on the function. A task defined on this
        # backend stays an AbsurdTask through any .using(), scheduler routing included.
        params = (
            task.absurd_params
            if isinstance(task, AbsurdTask)
            else getattr(task.func, "absurd_params", None)
        )
        merged = build_merged_spawn_options(params, None)
        merged.setdefault("max_attempts", self.default_max_attempts)
        # Absurd's spawn takes no available_at, so a deferred enqueue spawns a WRAPPER
        # row that sleeps until due and then enqueues the caller's task. The caller's
        # task is therefore never claimed before its work exists, which is what both of
        # Absurd's cancellation rules measure from. The caller's own options ride in the
        # wrapper's params and are replayed on the inner enqueue; the wrapper gets only
        # its own max_attempts.
        if task.run_after is not None:
            spawn_name = f"{task.module_path}{DEFER_NAME_SUFFIX}"
            spawn_params: dict[str, t.Any] = {
                "args": [],
                "kwargs": {
                    "args": list(args),
                    "kwargs": dict(kwargs),
                    "queue": task.queue_name,
                    "options": dict(merged),
                    "due": normalize_to_utc(task.run_after).isoformat(),
                },
            }
            wrapper_options: SpawnKwargs = {"max_attempts": self.default_max_attempts}
            # The caller's key stays on the inner enqueue, so the wrapper takes a
            # derived one: verbatim, it would reserve the key against its own row and
            # the enqueue it wakes to make would dedupe against itself.
            if "idempotency_key" in merged:
                wrapper_options["idempotency_key"] = (
                    f"{merged['idempotency_key']}{DEFER_NAME_SUFFIX}"
                )
            merged = wrapper_options
        else:
            spawn_name = task.module_path
            spawn_params = {"args": list(args), "kwargs": dict(kwargs)}
        try:
            # Savepoint so a misconfig DB error (below) rolls back only the spawn,
            # leaving an enclosing transaction.atomic() block usable.
            with transaction.atomic(using=self.database, savepoint=True):
                spawn_result = client.spawn(
                    spawn_name,
                    spawn_params,
                    queue=task.queue_name,
                    **merged,
                )
        except (
            psycopg.errors.UndefinedTable,
            psycopg.errors.UndefinedFunction,
            psycopg.errors.InvalidSchemaName,
        ) as exc:
            declared = get_declared_queues(self)
            # validate_task() rejects an undeclared queue (InvalidTask) when the
            # backend declares queues. This guards the empty-QUEUES config (where
            # that check is skipped) and the declared[...] access below from KeyError.
            if task.queue_name not in declared:
                raise QueueNotDeclaredError(
                    task.queue_name, self.alias, self.queues
                ) from exc
            try:
                client.create_queue(task.queue_name, **declared[task.queue_name])
            except (
                psycopg.errors.UndefinedFunction,
                psycopg.errors.InvalidSchemaName,
            ) as exc:
                msg = "Absurd schema is not installed. Run: manage.py migrate"
                raise ImproperlyConfigured(msg) from exc
            with transaction.atomic(using=self.database, savepoint=True):
                spawn_result = client.spawn(
                    spawn_name,
                    spawn_params,
                    queue=task.queue_name,
                    **merged,
                )
        result: TaskResult[t.Any, t.Any] = TaskResult(
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
        dispatch.send_task_signal(task_enqueued, type(self), result)
        return result

    def get_result(self, result_id: str) -> "TaskResult[t.Any, t.Any]":
        queue, task_id = decode_result_id(result_id)
        if queue not in self.queues:
            raise TaskResultDoesNotExist(result_id)
        task, run, worker_ids = fetch_task_and_run(
            self.database, queue, task_id, result_id
        )
        if task.task_name.endswith(DEFER_NAME_SUFFIX) and task.state == "completed":
            # Its payload is the id of the task it enqueued, so the caller's id keeps
            # describing their own task for the rest of its life.
            return self.get_result(str(task.completed_payload))
        return build_task_result(self, result_id, task, run, worker_ids)


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


def fetch_task_and_run(
    database: str,
    queue: str,
    task_id: str,
    result_id: str,
) -> tuple[TaskModel, RunModel | None, list[str]]:
    tasks_spec = next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
    runs_spec = next(s for s in ADMIN_ENTITY_SPECS if s.name == "runs")
    task_model: type[t.Any] = build_queue_table_model(tasks_spec, queue)
    run_model: type[t.Any] = build_queue_table_model(runs_spec, queue)
    try:
        with transaction.atomic(using=database, savepoint=True):
            task: TaskModel | None = (
                task_model.objects.using(database).filter(pk=task_id).first()
            )
    except ProgrammingError as exc:
        raise TaskResultDoesNotExist(result_id) from exc
    if task is None:
        raise TaskResultDoesNotExist(result_id)
    # Only the task read above translates a missing relation: past it the queue's
    # schema demonstrably exists, so a ProgrammingError here is a broken database, not
    # an absent task, and must not be relabelled as one.
    run: RunModel | None = None
    if task.last_attempt_run is not None:
        with transaction.atomic(using=database, savepoint=True):
            run = (
                run_model.objects.using(database)
                .filter(pk=task.last_attempt_run)
                # An indefinite await_event can leave available_at as Postgres's
                # 'infinity' sentinel, which psycopg can't decode — defer it since
                # build_task_result() below never reads it.
                .defer("available_at")
                .first()
            )
    with transaction.atomic(using=database, savepoint=True):
        worker_ids = list(
            run_model.objects.using(database)
            .filter(task_id=task_id, claimed_by__isnull=False)
            .order_by("attempt")
            .values_list("claimed_by", flat=True)
        )
    return task, run, worker_ids


def build_task_result(
    backend: "AbsurdBackend",
    result_id: str,
    task: TaskModel,
    run: RunModel | None,
    worker_ids_list: list[str],
) -> "TaskResult[t.Any, t.Any]":
    queue, _ = decode_result_id(result_id)
    task_name: str = task.task_name
    params: TaskParams = task.params
    enqueue_at = task.enqueue_at
    first_started_at = task.first_started_at
    state: str = task.state
    completed_payload = task.completed_payload
    cancelled_at = task.cancelled_at
    run_started = run.started_at if run else None
    completed_at = run.completed_at if run else None
    failed_at = run.failed_at if run else None
    failure_reason = run.failure_reason if run else None
    is_deferred_wrapper = task_name.endswith(DEFER_NAME_SUFFIX)
    if is_deferred_wrapper:
        # The caller's id names the wrapper, but their TaskResult must describe THEIR
        # task: its path is the name's suffix and its call is in the wrapper's kwargs.
        task_name = task_name.removesuffix(DEFER_NAME_SUFFIX)
        spec = params["kwargs"]
        params = {"args": list(spec["args"]), "kwargs": dict(spec["kwargs"])}
        # Claimed at t=0 only so it could sleep — nothing of the caller's has started.
        first_started_at = None
        run_started = None
    try:
        task_obj = import_string(task_name)
    except ImportError as exc:
        msg = f"task '{task_name}' is no longer importable"
        raise ImproperlyConfigured(msg) from exc
    # Rebinding re-runs Task.__post_init__, which validates the queue against the
    # DEFINITION's backend alias — one this read need not have configured. So the alias
    # moves with the queue.
    if (task_obj.queue_name, task_obj.backend) != (queue, backend.alias):
        task_obj = task_obj.using(queue_name=queue, backend=backend.alias)
    status = map_state_to_status(state)
    if is_deferred_wrapper and state == "sleeping":
        # No Django status means "the deferral is retrying", and READY is the honest
        # answer to whether the caller's task has started — so a wrapper waiting for its
        # due time and one in retry backoff both read READY. A launch that keeps failing
        # ends FAILED once out of attempts, carrying its error.
        status = TaskResultStatus.READY
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
    result: TaskResult[t.Any, t.Any] = TaskResult(
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


def normalize_to_utc(moment: dt.datetime) -> dt.datetime:
    """Aware UTC, whatever awareness the caller had.

    Django only rejects a naive ``run_after`` when ``USE_TZ`` is true, so under
    ``USE_TZ=False`` a naive one reaches here — and would reach the SDK, whose clock is
    aware, raising a ``TypeError`` per attempt until the task ran out of them. A naive
    value means local time, per Django's own convention.
    """
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment)
    return moment.astimezone(dt.UTC)


def get_declared_queues(backend: "AbsurdBackend") -> dict[str, CreateQueueOptions]:
    if "QUEUES" in backend.options:
        return dict(backend.options["QUEUES"])
    return {name: CreateQueueOptions() for name in backend.queues}


def get_absurd_backends() -> dict[str, "AbsurdBackend"]:
    return {
        alias: backend
        for alias in task_backends
        if isinstance((backend := task_backends[alias]), AbsurdBackend)
    }
