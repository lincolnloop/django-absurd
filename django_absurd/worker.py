import asyncio
import contextlib
import inspect
import logging
import os
import signal
import socket
import threading
import typing as t
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from traceback import format_exception

import psycopg
import psycopg.errors
from absurd_sdk import (
    AbsurdHooks,
    AsyncAbsurd,
    AsyncTaskContext,
    CancelledTask,
    ClaimedTask,
    FailedTask,
    JsonValue,
    SuspendTask,
)
from asgiref.sync import sync_to_async
from django.core.exceptions import ImproperlyConfigured
from django.db import close_old_connections, connections
from django.tasks import Task, TaskContext, TaskResult, TaskResultStatus
from django.tasks.base import TaskError
from django.tasks.signals import task_finished, task_started
from django.utils import timezone
from django.utils.json import normalize_json
from django.utils.module_loading import import_string

from django_absurd import admin_views, dispatch
from django_absurd.backends import AbsurdBackend, RunModel, TaskParams
from django_absurd.connection import register_jsonb_loader, validate_backend
from django_absurd.context import WORKER_LOOP
from django_absurd.deferred import DEFER_NAME_SUFFIX, build_deferred_handler
from django_absurd.exceptions import QueueNotDeclaredError, QueueNotProvisionedError
from django_absurd.hooks import (
    log_before_spawn,
    log_task_execution,
    read_sdk_claimed_task,
)
from django_absurd.management.base import resolve_backend
from django_absurd.queues import names_a_queue_table
from django_absurd.scheduler import run_beat

logger = logging.getLogger(__name__)

D = t.TypeVar("D")


@dataclass(frozen=True)
class WorkerOptions:
    concurrency: int = 1
    claim_timeout: int = 120
    poll_interval: float = 0.25
    batch_size: int | None = None
    worker_id: str | None = None


@dataclass(frozen=True)
class DrainedRun:
    """One run executed during a drain, in claim order.

    ``state``/``result``/``failure`` cost one read per run, taken immediately after
    THAT run executes — never batched at drain end, so a run that re-arms itself (an
    ``await_event`` waiter) keeps its earlier ``sleeping`` appearance honest instead
    of it being overwritten by the same run's later ``completed`` one.

    ``run_id``/``task_id`` are ``uuid.UUID`` at runtime even though the SDK's
    ``ClaimedTask`` stub types them ``str`` — psycopg deserializes the uuid columns.
    """

    run_id: uuid.UUID
    task_id: uuid.UUID
    task_name: str
    params: t.Any
    attempt: int
    state: str
    result: t.Any | None
    failure: t.Any | None


class LazyTaskRegistry(dict[str, dict[str, t.Any]]):
    """dict subclass that resolves tasks by import_string on first claim.

    The SDK reads _registry.get(task_name) in _execute_task, which every dispatch
    goes through — the drain and the blocking loop alike. Overriding .get intercepts
    all dispatch reads and resolves any importable Task on demand — no tasks.py scan
    required. Value type matches the SDK's own
    ``_registry: Dict[str, Dict[str, Any]]`` declaration — each entry mixes
    str/None/Callable fields, so the inner ``Any`` is a genuine boundary.
    """

    def __init__(self, queue: str, backend: AbsurdBackend) -> None:
        super().__init__()
        self.queue = queue
        self.backend = backend

    @t.overload
    def get(self, name: str, default: None = None, /) -> dict[str, t.Any] | None: ...
    @t.overload
    def get(self, name: str, default: dict[str, t.Any], /) -> dict[str, t.Any]: ...
    @t.overload
    def get(self, name: str, default: D, /) -> dict[str, t.Any] | D: ...
    def get(
        self, name: str, default: dict[str, t.Any] | D | None = None
    ) -> dict[str, t.Any] | D | None:
        if name not in self:
            if name.endswith(DEFER_NAME_SUFFIX):
                self[name] = {
                    "name": name,
                    "queue": self.queue,
                    "default_max_attempts": None,
                    "default_cancellation": None,
                    "handler": build_deferred_handler(
                        name.removesuffix(DEFER_NAME_SUFFIX)
                    ),
                }
                return super().get(name, default)
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
                "handler": build_handler(task, backend=self.backend, queue=self.queue),
            }
        return super().get(name, default)


