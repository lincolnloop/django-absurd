import typing as t
from asyncio import sleep as asleep

from django.tasks import TaskContext, task

from tests.models import Payload


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
