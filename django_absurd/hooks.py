"""Hooks passed to both Absurd clients, where Absurd's own lifecycle becomes visible.

Every hook body here is contained: the SDK runs hooks inside the same try/except that
wraps a task's own handler, so an exception escaping one of ours is indistinguishable
from the task itself failing — it consumes an attempt and lands in ``failure_reason``,
with nothing reaching stderr. Catch, log, and continue on every path.

Contained means the LOGGING, never the run. ``log_task_execution`` wraps the handler
itself, so the exceptions that reach it — the task's own, and the SDK's ``SuspendTask``/
``CancelledTask``/``FailedTask`` — are how a run ends and travel on untouched.
"""

import logging
import time
import typing as t

from absurd_sdk import (
    AbsurdHooks,
    AsyncTaskContext,
    CancelledTask,
    ClaimedTask,
    FailedTask,
    JsonValue,
    SpawnOptions,
    SuspendTask,
    TaskContext,
)

logger = logging.getLogger(__name__)


def build_absurd_hooks() -> AbsurdHooks:
    """Build the hooks dict passed to both the sync and async Absurd clients."""
    return {
        "before_spawn": log_before_spawn,
        "wrap_task_execution": log_task_execution,
    }


def log_before_spawn(
    task_name: str, params: JsonValue, options: SpawnOptions
) -> SpawnOptions:
    """Log a spawn, then return ``options`` unchanged.

    The SDK assigns this function's return value straight back into its own
    spawn_options (``spawn_options = before_spawn(task_name, params, spawn_options)``),
    so ``options`` must come back exactly as given on every path, including the one
    where logging itself raises — a hook that returns ``None`` breaks every spawn in
    the project.
    """
    try:
        logger.debug("spawn requested: %s", describe_spawn(task_name, options))
    except Exception:
        logger.exception("failed to log spawn: name=%s", task_name)
    return options


def describe_spawn(task_name: str, options: SpawnOptions) -> str:
    # The Absurd-side detail Django's own enqueue line omits: queue, retry ceiling,
    # and dedup key. max_attempts/idempotency_key are absent from options entirely
    # when the caller didn't set them, so they're reported only when present.
    detail = f"name={task_name} queue={options.get('queue')}"
    if "max_attempts" in options:
        detail += f" max_attempts={options['max_attempts']}"
    if "idempotency_key" in options:
        detail += f" idempotency_key={options['idempotency_key']}"
    return detail


async def log_task_execution(
    ctx: TaskContext | AsyncTaskContext, execute: t.Callable[[], t.Awaitable[t.Any]]
) -> t.Any:
    """Log one run's lifecycle around ``execute()``, returning what the run returned.

    One seam covers every handler the SDK dispatches, the ``:run_after`` deferral
    wrapper included, so no handler carries lifecycle logging of its own.

    Each arm re-raises what it caught, unchanged. ``SuspendTask``/``CancelledTask``/
    ``FailedTask`` are how Absurd itself ends a run and the SDK recognises its own
    classes by identity, so swallowing one would change execution rather than logging;
    the task's own exception is what the SDK turns into a failed attempt. Only the
    logging is contained, and only inside ``report_run_event``.
    """
    report_run_event(logging.INFO, "task started", ctx)
    started = time.monotonic()
    try:
        result = await execute()
    except SuspendTask:
        report_run_event(logging.INFO, "task suspended", ctx, started=started)
        raise
    except CancelledTask:
        report_run_event(logging.WARNING, "task cancelled", ctx, started=started)
        raise
    except FailedTask:
        report_run_event(
            logging.WARNING, "run already failed elsewhere", ctx, started=started
        )
        raise
    except Exception:
        report_run_event(
            logging.ERROR, "task failed", ctx, started=started, exc_info=True
        )
        raise
    report_run_event(logging.INFO, "task completed", ctx, started=started)
    return result


def report_run_event(
    level: int,
    event: str,
    ctx: TaskContext | AsyncTaskContext,
    *,
    started: float | None = None,
    exc_info: bool = False,
) -> None:
    """Emit one lifecycle line, and log rather than raise when emitting it fails.

    ``describe_run`` renders values the SDK supplied, so it is called INSIDE the try: a
    value that raises while rendering must not leave this hook, or the SDK reads it as
    the task failing. The fallback line interpolates ``event`` alone — a literal from
    the call site — so the same fault cannot repeat inside it.
    """
    try:
        logger.log(
            level, "%s: %s", event, describe_run(ctx, started), exc_info=exc_info
        )
    except Exception:
        logger.exception("failed to log a run lifecycle event: event=%s", event)


def describe_run(ctx: TaskContext | AsyncTaskContext, started: float | None) -> str:
    # attempt and max_attempts together, because "attempt 2 of 5 failed" and "the final
    # attempt failed" read differently and Django's own task log cannot tell them apart.
    claimed = read_sdk_claimed_task(ctx)
    detail = (
        f"name={claimed['task_name']} task_id={ctx.task_id}"
        f" attempt={claimed['attempt']} max_attempts={claimed['max_attempts']}"
    )
    if started is not None:
        detail += f" duration={time.monotonic() - started:.3f}s"
    return detail


def read_sdk_claimed_task(ctx: TaskContext | AsyncTaskContext) -> ClaimedTask:
    claimed: ClaimedTask = ctx._task  # noqa: SLF001 -- SDK TaskContext has no public accessor for the row it was claimed from
    return claimed


def read_sdk_attempt(ctx: TaskContext | AsyncTaskContext) -> int:
    return read_sdk_claimed_task(ctx)["attempt"]


def read_sdk_max_attempts(ctx: TaskContext | AsyncTaskContext) -> int | None:
    return read_sdk_claimed_task(ctx)["max_attempts"]
