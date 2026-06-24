from asyncio import sleep as asleep

from django.tasks import task

from tests.models import Payload


@task
async def aecho(value):
    return value


@task
async def aboom():
    msg = "aboom"
    raise ValueError(msg)


@task(takes_context=True)
async def areport_attempt(context):
    return context.attempt


@task
async def acreate_payload(data):
    obj = await Payload.objects.acreate(data=data)
    return obj.pk


@task
async def aread_payload(pk):
    # async QUERY: read a row back via Django async ORM, return its jsonb
    obj = await Payload.objects.aget(pk=pk)
    return obj.data


@task
async def asleeper(seconds):
    await asleep(seconds)
    return "slept"
