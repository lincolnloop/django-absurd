"""Durable task context accessors for Absurd tasks.

Two concrete-typed accessors return the live Absurd runtime context, orthogonal to
Django's ``TaskContext``. ``aget_absurd_context()`` (async tasks) returns an
``AsyncAbsurdTaskContext`` wrapper around the SDK's own ``AsyncTaskContext``, mirroring
its signatures and logging durable-primitive events (step replay, step completion,
sleep suspended, sleep resumed);
``get_absurd_context()`` (sync tasks) returns an ``AbsurdTaskContext`` bridge that
mirrors the SDK sync signatures and hops each op onto the worker loop.
"""

import asyncio
import contextvars
import logging
import time
import typing as t
from dataclasses import dataclass

import absurd_sdk
from absurd_sdk import AsyncTaskContext

if t.TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Callable, Coroutine, Mapping

    from absurd_sdk import JsonValue

R = t.TypeVar("R")

BRIDGE_TIMEOUT = 300.0

WORKER_LOOP: "contextvars.ContextVar[asyncio.AbstractEventLoop]" = (
    contextvars.ContextVar("django_absurd_worker_loop")
)

logger = logging.getLogger(__name__)


def get_absurd_context() -> "AbsurdTaskContext":
    """Return the live Absurd context for a running SYNC task.

    Wraps the live async context in the ``AbsurdTaskContext`` sync bridge over the
    stashed worker loop. Raises outside a running Absurd task.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # not on the loop → sync task, correct usage
    else:
        msg = (
            "get_absurd_context() is for sync tasks;"
            " use aget_absurd_context() in async tasks"
        )
        raise RuntimeError(msg)
    absurd_ctx = absurd_sdk.get_current_context()
    if absurd_ctx is None:
        msg = "get_absurd_context() must be called inside a running Absurd task"
        raise RuntimeError(msg)
    return AbsurdTaskContext(
        async_ctx=AsyncAbsurdTaskContext(
            absurd_ctx=t.cast("AsyncTaskContext", absurd_ctx)
        ),
        loop=WORKER_LOOP.get(),
    )


def aget_absurd_context() -> "AsyncAbsurdTaskContext":
    """Return the live Absurd context for a running ASYNC task.

    Raises outside a running Absurd task.
    """
    absurd_ctx = absurd_sdk.get_current_context()
    if absurd_ctx is None:
        msg = "aget_absurd_context() must be called inside a running Absurd task"
        raise RuntimeError(msg)
    # Our worker is always AsyncAbsurd, so the live ctx is always AsyncTaskContext; the
    # SDK types get_current_context() as TaskContext | AsyncTaskContext | None only
    # because it also supports a sync worker we don't run.
    return AsyncAbsurdTaskContext(absurd_ctx=t.cast("AsyncTaskContext", absurd_ctx))


@dataclass(frozen=True, slots=True)
class AbsurdTaskContext:
    """Sync bridge over the live async Absurd context.

    Sync ``def`` tasks run in the worker's threadpool executor, so each durable op
    hands its coroutine to the loop via ``run_coroutine_threadsafe`` and blocks on
    the result. The user's step ``fn`` runs in this executor thread (between the
    ``begin_step``/``complete_step`` bridges), never on the loop.
    """

    async_ctx: "AsyncAbsurdTaskContext"
    loop: asyncio.AbstractEventLoop

    @property
    def absurd_ctx(self) -> AsyncTaskContext:
        return self.async_ctx.absurd_ctx

    @property
    def headers(self) -> "Mapping[str, absurd_sdk.JsonValue]":
        return self.async_ctx.headers

    def step(self, name: str, fn: "Callable[[], R]") -> R:
        started = time.monotonic()
        handle = self.run_on_loop(self.async_ctx.begin_step(name))
        if handle.done:
            return t.cast("R", handle.state)
        rv = fn()
        result = self.run_on_loop(self.async_ctx.complete_step(handle, rv))
        logger.info(
            describe_step_completed(
                handle.checkpoint_name,
                self.async_ctx.task_id,
                time.monotonic() - started,
            )
        )
        return result

    @t.overload
    def run_step(
        self, name_or_fn: "str | None" = None
    ) -> "Callable[[Callable[[], R]], R]": ...

    @t.overload
    def run_step(self, name_or_fn: "Callable[[], R]") -> R: ...

    def run_step(
        self, name_or_fn: "str | Callable[[], R] | None" = None
    ) -> "R | Callable[[Callable[[], R]], R]":
        if callable(name_or_fn):
            return self.step(name_or_fn.__name__, name_or_fn)

        custom_name = name_or_fn

        def decorator(fn: "Callable[[], R]") -> R:
            return self.step(custom_name or fn.__name__, fn)

        return decorator

    def heartbeat(self, seconds: int | None = None) -> None:
        self.run_on_loop(self.async_ctx.heartbeat(seconds))

    def sleep_for(self, step_name: str, duration: float) -> None:
        self.run_on_loop(self.async_ctx.sleep_for(step_name, duration))

    def sleep_until(self, step_name: str, wake_at: "dt.datetime | int | float") -> None:
        self.run_on_loop(self.async_ctx.sleep_until(step_name, wake_at))

    def await_event(
        self, event_name: str, step_name: str | None = None, timeout: int | None = None
    ) -> "JsonValue":
        return self.run_on_loop(
            self.async_ctx.await_event(event_name, step_name, timeout)
        )

    def emit_event(self, event_name: str, payload: "JsonValue | None" = None) -> None:
        self.run_on_loop(self.async_ctx.emit_event(event_name, payload))

    def run_on_loop(self, coro: "Coroutine[t.Any, t.Any, R]") -> R:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=BRIDGE_TIMEOUT)


@dataclass(frozen=True, slots=True)
class AsyncAbsurdTaskContext:
    """Logging wrapper over the live async Absurd context.

    Mirrors the SDK's ``AsyncTaskContext`` surface so it substitutes transparently for
    it, adding INFO logs around durable steps: a replay (the checkpoint already exists,
    so the user's ``fn`` is skipped) and a completion (``fn`` ran and its result was
    persisted). Sleeps log a suspension (caught via the SDK's ``SuspendTask``, then
    re-raised untouched) and a resumption on the attempt that returns past the
    checkpoint. Events are plain delegations for now; they gain their own log lines
    separately. ``await_task_result`` is deliberately absent — see ``AGENTS.md``'s
    "await_task_result is not provided".
    """

    absurd_ctx: AsyncTaskContext

    @property
    def task_id(self) -> str:
        return self.absurd_ctx.task_id

    @property
    def headers(self) -> "Mapping[str, absurd_sdk.JsonValue]":
        headers: Mapping[str, absurd_sdk.JsonValue] = self.absurd_ctx.headers
        return headers

    async def step(
        self, name: str, fn: "Callable[[], Coroutine[t.Any, t.Any, R]]"
    ) -> R:
        started = time.monotonic()
        handle = await self.begin_step(name)
        if handle.done:
            return t.cast("R", handle.state)
        rv = await fn()
        result = await self.complete_step(handle, rv)
        logger.info(
            describe_step_completed(
                handle.checkpoint_name, self.task_id, time.monotonic() - started
            )
        )
        return result

    async def begin_step(self, name: str) -> "absurd_sdk.StepHandle":
        handle = await self.absurd_ctx.begin_step(name)
        if handle.done:
            logger.info(describe_step(handle.checkpoint_name, self.task_id))
        return handle

    async def complete_step(self, handle: "absurd_sdk.StepHandle", value: R) -> R:
        return await self.absurd_ctx.complete_step(handle, value)

    async def heartbeat(self, seconds: int | None = None) -> None:
        await self.absurd_ctx.heartbeat(seconds)

    async def sleep_for(self, step_name: str, duration: float) -> None:
        try:
            await self.absurd_ctx.sleep_for(step_name, duration)
        except absurd_sdk.SuspendTask:
            logger.info(describe_sleep_for_suspended(step_name, self.task_id, duration))
            raise
        logger.info(describe_sleep_resumed(step_name, self.task_id))

    async def sleep_until(
        self, step_name: str, wake_at: "dt.datetime | int | float"
    ) -> None:
        try:
            await self.absurd_ctx.sleep_until(step_name, wake_at)
        except absurd_sdk.SuspendTask:
            logger.info(
                describe_sleep_until_suspended(step_name, self.task_id, wake_at)
            )
            raise
        logger.info(describe_sleep_resumed(step_name, self.task_id))

    async def await_event(
        self,
        event_name: str,
        step_name: str | None = None,
        timeout: int | None = None,  # noqa: ASYNC109 -- mirrors the SDK signature
    ) -> "JsonValue":
        return await self.absurd_ctx.await_event(event_name, step_name, timeout)

    async def emit_event(
        self, event_name: str, payload: "JsonValue | None" = None
    ) -> None:
        await self.absurd_ctx.emit_event(event_name, payload)


def describe_step(checkpoint_name: str, task_id: str) -> str:
    return f"step replayed: name={checkpoint_name} task_id={task_id}"


def describe_step_completed(checkpoint_name: str, task_id: str, duration: float) -> str:
    return (
        f"step completed: name={checkpoint_name} task_id={task_id}"
        f" duration={duration:.3f}s"
    )


def describe_sleep_for_suspended(step_name: str, task_id: str, duration: float) -> str:
    return f"sleep suspended: step={step_name} task_id={task_id} for={duration}s"


def describe_sleep_until_suspended(
    step_name: str, task_id: str, wake_at: "dt.datetime | int | float"
) -> str:
    return f"sleep suspended: step={step_name} task_id={task_id} until={wake_at}"


def describe_sleep_resumed(step_name: str, task_id: str) -> str:
    return f"sleep resumed: step={step_name} task_id={task_id}"
