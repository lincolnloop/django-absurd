import time
import typing as t
from asyncio import sleep as asleep

from django.tasks import TaskContext, task

from django_absurd import aget_absurd_context
from tests.models import Payload

DURABLE_STEP_CALLS: dict[str, int] = {"n": 0}


@task
async def astep_echo(value: str) -> str:
    async def compute() -> str:
        return value

    return await aget_absurd_context().step("echo", compute)


@task
async def aheaders_tenant() -> str | None:
    tenant = aget_absurd_context().headers.get("tenant")
    return t.cast("str | None", tenant)


@task
async def aheartbeat_then_return(value: str) -> str:
    await aget_absurd_context().heartbeat()
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


@task
async def asleep_for_once(key: str) -> int:
    context = aget_absurd_context()

    async def bump() -> int:
        DURABLE_STEP_CALLS["n"] += 1
        return DURABLE_STEP_CALLS["n"]

    n = await context.step("bump", bump)
    await context.sleep_for("nap", 1.5)
    return n


@task
async def asleep_until_once(key: str) -> str:
    await aget_absurd_context().sleep_until("nap", time.time() + 1.5)
    return "woke"
