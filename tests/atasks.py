import time
import typing as t
from asyncio import sleep as asleep

from django.tasks import TaskContext, task

from tests.models import Payload

if t.TYPE_CHECKING:
    from django_absurd.context import AsyncDurableContext

DURABLE_STEP_CALLS: dict[str, int] = {"n": 0}


@task(takes_context=True)  # type: ignore[arg-type]  # django-stubs types the ctx param as base TaskContext; the worker passes an AsyncDurableContext to coroutine tasks
async def astep_echo(context: "AsyncDurableContext", value: str) -> str:
    async def compute() -> str:
        return value

    return await context.step("echo", compute)


@task(takes_context=True)  # type: ignore[arg-type]  # django-stubs types the ctx param as base TaskContext; the worker passes an AsyncDurableContext to coroutine tasks
async def aheaders_tenant(context: "AsyncDurableContext") -> str | None:
    return context.headers.get("tenant")


@task(takes_context=True)  # type: ignore[arg-type]  # django-stubs types the ctx param as base TaskContext; the worker passes an AsyncDurableContext to coroutine tasks
async def aheartbeat_then_return(context: "AsyncDurableContext", value: str) -> str:
    await context.heartbeat()
    return value


@task
async def aecho(value: t.Any) -> t.Any:
    return value


@task
async def aboom() -> t.Never:
    msg = "aboom"
    raise ValueError(msg)


@task(takes_context=True)
async def areport_attempt(context: "TaskContext[t.Any, t.Any]") -> int:
    return context.attempt


@task
async def acreate_payload(data: t.Any) -> int:
    obj = await Payload.objects.acreate(data=data)
    return obj.pk


@task
async def aread_payload(pk: int) -> t.Any:
    # async QUERY: read a row back via Django async ORM, return its jsonb
    obj = await Payload.objects.aget(pk=pk)
    return obj.data


@task
async def asleeper(seconds: float) -> str:
    await asleep(seconds)
    return "slept"


@task(takes_context=True)  # type: ignore[arg-type]  # django-stubs types the ctx param as base TaskContext; the worker passes an AsyncDurableContext to coroutine tasks
async def asleep_for_once(context: "AsyncDurableContext", key: str) -> int:
    async def bump() -> int:
        DURABLE_STEP_CALLS["n"] += 1
        return DURABLE_STEP_CALLS["n"]

    n = await context.step("bump", bump)
    await context.sleep_for("nap", 1.5)
    return n


@task(takes_context=True)  # type: ignore[arg-type]  # django-stubs types the ctx param as base TaskContext; the worker passes an AsyncDurableContext to coroutine tasks
async def asleep_until_once(context: "AsyncDurableContext", key: str) -> str:
    await context.sleep_until("nap", time.time() + 1.5)
    return "woke"
