import asyncio
import contextlib

import pytest
from django.core.management import call_command
from django.tasks import TaskResultStatus, task

from django_absurd import aget_absurd_context
from django_absurd.backends import get_absurd_backends
from django_absurd.worker import WorkerOptions, aworker_client, run_blocking_worker

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("_isolate_queues"),
]

# concurrency 2 so the poll loop keeps claiming with the one slow run in flight: a
# worker that joined its whole batch before claiming again could never redeliver it.
SHORT_LEASE_OPTIONS = WorkerOptions(concurrency=2, claim_timeout=1, poll_interval=0.05)

HOLD: dict[str, asyncio.Event] = {}
REDELIVERED: dict[str, asyncio.Event] = {}
EXECUTIONS: list[str] = []


@task(queue_name="default")
async def hold_until_a_redelivery_arrives(name: str) -> str:
    """Hold one in-flight run open, announcing a second delivery of the same run."""
    EXECUTIONS.append(name)
    if len(EXECUTIONS) > 1:
        REDELIVERED["gate"].set()
    await HOLD["gate"].wait()
    return "held"


@task(queue_name="default")
async def hold_while_heartbeating(name: str) -> str:
    EXECUTIONS.append(name)
    context = aget_absurd_context()
    while not HOLD["gate"].is_set():
        await context.heartbeat()
        await asyncio.sleep(0.05)
    return "held"


def arm_events() -> None:
    EXECUTIONS.clear()
    HOLD["gate"] = asyncio.Event()
    REDELIVERED["gate"] = asyncio.Event()


def test_a_body_outrunning_its_own_lease_is_delivered_to_the_same_worker_again() -> (
    None
):
    """At-least-once, from inside one worker: the lease is the only thing keeping a
    running body's run off the claim queue, so a body that neither finishes nor
    heartbeats within ``claim_timeout`` is re-claimed by the very worker still
    executing it — and its un-checkpointed work runs a second time, concurrently
    with the first.
    """
    call_command("absurd_sync_queues")
    arm_events()
    backend = get_absurd_backends()["default"]
    result = hold_until_a_redelivery_arrives.enqueue("slow")

    async def drive() -> None:
        stop = asyncio.Event()
        async with aworker_client(backend, "default") as client:

            async def release_once_the_run_is_delivered_again() -> None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(REDELIVERED["gate"].wait(), timeout=5)
                HOLD["gate"].set()
                stop.set()

            await asyncio.gather(
                run_blocking_worker(client, SHORT_LEASE_OPTIONS, stop=stop),
                release_once_the_run_is_delivered_again(),
            )

    asyncio.run(drive())

    assert EXECUTIONS == ["slow", "slow"]
    assert backend.get_result(result.id).attempts == 2


def test_heartbeating_past_the_lease_keeps_the_body_to_one_execution() -> None:
    """The counterpart, and the only way to run a body longer than ``claim_timeout``
    safely: the same hold, past the same lease, with ``heartbeat()`` extending the
    claim as it goes — one execution, completed on its first attempt.
    """
    call_command("absurd_sync_queues")
    arm_events()
    backend = get_absurd_backends()["default"]
    result = hold_while_heartbeating.enqueue("slow")

    async def drive() -> None:
        stop = asyncio.Event()
        async with aworker_client(backend, "default") as client:

            async def release_well_past_the_lease() -> None:
                # Real seconds, not a frozen shift: the worker's SDK session inherits
                # absurd.fake_now at connect time, so a later shift never reaches the
                # claim this test has to outlive.
                await asyncio.sleep(SHORT_LEASE_OPTIONS.claim_timeout * 2.5)
                HOLD["gate"].set()
                stop.set()

            await asyncio.gather(
                run_blocking_worker(client, SHORT_LEASE_OPTIONS, stop=stop),
                release_well_past_the_lease(),
            )

    asyncio.run(drive())

    snapshot = backend.get_result(result.id)
    assert EXECUTIONS == ["slow"]
    assert snapshot.status is TaskResultStatus.SUCCESSFUL
    assert snapshot.attempts == 1
