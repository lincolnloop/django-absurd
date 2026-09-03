import asyncio
import time

from django.tasks import task

from django_absurd import get_absurd_context
from workload import models

# Big enough that the insert and the read back move a real row rather than an empty
# one, small enough to stay off Postgres's out-of-line storage.
DURABLE_PAYLOAD = "x" * 512


@task(queue_name="bench")
def noop_sync() -> int:
    return 0


@task(queue_name="bench")
async def noop_async() -> int:
    return 0


@task(queue_name="bench")
def sleep_sync(seconds: float = 0.05) -> int:
    # A blocking sleep on purpose: this workload measures the sync thread-pool bridge,
    # so it must hold a pool thread rather than yield the loop.
    time.sleep(seconds)
    return 0


@task(queue_name="bench")
async def sleep_async(seconds: float = 0.05) -> int:
    # asyncio.sleep on purpose: this workload measures loop concurrency, so it must
    # hold a slot while leaving the loop free.
    await asyncio.sleep(seconds)
    return 0


@task(queue_name="bench")
def run_steps(step_count: int = 4) -> int:
    context = get_absurd_context()
    for index in range(step_count):
        context.step(f"s{index}", report_step_done)
    return step_count


def report_step_done() -> int:
    return 1


@task(queue_name="bench")
def run_durable_work(seconds: float = 2.0, touches: int = 4) -> int:
    """Hold a worker thread for ``seconds``, reading and writing rows as it goes.

    The regime django-absurd is FOR — a durable agent tool call runs for seconds to
    minutes and works on application data — and the one no other workload here reaches:
    a sync body runs on the worker's own thread pool, so ORM work inside it opens that
    thread's own Django connection and holds it until the body returns.

    The row is cleared on the way out. A table that only grows would make every later
    rep of a measurement slower for a reason no column of it records.
    """
    item = models.WorkItem.objects.create(payload=DURABLE_PAYLOAD)
    for touch in range(1, touches + 1):
        time.sleep(seconds / touches)
        item.touches = touch
        # The row describes itself, so each write moves real bytes rather than storing
        # the same ones again, the way a tool call records what it has just done.
        item.payload = f"{item}: {DURABLE_PAYLOAD}"
        item.save(update_fields=["payload", "touches", "updated_at"])
        item.refresh_from_db()
    item.delete()
    return item.touches
