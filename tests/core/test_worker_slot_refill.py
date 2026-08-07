import asyncio
import contextlib

import pytest
from django.core.management import call_command
from django.tasks import task

from django_absurd.backends import AbsurdBackend, get_absurd_backends
from django_absurd.test import AbsurdTestRuntime
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
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(STARTED["fast-2"].wait(), timeout=5)
                HOLD["gate"].set()
                stop.set()

            await asyncio.gather(
                run_blocking_worker(client, WorkerOptions(concurrency=2), stop=stop),
                release_once_the_third_task_starts(),
            )

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
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        asyncio.gather(*(STARTED[n].wait() for n in STARTED)),
                        timeout=5,
                    )
                stop.set()

            await asyncio.gather(
                run_blocking_worker(
                    client, WorkerOptions(batch_size=3, concurrency=1), stop=stop
                ),
                stop_once_all_three_ran(),
            )

    asyncio.run(drive())

    assert ORDER == ["first", "second", "third"]


def test_stopping_lets_in_flight_work_finish_and_claims_nothing_new() -> None:
    call_command("absurd_sync_queues")
    arm_events("slow", "unclaimed")

    hold_until_released.enqueue("slow")
    record_started.enqueue("unclaimed")

    async def drive() -> None:
        stop = asyncio.Event()
        async with aworker_client(get_default_backend(), "default") as client:

            async def stop_once_the_slow_task_holds_a_slot() -> None:
                await asyncio.wait_for(STARTED["slow"].wait(), timeout=5)
                stop.set()
                HOLD["gate"].set()

            await asyncio.gather(
                run_blocking_worker(client, WorkerOptions(concurrency=1), stop=stop),
                stop_once_the_slow_task_holds_a_slot(),
            )

    asyncio.run(drive())

    assert ORDER == ["slow"]
    assert not STARTED["unclaimed"].is_set()


def test_stopping_at_concurrency_above_one_still_finishes_a_held_slot(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    # The concurrency=1 stop test above takes the other code path entirely, so only
    # this one pins the windowed loop's contract: a stop stops CLAIMING, it never
    # cancels the window already claimed. The release is deliberately late — the stop
    # has to land while the task is still parked on the gate, because a worker that
    # cancels on stop leaves that run un-completed while one that waits does not.
    call_command("absurd_sync_queues")
    arm_events("slow")

    result = hold_until_released.enqueue("slow")

    async def drive() -> None:
        stop = asyncio.Event()
        async with aworker_client(get_default_backend(), "default") as client:

            async def release_once_the_worker_has_stopped_claiming() -> None:
                await asyncio.wait_for(STARTED["slow"].wait(), timeout=5)
                stop.set()
                await asyncio.sleep(0.1)
                HOLD["gate"].set()

            await asyncio.gather(
                run_blocking_worker(
                    client,
                    WorkerOptions(concurrency=2, poll_interval=0.01),
                    stop=stop,
                ),
                release_once_the_worker_has_stopped_claiming(),
            )

    asyncio.run(drive())

    assert dj_absurd.get_result(result.id).state == "completed"


def test_cancelling_the_worker_leaves_no_handler_running_on_the_loop() -> None:
    call_command("absurd_sync_queues")
    arm_events("slow")

    hold_until_released.enqueue("slow")

    async def drive() -> None:
        async with aworker_client(get_default_backend(), "default") as client:
            worker = asyncio.create_task(
                run_blocking_worker(client, WorkerOptions(concurrency=2))
            )
            await asyncio.wait_for(STARTED["slow"].wait(), timeout=5)
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
            leftovers = [
                pending
                for pending in asyncio.all_tasks()
                if pending is not asyncio.current_task() and not pending.done()
            ]
            assert leftovers == []

    asyncio.run(drive())
