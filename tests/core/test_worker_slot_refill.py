import asyncio

import pytest
from django.core.management import call_command
from django.tasks import task

from django_absurd.backends import AbsurdBackend, get_absurd_backends
from django_absurd.worker import WorkerOptions, aworker_client, run_blocking_worker

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("_isolate_queues"),
]

HOLD: dict[str, asyncio.Event] = {}
STARTED: dict[str, asyncio.Event] = {}
ORDER: list[str] = []


def get_default_backend() -> AbsurdBackend:
    return get_absurd_backends()["default"]


@task(queue_name="default")
async def hold_until_released(name: str) -> None:
    """Occupy one worker slot until the test lets go of it."""
    ORDER.append(name)
    STARTED[name].set()
    await HOLD["gate"].wait()


@task(queue_name="default")
async def record_started(name: str) -> None:
    ORDER.append(name)
    STARTED[name].set()


def arm_events(*names: str) -> None:
    ORDER.clear()
    STARTED.clear()
    HOLD["gate"] = asyncio.Event()
    for name in names:
        STARTED[name] = asyncio.Event()


def test_worker_starts_a_later_task_while_a_slow_one_still_runs() -> None:
    # A worker of C slots must keep claiming while one slot is busy. Enqueue C+1
    # tasks so the extra one cannot ride in the same claim batch as the slow task:
    # that is what forces a second claim, and a worker that joins its whole batch
    # before claiming again never issues it.
    #
    # Deterministic, not timed: the slow task blocks on an Event the test owns, and
    # each task announces its own start on another. The only clock is the wait_for
    # that turns "never started" into a clean assertion failure instead of a hang.
    # Time cannot be frozen here — a live worker loop and a real thread pool are the
    # subject, so dj_absurd.freeze_time would deadlock rather than help.
    call_command("absurd_sync_queues")
    arm_events("fast-1", "fast-2", "slow")

    hold_until_released.enqueue("slow")
    record_started.enqueue("fast-1")
    record_started.enqueue("fast-2")

    async def drive() -> None:
        stop = asyncio.Event()
        async with aworker_client(get_default_backend(), "default") as client:

            async def release_once_the_third_task_starts() -> None:
                try:
                    await asyncio.wait_for(STARTED["fast-2"].wait(), timeout=5)
                finally:
                    HOLD["gate"].set()
                    stop.set()

            outcomes = await asyncio.gather(
                run_blocking_worker(client, WorkerOptions(concurrency=2), stop=stop),
                release_once_the_third_task_starts(),
                return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, BaseException) and not isinstance(
                    outcome, TimeoutError
                ):
                    raise outcome

    asyncio.run(drive())

    assert STARTED["fast-2"].is_set(), (
        "the worker never started the third task while the slow one held a slot: "
        f"started {ORDER}"
    )


def test_one_slot_runs_a_claimed_batch_in_order() -> None:
    call_command("absurd_sync_queues")
    arm_events("first", "second", "third")

    record_started.enqueue("first")
    record_started.enqueue("second")
    record_started.enqueue("third")

    async def drive() -> None:
        stop = asyncio.Event()
        async with aworker_client(get_default_backend(), "default") as client:

            async def stop_once_all_three_ran() -> None:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*(STARTED[n].wait() for n in STARTED)),
                        timeout=5,
                    )
                finally:
                    stop.set()

            outcomes = await asyncio.gather(
                run_blocking_worker(
                    client, WorkerOptions(batch_size=3, concurrency=1), stop=stop
                ),
                stop_once_all_three_ran(),
                return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, BaseException) and not isinstance(
                    outcome, TimeoutError
                ):
                    raise outcome

    asyncio.run(drive())

    assert ORDER == ["first", "second", "third"]
