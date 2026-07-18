import asyncio
import inspect
import logging
import signal
import threading
import time
import typing as t
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass

import psycopg
import psycopg.errors
from absurd_sdk import AsyncAbsurd
from django.core.exceptions import ImproperlyConfigured
from django.db import close_old_connections, connections
from django.tasks import Task, TaskContext, TaskResult, TaskResultStatus
from django.utils import timezone
from django.utils.module_loading import import_string

from django_absurd.backends import AbsurdBackend
from django_absurd.connection import register_jsonb_loader, validate_backend
from django_absurd.context import AsyncDurableContext
from django_absurd.scheduler import run_beat

logger = logging.getLogger("django_absurd")


@dataclass(frozen=True)
class WorkerOptions:
    concurrency: int = 1
    claim_timeout: int = 120
    poll_interval: float = 0.25
    batch_size: int | None = None
    worker_id: str | None = None


class LazyTaskRegistry(dict[str, t.Any]):
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


def run_worker(
    backend: AbsurdBackend,
    queue: str,
    *,
    burst: bool = False,
    run_beat: bool = False,
    options: WorkerOptions | None = None,
) -> None:
    options = options or WorkerOptions()
    validate_backend(backend.database)
    asyncio.run(
        arun_worker(backend, queue, burst=burst, run_beat=run_beat, options=options)
    )


async def arun_worker(
    backend: AbsurdBackend,
    queue: str,
    *,
    burst: bool = False,
    run_beat: bool = False,
    options: WorkerOptions,
) -> None:
    with ThreadPoolExecutor(max_workers=options.concurrency) as executor:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(executor)
        async with aworker_client(backend, queue) as client:
            logger.info(
                "django-absurd worker started: alias=%s queue=%s database=%s "
                "burst=%s concurrency=%d",
                backend.alias,
                queue,
                backend.database,
                burst,
                options.concurrency,
            )
            if burst:
                await drain_queue(
                    client,
                    concurrency=options.concurrency,
                    claim_timeout=options.claim_timeout,
                    batch_size=options.batch_size,
                    worker_id=options.worker_id,
                )
            elif run_beat:
                await run_worker_with_beat(client, options, backend)
            else:
                await run_blocking_worker(client, options)


@asynccontextmanager
async def aworker_client(
    backend: AbsurdBackend, queue: str
) -> t.AsyncGenerator[AsyncAbsurd, None]:
    # DEDICATED async connection (built from Django's DB config, NOT Django's registered
    # connection). cursor_factory from Django's params is fatal for AsyncConnection
    # (sync cursor factory incompatible with async execute) — pop it before connecting.
    params: dict[str, t.Any] = connections[backend.database].get_connection_params()
    params.pop("cursor_factory", None)
    conn: psycopg.AsyncConnection = await psycopg.AsyncConnection.connect(
        **params, autocommit=True
    )
    try:
        register_jsonb_loader(conn)
        client = AsyncAbsurd(conn, queue_name=queue)
        client._registry = LazyTaskRegistry(queue)  # noqa: SLF001 -- SDK has no public fallback-resolver hook; install lazy import_string resolution
        try:
            # Probes for the schema-absent guard; raises if Absurd is not migrated.
            await client.list_queues()
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
        yield client
    finally:
        await conn.close()


async def drain_queue(
    client: AsyncAbsurd,
    *,
    concurrency: int = 1,
    claim_timeout: int = 120,
    batch_size: int | None = None,
    worker_id: str | None = None,
) -> int:
    count = 0
    while True:
        claimed = await client.claim_tasks(
            batch_size or concurrency, claim_timeout, worker_id or "worker"
        )
        if not claimed:
            break
        await asyncio.gather(
            *[client._execute_task(t_, claim_timeout) for t_ in claimed]  # noqa: SLF001 -- SDK exposes no public counted dispatch; mirrors work_batch
        )
        count += len(claimed)
    return count


def build_task_context(
    task: "Task[t.Any, t.Any]",
    ctx: t.Any,
    args: t.Sequence[t.Any],
    kwargs: dict[str, t.Any],
) -> "TaskContext[t.Any, t.Any]":
    attempt = read_sdk_attempt(ctx)
    task_result = t.cast(
        "TaskResult[t.Any, t.Any]",
        TaskResult(
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
        ),
    )
    if inspect.iscoroutinefunction(task.func):
        return AsyncDurableContext(task_result=task_result, absurd_ctx=ctx)
    return TaskContext(task_result=task_result)


def build_handler(
    task: "Task[t.Any, t.Any]",
) -> t.Callable[[t.Any, t.Any], t.Awaitable[t.Any]]:
    async def handler(params: t.Any, ctx: t.Any) -> t.Any:
        args = params.get("args", [])
        kwargs = params.get("kwargs", {})
        attempt = read_sdk_attempt(ctx)
        start = time.monotonic()
        logger.info(
            "django-absurd task started: name=%s task_id=%s attempt=%d",
            task.module_path,
            ctx.task_id,
            attempt,
        )
        try:
            if task.takes_context:
                ctx_ = build_task_context(task, ctx, args, kwargs)
            if inspect.iscoroutinefunction(task.func):
                if task.takes_context:
                    result = await task.func(ctx_, *args, **kwargs)
                else:
                    result = await task.func(*args, **kwargs)
            else:

                def call_sync() -> t.Any:
                    close_old_connections()
                    try:
                        if task.takes_context:
                            return task.func(ctx_, *args, **kwargs)
                        return task.func(*args, **kwargs)
                    finally:
                        close_old_connections()

                result = await asyncio.get_running_loop().run_in_executor(
                    None, call_sync
                )
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

    return handler


def read_sdk_attempt(ctx: t.Any) -> int:
    attempt: int = ctx._task["attempt"]  # noqa: SLF001 -- SDK TaskContext has no public attempt property
    return attempt


async def run_blocking_worker(client: AsyncAbsurd, options: WorkerOptions) -> None:
    loop = asyncio.get_running_loop()

    def handle_stop() -> None:
        client.stop_worker()

    loop.add_signal_handler(signal.SIGINT, handle_stop)
    loop.add_signal_handler(signal.SIGTERM, handle_stop)
    try:
        await client.start_worker(
            worker_id=options.worker_id,
            claim_timeout=options.claim_timeout,
            concurrency=options.concurrency,
            batch_size=options.batch_size,
            poll_interval=options.poll_interval,
        )
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)


async def run_worker_with_beat(
    client: AsyncAbsurd,
    options: WorkerOptions,
    backend: AbsurdBackend,
) -> None:
    beat_stop = threading.Event()
    beat_thread = threading.Thread(
        target=run_beat, args=(backend,), kwargs={"stop": beat_stop}, daemon=True
    )
    beat_thread.start()
    try:
        await run_blocking_worker(client, options)
    finally:
        beat_stop.set()
        await asyncio.get_running_loop().run_in_executor(None, beat_thread.join, 5)
