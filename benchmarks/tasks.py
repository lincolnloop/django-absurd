import asyncio
import time

from django.tasks import task

from django_absurd import get_absurd_context


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
