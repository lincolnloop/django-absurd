"""Durable task context accessors for Absurd tasks.

Two concrete-typed accessors return the live Absurd runtime context, orthogonal to
Django's ``TaskContext``. ``aget_absurd_context()`` (async tasks) returns the SDK's own
``AsyncTaskContext`` (pure passthrough — ``await context.step(...)``);
``get_absurd_context()`` (sync tasks) returns an ``AbsurdTaskContext`` bridge that
mirrors the SDK sync signatures and hops each op onto the worker loop.
"""

import asyncio
import contextvars
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
        absurd_ctx=t.cast("AsyncTaskContext", absurd_ctx), loop=WORKER_LOOP.get()
    )


def aget_absurd_context() -> "AsyncTaskContext":
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
    return t.cast("AsyncTaskContext", absurd_ctx)


@dataclass(frozen=True, slots=True)
class AbsurdTaskContext:
    """Sync bridge over the live async Absurd context.

    Sync ``def`` tasks run in the worker's threadpool executor, so each durable op
    hands its coroutine to the loop via ``run_coroutine_threadsafe`` and blocks on
    the result. The user's step ``fn`` runs in this executor thread (between the
    ``begin_step``/``complete_step`` bridges), never on the loop.
    """

    absurd_ctx: AsyncTaskContext
    loop: asyncio.AbstractEventLoop

    @property
    def headers(self) -> "Mapping[str, absurd_sdk.JsonValue]":
        headers: Mapping[str, absurd_sdk.JsonValue] = self.absurd_ctx.headers
        return headers

    def step(self, name: str, fn: "Callable[[], R]") -> R:
        handle = self.run_on_loop(self.absurd_ctx.begin_step(name))
        if handle.done:
            return t.cast("R", handle.state)
        rv = fn()
        return self.run_on_loop(self.absurd_ctx.complete_step(handle, rv))

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
        self.run_on_loop(self.absurd_ctx.heartbeat(seconds))

    def sleep_for(self, step_name: str, duration: float) -> None:
        self.run_on_loop(self.absurd_ctx.sleep_for(step_name, duration))

    def sleep_until(self, step_name: str, wake_at: "dt.datetime | int | float") -> None:
        self.run_on_loop(self.absurd_ctx.sleep_until(step_name, wake_at))

    def await_event(
        self, event_name: str, step_name: str | None = None, timeout: int | None = None
    ) -> "JsonValue":
        return self.run_on_loop(
            self.absurd_ctx.await_event(event_name, step_name, timeout)
        )

    def emit_event(self, event_name: str, payload: "JsonValue | None" = None) -> None:
        self.run_on_loop(self.absurd_ctx.emit_event(event_name, payload))

    def run_on_loop(self, coro: "Coroutine[t.Any, t.Any, R]") -> R:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=BRIDGE_TIMEOUT)
