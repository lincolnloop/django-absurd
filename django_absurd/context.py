"""Durable task context exposed to ``takes_context=True`` Absurd tasks."""

import typing as t
from dataclasses import dataclass

from django.tasks import TaskContext

if t.TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    _TaskContextBase = TaskContext[t.Any, t.Any]
else:
    _TaskContextBase = TaskContext

R = t.TypeVar("R")


@dataclass(frozen=True, slots=True, kw_only=True)
class AsyncDurableContext(_TaskContextBase):  # type: ignore[misc]  # django-stubs omits frozen=True on TaskContext; the runtime dataclass IS frozen, so a frozen subclass is correct
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
