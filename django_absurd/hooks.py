"""Hooks handed to the Absurd clients, where Absurd's own lifecycle becomes visible.

The async client takes both; the sync client takes only ``log_before_spawn``, because
its ``_execute_task`` never awaits a hook's return value.

Every hook body is contained, for a different reason each:

- ``wrap_task_execution`` runs inside the same try/except that wraps a task's own
  handler, so an exception escaping ours consumes an attempt and lands in
  ``failure_reason`` as though the task failed, with nothing reaching stderr.
- ``log_before_spawn`` runs inside ``spawn()``, outside any try, so an exception
  escaping it surfaces loudly at the caller's ``enqueue()`` — breaking every enqueue in
  the project rather than one run.
"""

import contextlib
import logging
import time
import typing as t

import absurd_sdk

if t.TYPE_CHECKING:
    from absurd_sdk import (
        AsyncTaskContext,
        ClaimedTask,
        JsonValue,
        SpawnOptions,
        TaskContext,
    )

logger = logging.getLogger(__name__)


def log_before_spawn(
    task_name: str, params: "JsonValue", options: "SpawnOptions"
) -> "SpawnOptions":
    """Log a spawn, then return ``options`` unchanged.

    The SDK assigns this return value straight back into its own spawn_options, so a
    hook returning ``None`` breaks every spawn in the project — including on the path
    where logging itself raises.
    """
    try:
        # max_attempts/idempotency_key are absent from options entirely when the caller
        # didn't set them, so they're reported only when present.
        detail = f'name="{task_name}" queue="{options.get("queue")}"'
        if "max_attempts" in options:
            detail += f" max_attempts={options['max_attempts']}"
        if "idempotency_key" in options:
            detail += f' idempotency_key="{options["idempotency_key"]}"'
        logger.debug("spawn requested: %s", detail)
    except Exception:
        logger.exception('failed to log spawn: name="%s"', task_name)
    return options


async def log_task_execution(
    ctx: "TaskContext | AsyncTaskContext",
    execute: t.Callable[[], t.Awaitable[t.Any]],
) -> t.Any:
    """Log one run's lifecycle around ``execute()``, returning what the run returned.

    One seam covers every handler the SDK dispatches, the ``:run_after`` deferral
    wrapper included.

    Each arm re-raises what it caught, unchanged: ``SuspendTask``/``CancelledTask``/
    ``FailedTask`` are how Absurd itself ends a run and the SDK recognises its own
    classes by identity, so swallowing one would change execution rather than logging.
    Only the logging is contained, inside ``report_run_event``.
    """
    report_run_event(logging.INFO, "task started", ctx)
    started = time.monotonic()
    try:
        result = await execute()
    except absurd_sdk.SuspendTask:
        report_run_event(logging.INFO, "task suspended", ctx, started=started)
        raise
    except absurd_sdk.CancelledTask:
        report_run_event(logging.WARNING, "task cancelled", ctx, started=started)
        raise
    except absurd_sdk.FailedTask:
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
    ctx: "TaskContext | AsyncTaskContext",
    *,
    started: float | None = None,
    exc_info: bool = False,
) -> None:
    """Emit one lifecycle line, and log rather than raise when emitting it fails.

    The message is built INSIDE the try: it renders values the SDK supplied, and a value
    that raises while rendering must not leave this hook. The fallback interpolates
    ``event`` alone — a literal from the call site — so no *rendering* fault can repeat
    in it. A fault in the logging machinery itself can: a filter or handler that raises
    on record content sees the fallback's record too, and that second exception would
    escape into the SDK. Hence the inner guard, where there is nowhere left to report.
    """
    try:
        # attempt and max_attempts together, because "attempt 2 of 5 failed" and "the
        # final attempt failed" read differently and Django's own log cannot tell them
        # apart.
        claimed = read_sdk_claimed_task(ctx)
        detail = (
            f'name="{claimed["task_name"]}" task_id="{ctx.task_id}"'
            f" attempt={claimed['attempt']} max_attempts={claimed['max_attempts']}"
        )
        if started is not None:
            detail += f" duration={time.monotonic() - started:.3f}s"
        logger.log(level, "%s: %s", event, detail, exc_info=exc_info)
    except Exception:
        # Last resort: reporting the fault is itself what failed, and raising from here
        # would reach the SDK and consume the attempt.
        with contextlib.suppress(Exception):
            logger.exception('failed to log a run lifecycle event: event="%s"', event)


def read_sdk_claimed_task(ctx: "TaskContext | AsyncTaskContext") -> "ClaimedTask":
    claimed: ClaimedTask = ctx._task  # noqa: SLF001 -- SDK TaskContext has no public accessor for the row it was claimed from
    return claimed
