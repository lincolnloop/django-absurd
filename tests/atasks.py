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
async def asleeper(seconds):
    await asleep(seconds)
    return "slept"
