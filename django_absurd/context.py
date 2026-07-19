"""Durable task context exposed to ``takes_context=True`` Absurd tasks."""

import asyncio
import typing as t
from dataclasses import dataclass

from django.tasks import TaskContext

if t.TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Awaitable, Callable, Coroutine, Mapping

    _TaskContextBase = TaskContext[t.Any, t.Any]
else:
    _TaskContextBase = TaskContext

R = t.TypeVar("R")

BRIDGE_TIMEOUT = 300.0


@dataclass(frozen=True, slots=True, kw_only=True)
class AsyncAbsurdTaskContext(_TaskContextBase):  # type: ignore[misc]  # django-stubs omits frozen=True on TaskContext; the runtime dataclass IS frozen, so a frozen subclass is correct
    """``TaskContext`` wrapping the live Absurd SDK ctx for async durable tasks."""

    absurd_ctx: t.Any

    @property
    def headers(self) -> "Mapping[str, t.Any]":
        headers: Mapping[str, t.Any] = self.absurd_ctx.headers
        return headers

    async def step(self, name: str, fn: "Callable[[], Awaitable[R]]") -> R:
        result: R = await self.absurd_ctx.step(name, fn)
        return result

    async def heartbeat(self, seconds: int | None = None) -> None:
        await self.absurd_ctx.heartbeat(seconds)

    async def sleep_for(self, step_name: str, duration: float) -> None:
        await self.absurd_ctx.sleep_for(step_name, duration)

    async def sleep_until(
        self, step_name: str, wake_at: "dt.datetime | int | float"
    ) -> None:
        await self.absurd_ctx.sleep_until(step_name, wake_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class AbsurdTaskContext(_TaskContextBase):  # type: ignore[misc]  # django-stubs omits frozen=True on TaskContext; the runtime dataclass IS frozen, so a frozen subclass is correct
    """``TaskContext`` for sync durable tasks, bridging to the worker event loop.

    Sync ``def`` tasks run in the worker's threadpool executor, so each durable
    op hands its coroutine to the loop via ``run_coroutine_threadsafe`` and blocks
    on the result. The user's step ``fn`` runs in this executor thread (between the
    ``begin_step``/``complete_step`` bridges), never on the loop.
    """

    absurd_ctx: t.Any
    loop: asyncio.AbstractEventLoop

    @property
    def headers(self) -> "Mapping[str, t.Any]":
        headers: Mapping[str, t.Any] = self.absurd_ctx.headers
        return headers

    def step(self, name: str, fn: "Callable[[], R]") -> R:
        handle = self.run_on_loop(self.absurd_ctx.begin_step(name))
        if handle.done:
            return t.cast("R", handle.state)
        rv = fn()
        return t.cast("R", self.run_on_loop(self.absurd_ctx.complete_step(handle, rv)))

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

    def run_on_loop(self, coro: "Coroutine[t.Any, t.Any, R]") -> R:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=BRIDGE_TIMEOUT)
