import logging
import signal
import time
import typing as t
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
import psycopg.errors
from absurd_sdk import Absurd
from django.core.exceptions import ImproperlyConfigured
from django.db import close_old_connections, connections
from django.tasks import Task, TaskContext, TaskResult, TaskResultStatus
from django.utils import timezone
from django.utils.module_loading import import_string

from django_absurd.backends import AbsurdBackend
from django_absurd.connection import register_jsonb_loader, validate_backend

logger = logging.getLogger("django_absurd")


@dataclass(frozen=True)
class WorkerOptions:
    concurrency: int = 1
    claim_timeout: int = 120
    poll_interval: float = 0.25
    batch_size: int | None = None
    worker_id: str | None = None


class LazyTaskRegistry(dict):
    """dict subclass that resolves tasks by import_string on first claim.

    The SDK reads _registry.get(task_name) in both _execute_task (burst) and
    start_worker (blocking). Overriding .get intercepts all dispatch reads and
    resolves any importable Task on demand — no tasks.py scan required.
    """

    def __init__(self, queue: str) -> None:
        super().__init__()
        self.queue = queue

    def get(self, name: t.Any, default: t.Any = None) -> t.Any:
        if name not in self:
            try:
                task = import_string(name)
            except ImportError:
                return default
            if not isinstance(task, Task):
                return default
            self[name] = {
                "name": name,
                "queue": self.queue,
                "default_max_attempts": None,
                "default_cancellation": None,
                "handler": build_handler(task),
            }
        return super().get(name, default)


@contextmanager
def worker_client(
    backend: AbsurdBackend, queue: str
) -> t.Generator[Absurd, None, None]:
    validate_backend(backend.database)
    # DEDICATED connection (built from Django's DB config, NOT Django's registered
    # connection). It must not be Django-managed: the adapter calls
    # close_old_connections() around each task to keep handler ORM connections fresh
    # in this long-running process, which would close Django's connection out from
    # under the SDK's claim/complete/fail bookkeeping. A raw psycopg connection isn't
    # in Django's registry, so close_old_connections() never touches it.
    params: dict[str, t.Any] = connections[backend.database].get_connection_params()
    conn: psycopg.Connection = psycopg.connect(**params, autocommit=True)
    try:
        register_jsonb_loader(conn)
        client = Absurd(conn, queue_name=queue)
        client._registry = LazyTaskRegistry(queue)  # noqa: SLF001 -- SDK has no public fallback-resolver hook; install lazy import_string resolution
        try:
            provisioned = client.list_queues()
        except (
            psycopg.errors.InvalidSchemaName,
            psycopg.errors.UndefinedTable,
            psycopg.errors.UndefinedFunction,
        ) as err:
            msg = (
                "Absurd schema is not installed."
                " Run: manage.py migrate then manage.py absurd_sync_queues"
            )
            raise ImproperlyConfigured(msg) from err
        if queue not in provisioned:
            msg = (
                f"Queue '{queue}' is not provisioned. Run: manage.py absurd_sync_queues"
            )
            raise ImproperlyConfigured(msg)
        yield client
    finally:
        conn.close()


def drain_queue(
    client: Absurd,
    *,
    claim_timeout: int = 120,
    batch_size: int | None = None,
    worker_id: str | None = None,
) -> int:
    count = 0
    while True:
        claimed = client.claim_tasks(
            batch_size or 1, claim_timeout, worker_id or "worker"
        )
        if not claimed:
            break
        for t_ in claimed:
            client._execute_task(t_, claim_timeout)  # noqa: SLF001 -- SDK exposes no public counted dispatch; mirrors work_batch
            count += 1
    return count


def run_worker(
    backend: AbsurdBackend,
    queue: str,
    *,
    burst: bool = False,
    options: WorkerOptions | None = None,
) -> None:
    options = options or WorkerOptions()
    with worker_client(backend, queue) as client:
        logger.info(
            "django-absurd worker starting: alias=%s queue=%s database=%s "
            "burst=%s concurrency=%d",
            backend.alias,
            queue,
            backend.database,
            burst,
            options.concurrency,
        )
        if burst:
            drain_queue(
                client,
                claim_timeout=options.claim_timeout,
                batch_size=options.batch_size,
                worker_id=options.worker_id,
            )
        else:
            run_blocking_worker(client, options)


def build_task_context(
    task: Task, ctx: t.Any, args: t.Sequence[t.Any], kwargs: dict[str, t.Any]
) -> TaskContext:
    attempt = read_sdk_attempt(ctx)
    task_result: TaskResult[..., t.Any] = TaskResult(
        task=task,
        id=ctx.task_id,
        status=TaskResultStatus.RUNNING,
        enqueued_at=None,
        started_at=timezone.now(),
        finished_at=None,
        last_attempted_at=None,
        args=list(args),
        kwargs=dict(kwargs),
        backend=task.backend,
        errors=[],
        worker_ids=["absurd"] * attempt,
    )
    return TaskContext(task_result=task_result)


def build_handler(task: Task) -> t.Callable[[t.Any, t.Any], t.Any]:
    def handler(params: t.Any, ctx: t.Any) -> t.Any:
        close_old_connections()
        args = params.get("args", [])
        kwargs = params.get("kwargs", {})
        attempt = read_sdk_attempt(ctx)
        start = time.monotonic()
        logger.info(
            "django-absurd task starting: name=%s task_id=%s attempt=%d",
            task.module_path,
            ctx.task_id,
            attempt,
        )
        try:
            if task.takes_context:
                ctx_ = build_task_context(task, ctx, args, kwargs)
                result = task.func(ctx_, *args, **kwargs)
            else:
                result = task.func(*args, **kwargs)
        except Exception:
            duration = time.monotonic() - start
            logger.exception(
                "django-absurd task failed: name=%s task_id=%s attempt=%d "
                "duration=%.3fs",
                task.module_path,
                ctx.task_id,
                attempt,
                duration,
            )
            raise
        else:
            duration = time.monotonic() - start
            logger.info(
                "django-absurd task completed: name=%s task_id=%s attempt=%d "
                "duration=%.3fs",
                task.module_path,
                ctx.task_id,
                attempt,
                duration,
            )
            return result
        finally:
            close_old_connections()

    return handler


def read_sdk_attempt(ctx: t.Any) -> int:
    return ctx._task["attempt"]  # noqa: SLF001 -- SDK TaskContext has no public attempt property


def run_blocking_worker(client: Absurd, options: WorkerOptions) -> None:
    prior_sigint = signal.signal(signal.SIGINT, lambda _s, _f: client.stop_worker())
    prior_sigterm = signal.signal(signal.SIGTERM, lambda _s, _f: client.stop_worker())
    try:
        client.start_worker(
            worker_id=options.worker_id,
            claim_timeout=options.claim_timeout,
            concurrency=options.concurrency,
            batch_size=options.batch_size,
            poll_interval=options.poll_interval,
        )
    finally:
        signal.signal(signal.SIGINT, prior_sigint)
        signal.signal(signal.SIGTERM, prior_sigterm)