def drain_queue(queue: str = "default") -> list[DrainedRun]:
    """Drain ``queue`` in-process, one ``DrainedRun`` per run, in claim order.

    The entry point behind ``AbsurdTestRuntime.drain``: resolves the backend itself
    and takes no ``WorkerOptions`` — worker knobs live on the ``absurd_worker`` CLI.
    Provisions nothing: migrate already provisions every declared queue, so a queue
    declared mid-test surfaces as the same ``QueueNotProvisionedError``
    ``events.emit_event`` raises — not as schema DDL from a call tests read results
    through. Only a relation of THIS queue's own earns that translation; any other
    missing relation re-raises as itself (chained, so it stays visible), because
    "run absurd_sync_queues" would send the reader to the wrong door.
    """
    backend = resolve_backend()
    if queue not in backend.queues:
        raise QueueNotDeclaredError(queue, backend.alias, backend.queues)
    validate_backend(backend.database)

    try:
        return asyncio.run(arun_drain(backend, queue, WorkerOptions()))
    except psycopg.errors.UndefinedTable as exc:
        if not names_a_queue_table(exc, queue):
            raise
        raise QueueNotProvisionedError(queue) from exc


def run_worker(
    backend: AbsurdBackend,
    queue: str,
    *,
    run_beat: bool = False,
    options: WorkerOptions | None = None,
    on_stop_requested: t.Callable[[], None] | None = None,
) -> None:
    options = options or WorkerOptions()
    validate_backend(backend.database)
    asyncio.run(
        arun_worker(
            backend,
            queue,
            run_beat=run_beat,
            options=options,
            on_stop_requested=on_stop_requested,
        )
    )


async def arun_worker(
    backend: AbsurdBackend,
    queue: str,
    *,
    run_beat: bool = False,
    options: WorkerOptions,
    on_stop_requested: t.Callable[[], None] | None = None,
) -> None:
    async with open_worker_runtime(backend, queue, options) as client:
        if run_beat:
            await run_worker_with_beat(
                client, options, backend, on_stop_requested=on_stop_requested
            )
        else:
            await run_blocking_worker(
                client, options, on_stop_requested=on_stop_requested
            )
        logger.info(
            "worker stopped: alias=%s queue=%s database=%s",
            backend.alias,
            queue,
            backend.database,
        )


async def arun_drain(
    backend: AbsurdBackend, queue: str, options: WorkerOptions
) -> list[DrainedRun]:
    """Drain ``queue`` on the same runtime the blocking worker runs on.

    The coroutine behind ``drain_queue``: same executor, same client, same started
    line — it differs only in claiming until the queue is empty instead of until a
    signal arrives, so its stopped line can report how many runs that took.
    """
    async with open_worker_runtime(backend, queue, options) as client:
        drained = await adrain_queue(backend.database, client, queue, options)
        logger.info(
            "worker stopped: alias=%s queue=%s database=%s runs=%d",
            backend.alias,
            queue,
            backend.database,
            len(drained),
        )
        return drained


@asynccontextmanager
async def open_worker_runtime(
    backend: AbsurdBackend, queue: str, options: WorkerOptions
) -> t.AsyncGenerator[AsyncAbsurd, None]:
    """Set up what every worker entry point needs, and announce it started.

    A thread pool sized to ``concurrency`` installed as the loop's default executor
    (every sync task body lands there), the SDK client on its own connection, and the
    ``worker started`` line. Each caller logs its own ``worker stopped`` line, because
    only a drain has a run count to report.
    """
    with ThreadPoolExecutor(max_workers=options.concurrency) as executor:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(executor)
        async with aworker_client(backend, queue) as client:
            logger.info(
                "worker started: alias=%s queue=%s database=%s concurrency=%d",
                backend.alias,
                queue,
                backend.database,
                options.concurrency,
            )
            yield client


