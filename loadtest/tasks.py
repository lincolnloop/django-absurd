import asyncio
import os
import time
import typing as t

from django.tasks import TaskContext, task
from django.utils import timezone

from django_absurd import aget_absurd_context, get_absurd_context
from loadtest.models import ExecutionLog, OccupancyLog


@task(queue_name="bulk", takes_context=True)
def burn_sync(context: "TaskContext[t.Any, t.Any]", payload: dict[str, int]) -> int:
    ExecutionLog.objects.create(task_id=take_task_uuid(context), pid=os.getpid())
    return payload["n"]


@task(queue_name="bulk", takes_context=True)
async def burn_async(
    context: "TaskContext[t.Any, t.Any]", payload: dict[str, int]
) -> int:
    await ExecutionLog.objects.acreate(task_id=take_task_uuid(context), pid=os.getpid())
    return payload["n"]


def take_task_uuid(context: "TaskContext[t.Any, t.Any]") -> str:
    """Return the bare task UUID from a running task's context.

    ``TaskResult.id`` is the queue-prefixed natural key (``<queue>:<uuid>``), matching
    what ``enqueue()`` hands back — so it cannot go straight into a ``UUIDField``.
    Splitting on the last colon rather than the first keeps a queue name containing one
    from silently truncating the uuid.
    """
    return context.task_result.id.rsplit(":", 1)[-1]


@task(queue_name="bulk")
def nap_sync(payload: dict[str, float]) -> None:
    """Enter a durable sleep long enough to outlast the run that enqueued it.

    Writes no ``ExecutionLog`` row on purpose: ``load_sleepers`` counts that table to
    know when its quick tasks have drained, and a sleeper's row would be indistinguable
    from a quick task's. Whether this task is holding a worker slot is read from its
    run's ``state``, which is the honest answer anyway.
    """
    context = get_absurd_context()
    context.sleep_for("nap", payload["seconds"])


@task(queue_name="bulk")
async def nap_async(payload: dict[str, float]) -> None:
    context = aget_absurd_context()
    await context.sleep_for("nap", payload["seconds"])


@task(queue_name="bulk", takes_context=True)
def toil_sync(context: "TaskContext[t.Any, t.Any]", payload: dict[str, float]) -> None:
    """Hold a worker slot for ``payload["seconds"]`` and record the interval.

    A blocking ``time.sleep``, deliberately, and never ``context.sleep_for``: a durable
    sleep suspends the run and hands the slot back (which is what ``load_sleepers``
    measured), so it cannot stand in for a task that is genuinely slow. A sync task
    body runs in the worker's thread pool, sized to ``--concurrency``, so sleeping here
    occupies one of that worker's slots and nothing else.
    """
    started_at = timezone.now()
    time.sleep(payload["seconds"])
    OccupancyLog.objects.create(
        task_id=take_task_uuid(context),
        pid=os.getpid(),
        started_at=started_at,
        finished_at=timezone.now(),
    )


@task(queue_name="bulk", takes_context=True)
async def toil_async(
    context: "TaskContext[t.Any, t.Any]", payload: dict[str, float]
) -> None:
    """The async twin, sleeping on the loop rather than blocking a pool thread.

    ``asyncio.sleep`` rather than ``time.sleep``: an async task body runs on the
    worker's one event loop, so blocking it would park every slot at once and measure
    the loop instead of a slot. The awaited sleep still holds this task's place in the
    batch the worker is waiting on, which is the occupancy under test.
    """
    started_at = timezone.now()
    await asyncio.sleep(payload["seconds"])
    await OccupancyLog.objects.acreate(
        task_id=take_task_uuid(context),
        pid=os.getpid(),
        started_at=started_at,
        finished_at=timezone.now(),
    )


@task(queue_name="bulk")
def burn_workflow(payload: dict[str, int]) -> None:
    """Touch every durable primitive the admin has an entity for.

    The seeder runs exactly one of these per queue so Checkpoints, Events and Waits
    have non-empty views. It never returns: the awaited event is never emitted, so the
    task suspends and stays that way, which is precisely what leaves a wait row behind.
    """
    context = get_absurd_context()
    context.step("stamp", lambda: payload["n"])
    context.emit_event(f"loadtest.emitted:{payload['n']}")
    context.await_event(f"loadtest.never-emitted:{payload['n']}")