@asynccontextmanager
async def aworker_client(
    backend: AbsurdBackend, queue: str
) -> t.AsyncGenerator[AsyncAbsurd, None]:
    # DEDICATED async connection (built from Django's DB config, NOT Django's registered
    # connection). cursor_factory from Django's params is fatal for AsyncConnection
    # (sync cursor factory incompatible with async execute) — pop it before connecting.
    # The connection stays private to the client built on it: every read django-absurd
    # makes for itself goes through the ORM instead.
    params: dict[str, t.Any] = connections[backend.database].get_connection_params()
    params.pop("cursor_factory", None)
    conn: psycopg.AsyncConnection = await psycopg.AsyncConnection.connect(
        **params, autocommit=True
    )
    try:
        register_jsonb_loader(conn)
        client = AsyncAbsurd(
            conn,
            queue_name=queue,
            hooks=AbsurdHooks(
                before_spawn=log_before_spawn,
                wrap_task_execution=log_task_execution,
            ),
        )
        client._registry = LazyTaskRegistry(queue, backend)  # noqa: SLF001 -- SDK has no public fallback-resolver hook; install lazy import_string resolution
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
        # fetch_run_outcome's ORM read runs on asgiref's thread-sensitive executor —
        # one process-wide thread nothing else tears down — so its Django session
        # would outlive the whole worker run, and one session fails DROP DATABASE
        # (measured: a run without --reuse-db dies with "database ... is being
        # accessed by other users"). Close once per run, here, in that same thread
        # (both calls resolve the same executor). The blocking worker reads nothing.
        await sync_to_async(close_old_connections)()


async def adrain_queue(
    database: str,
    client: AsyncAbsurd,
    queue: str,
    options: WorkerOptions,
) -> list[DrainedRun]:
    drained: list[DrainedRun] = []
    while True:
        claimed = await client.claim_tasks(
            options.batch_size or options.concurrency,
            options.claim_timeout,
            options.worker_id or "worker",
        )
        if not claimed:
            break
        drained.extend(
            await asyncio.gather(
                *[
                    execute_claimed_run(
                        database, client, queue, claimed_task, options.claim_timeout
                    )
                    for claimed_task in claimed
                ]
            )
        )
    return drained


async def execute_claimed_run(
    database: str,
    client: AsyncAbsurd,
    queue: str,
    claimed: ClaimedTask,
    claim_timeout: int,
) -> DrainedRun:
    await client._execute_task(claimed, claim_timeout)  # noqa: SLF001 -- SDK exposes no public counted dispatch; mirrors work_batch
    state, result, failure = await fetch_run_outcome(database, queue, claimed["run_id"])
    return DrainedRun(
        run_id=t.cast("uuid.UUID", claimed["run_id"]),
        task_id=t.cast("uuid.UUID", claimed["task_id"]),
        task_name=claimed["task_name"],
        params=claimed["params"],
        attempt=claimed["attempt"],
        state=state,
        result=result,
        failure=failure,
    )


async def fetch_run_outcome(
    database: str, queue: str, run_id: str
) -> tuple[str, t.Any, t.Any]:
    """Read one run's current ``state``/``result``/``failure_reason``.

    No public SDK accessor keys a read by ``run_id`` (only ``task_id``, which
    collapses a retry's several runs), so read the run row through the same
    per-queue dynamic model the other reads use — no SQL of its own. ORM means
    Django's connection via a ``sync_to_async`` hop; only drains pay it, and
    next to executing the task it is noise. The row is committed by the time
    ``_execute_task`` returns (the SDK writes on autocommit). ``aget``, not
    ``afirst``: a missing just-executed run is a broken invariant that should raise.
    """
    runs_spec = next(s for s in admin_views.ADMIN_ENTITY_SPECS if s.name == "runs")
    run_model: type[t.Any] = admin_views.build_queue_table_model(runs_spec, queue)
    run: RunModel = (
        await run_model.objects.using(database).defer("available_at").aget(pk=run_id)
    )
    return run.state, run.result, run.failure_reason


def build_running_task_result(
    task: "Task[t.Any, t.Any]",
    ctx: AsyncTaskContext,
    args: t.Sequence[t.Any],
    kwargs: dict[str, t.Any],
    *,
    backend: AbsurdBackend,
    queue: str,
) -> "TaskResult[t.Any, t.Any]":
    # ClaimedTask carries no queue, and the re-imported task holds its
    # definition-time one, so only the worker's own queue is authoritative here. The
    # alias moves with it: rebinding re-runs Django's Task.__post_init__, which
    # validates the queue against whatever backend the DEFINITION named — an alias a
    # beat-scheduled task need not even have configured — and the TaskResult below
    # reports this worker's alias anyway.
    if (task.queue_name, task.backend) != (queue, backend.alias):
        task = task.using(queue_name=queue, backend=backend.alias)
    attempt = read_sdk_claimed_task(ctx)["attempt"]
    # One instant for both fields, as Django's own backend does: this handler entry IS
    # the attempt, so "when it started" and "when it was last attempted" cannot differ.
    started_at = timezone.now()
    task_result: TaskResult[t.Any, t.Any] = TaskResult(
        task=task,
        id=f"{queue}:{ctx.task_id}",
        status=TaskResultStatus.RUNNING,
        enqueued_at=None,
        started_at=started_at,
        finished_at=None,
        last_attempted_at=started_at,
        args=list(args),
        kwargs=dict(kwargs),
        backend=backend.alias,
        errors=[],
        worker_ids=["absurd"] * attempt,
    )
    return task_result


def build_handler(
    task: "Task[t.Any, t.Any]",
    *,
    backend: AbsurdBackend,
    queue: str,
) -> t.Callable[[TaskParams, AsyncTaskContext], t.Awaitable[JsonValue]]:
    async def handler(params: TaskParams, ctx: AsyncTaskContext) -> JsonValue:
        WORKER_LOOP.set(asyncio.get_running_loop())
        args = params.get("args", [])
        kwargs = params.get("kwargs", {})
        attempt = read_sdk_claimed_task(ctx)["attempt"]
        task_result = build_running_task_result(
            task, ctx, args, kwargs, backend=backend, queue=queue
        )
        dispatch.send_task_signal(task_started, type(backend), task_result)
        try:
            if task.takes_context:
                ctx_: TaskContext[t.Any, t.Any] = TaskContext(task_result=task_result)
            if inspect.iscoroutinefunction(task.func):
                if task.takes_context:
                    result = t.cast("JsonValue", await task.func(ctx_, *args, **kwargs))
                else:
                    result = t.cast("JsonValue", await task.func(*args, **kwargs))
            else:

                def call_sync() -> JsonValue:
                    close_old_connections()
                    try:
                        if task.takes_context:
                            return t.cast("JsonValue", task.func(ctx_, *args, **kwargs))
                        return t.cast("JsonValue", task.func(*args, **kwargs))
                    finally:
                        close_old_connections()

                result = await asyncio.to_thread(call_sync)
        except (SuspendTask, CancelledTask, FailedTask):
            # Absurd decided this run's fate itself, so no task_finished is sent: these
            # are not endings Django's task signals describe. The arm exists to keep
            # them out of the ``except Exception`` arm below, which WOULD report one.
            raise
        except Exception as exc:
            send_finished_if_terminal(ctx, attempt, exc, task_result, backend=backend)
            raise
        else:
            object.__setattr__(task_result, "status", TaskResultStatus.SUCCESSFUL)
            object.__setattr__(task_result, "finished_at", timezone.now())
            # normalize_json, as Django's own backend applies to a raw return value
            # before storing it — otherwise a receiver sees the tuple a task returned
            # while ``get_result().return_value`` reads back the list Postgres holds.
            # The value handed BACK to the SDK stays raw; only the payload normalizes.
            object.__setattr__(task_result, "_return_value", normalize_json(result))
            dispatch.send_task_signal(task_finished, type(backend), task_result)
            return result

    return handler


def send_finished_if_terminal(
    ctx: AsyncTaskContext,
    attempt: int,
    exc: Exception,
    task_result: "TaskResult[t.Any, t.Any]",
    *,
    backend: AbsurdBackend,
) -> None:
    """Send ``task_finished`` for a failed run, but only once it is provably terminal.

    A failed non-final attempt is READY again, not FAILED — Absurd itself will retry
    it, so sending here would be a false completion signal.

    Sent from inside the handler's ``except`` arm, so ``sys.exc_info()`` already holds
    ``exc`` — the traceback Django's ``log_task_finished`` attaches to its ERROR line is
    the task's own, matching the ``TaskError`` payload with nothing to arrange.
    """
    if not is_terminal_attempt(attempt, read_sdk_claimed_task(ctx)["max_attempts"]):
        return
    mark_task_result_failed(task_result, exc)
    dispatch.send_task_signal(task_finished, type(backend), task_result)


def is_terminal_attempt(attempt: int, max_attempts: int | None) -> bool:
    """Whether this attempt is the last one Absurd will make.

    ``fail_run`` retries when ``v_max_attempts is null or v_next_attempt <=
    v_max_attempts``, so a NULL ``max_attempts`` means retry forever: no attempt is
    ever terminal, and ``task_finished`` must stay unsent.
    """
    return max_attempts is not None and attempt >= max_attempts


def mark_task_result_failed(
    task_result: "TaskResult[t.Any, t.Any]", exc: Exception
) -> None:
    """Mutate ``task_result`` into its terminal FAILED shape, exactly as
    ``ImmediateBackend._execute_task`` does for its own failure branch."""
    object.__setattr__(task_result, "status", TaskResultStatus.FAILED)
    object.__setattr__(task_result, "finished_at", timezone.now())
    task_result.errors.append(
        TaskError(
            exception_class_path=f"{type(exc).__module__}.{type(exc).__qualname__}",
            traceback="".join(format_exception(exc)),
        )
    )


async def run_blocking_worker(
    client: AsyncAbsurd,
    options: WorkerOptions,
    *,
    stop: asyncio.Event | None = None,
    on_stop_requested: t.Callable[[], None] | None = None,
) -> None:
    stop_signal = stop if stop is not None else asyncio.Event()
    loop = asyncio.get_running_loop()

    def handle_stop() -> None:
        stop_signal.set()
        logger.info("worker stop requested: finishing in-flight tasks")
        if on_stop_requested is not None:
            on_stop_requested()

    loop.add_signal_handler(signal.SIGINT, handle_stop)
    loop.add_signal_handler(signal.SIGTERM, handle_stop)
    # Worker id as the SDK's own start_worker derives it, since we no longer call it.
    worker_id = options.worker_id or f"{socket.gethostname()}:{os.getpid()}"
    # Both loops below are ported from
    # https://github.com/earendil-works/absurd/pull/137, because the SDK's
    # AsyncAbsurd.start_worker awaits its whole claimed batch before claiming again —
    # one slow task then idles every other slot. Delete them in favour of
    # client.start_worker once a released SDK carries that fix.
    try:
        if options.concurrency <= 1:
            await run_one_slot_at_a_time(client, options, stop_signal, worker_id)
        else:
            await refill_slots_until_stopped(client, options, stop_signal, worker_id)
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)


async def run_one_slot_at_a_time(
    client: AsyncAbsurd,
    options: WorkerOptions,
    stop: asyncio.Event,
    worker_id: str,
) -> None:
    """Claim a whole batch and execute it sequentially — the single-slot fast path.

    Straight from the SDK's SYNC worker, which has always had it. Without this branch
    the windowed loop's cap of ``min(batch_size, free capacity)`` would silently turn
    ``--batch-size N --concurrency 1`` from one round trip into N.
    """
    batch_size = options.batch_size or options.concurrency
    while not stop.is_set():
        claimed = await client.claim_tasks(batch_size, options.claim_timeout, worker_id)
        if not claimed:
            await wait_for_stop_or_timeout(stop, options.poll_interval)
            continue
        for claimed_task in claimed:
            await client._execute_task(claimed_task, options.claim_timeout)  # noqa: SLF001 -- SDK exposes no public counted dispatch; mirrors work_batch


async def refill_slots_until_stopped(
    client: AsyncAbsurd,
    options: WorkerOptions,
    stop: asyncio.Event,
    worker_id: str,
) -> None:
    """Keep ``concurrency`` slots busy, claiming again the moment one frees.

    ONE ``executing`` window across iterations is the whole point: rebuilding it per
    batch is what makes the SDK's async worker wait on its slowest claimed task before
    claiming anything else.
    """
    effective_batch_size = options.batch_size or options.concurrency
    executing: set[asyncio.Task[None]] = set()
    try:
        while not stop.is_set():
            reap_finished_slots(executing)
            capacity = options.concurrency - len(executing)
            if capacity <= 0:
                await wait_for_slot_progress(executing, stop, options.poll_interval)
                continue
            claimed = await client.claim_tasks(
                min(effective_batch_size, capacity), options.claim_timeout, worker_id
            )
            if not claimed:
                await wait_for_slot_progress(executing, stop, options.poll_interval)
                continue
            for claimed_task in claimed:
                executing.add(
                    asyncio.create_task(
                        client._execute_task(claimed_task, options.claim_timeout)  # noqa: SLF001 -- SDK exposes no public counted dispatch; mirrors work_batch
                    )
                )
    except asyncio.CancelledError:
        # The connection every one of these runs writes through is about to close, so
        # none of them may outlive the loop. The sync worker gets this free from
        # ThreadPoolExecutor.__exit__; the async loop has to do it by hand.
        await cancel_and_drain_slots(executing)
        raise
    finally:
        # A stop means stop CLAIMING: the window it already owns runs to completion.
        await finish_remaining_slots(executing)


async def cancel_and_drain_slots(executing: set[asyncio.Task[None]]) -> None:
    """Cancel the whole in-flight window and wait for it to unwind.

    Awaiting is the point: ``cancel()`` only requests, so returning without it leaves
    handlers still on the loop. Emptying ``executing`` leaves nothing for the caller's
    ``finally`` to await a second time — that pass is the graceful one, and it would
    re-raise a slot's own ``CancelledError`` in place of the loop's.
    """
    for entry in executing:
        entry.cancel()
    await asyncio.gather(*executing, return_exceptions=True)
    executing.clear()


async def finish_remaining_slots(executing: set[asyncio.Task[None]]) -> None:
    """Await the whole in-flight window, then surface the first machinery failure.

    Two steps rather than one bare ``gather``: a gather raises the instant one slot
    fails, abandoning its siblings mid-execution against a connection
    ``aworker_client`` is about to close. ``asyncio.wait`` settles every slot first, so
    the gather after it only re-raises — and it marks the other slots' exceptions
    retrieved, so none resurfaces as "Task exception was never retrieved". What
    surfaces here is never a task failing: ``_execute_task`` records a handler's own
    failure against the run and returns normally.
    """
    if not executing:
        return
    await asyncio.wait(executing)
    await asyncio.gather(*executing)


def reap_finished_slots(executing: set[asyncio.Task[None]]) -> None:
    """Free every finished slot, re-raising whatever its task raised.

    ``_execute_task`` records a handler's own failure against the run and returns, so
    an exception surfacing here is the worker's own machinery breaking — never a task
    failing — and must not be swallowed into a worker that keeps polling.
    """
    for finished in [entry for entry in executing if entry.done()]:
        executing.discard(finished)
        finished.result()


async def wait_for_slot_progress(
    executing: set[asyncio.Task[None]], stop: asyncio.Event, poll_interval: float
) -> None:
    """Wait until a slot frees, or ``poll_interval`` passes, whichever comes first.

    ``asyncio.wait`` only observes; the loop head reaps what it reports. It needs no
    stop of its own: waking early on one would only reach a ``finally`` that awaits
    the very slots being waited on here. An EMPTY window has nothing to wait on, so
    that arm is a plain idle and races the stop like every other idle does.
    """
    if not executing:
        await wait_for_stop_or_timeout(stop, poll_interval)
        return
    await asyncio.wait(
        executing, timeout=poll_interval, return_when=asyncio.FIRST_COMPLETED
    )


async def wait_for_stop_or_timeout(stop: asyncio.Event, poll_interval: float) -> None:
    """Idle for ``poll_interval``, returning the moment ``stop`` is set.

    Sleeping blind instead would make an idle worker sit out the rest of its poll
    interval before noticing a SIGTERM — and idle is exactly when a stop tends to
    arrive, so that is the whole of the delay a shutdown shows.
    """
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), poll_interval)


async def run_worker_with_beat(
    client: AsyncAbsurd,
    options: WorkerOptions,
    backend: AbsurdBackend,
    *,
    on_stop_requested: t.Callable[[], None] | None = None,
) -> None:
    stop = asyncio.Event()
    beat_stop = threading.Event()
    beat_thread = threading.Thread(
        target=run_beat, args=(backend,), kwargs={"stop": beat_stop}, daemon=True
    )
    beat_thread.start()
    try:
        await run_blocking_worker(
            client, options, stop=stop, on_stop_requested=on_stop_requested
        )
    finally:
        beat_stop.set()
        await asyncio.get_running_loop().run_in_executor(None, beat_thread.join, 5)
