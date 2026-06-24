import time

import pytest
from django.core.management import call_command

from django_absurd.params import AbsurdSpawnParams
from tests.atasks import aboom, acreate_payload, aecho, areport_attempt, asleeper
from tests.models import Payload
from tests.tasks import create_payload, echo  # sync ORM task + sync echo
from tests.test_worker import get_task_result, run_absurd_worker

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "value",
    [None, 0, False, "", [], {}, {"nested": [1, 2, {"a": None, "b": "ünïçødé"}]}],
)
def test_async_return_value_round_trips(value):
    call_command("absurd_sync_queues")
    r = aecho.enqueue(value)
    run_absurd_worker()
    snap = get_task_result(r.id)
    assert snap.state == "completed"
    assert snap.result == value


def test_async_failure_recorded():
    call_command("absurd_sync_queues")
    r = aboom.enqueue(absurd_spawn_params=AbsurdSpawnParams(max_attempts=1))
    run_absurd_worker()
    assert get_task_result(r.id).state == "failed"


def test_async_takes_context_attempt_is_one():
    call_command("absurd_sync_queues")
    r = areport_attempt.enqueue()
    run_absurd_worker()
    assert get_task_result(r.id).result == 1


def test_sync_orm_jsonfield_round_trips():
    # ORM in a SYNC task (executor path) — matched pair with the async-ORM test below
    call_command("absurd_sync_queues")
    r = create_payload.enqueue({"sync": True, "x": [9, 8]})
    run_absurd_worker()
    pk = get_task_result(r.id).result
    assert Payload.objects.get(pk=pk).data == {"sync": True, "x": [9, 8]}


def test_async_orm_jsonfield_round_trips():
    # ORM in an ASYNC task (loop path) — matched pair with the sync-ORM test above
    call_command("absurd_sync_queues")
    r = acreate_payload.enqueue({"async": True, "y": {"z": None}})
    run_absurd_worker()
    pk = get_task_result(r.id).result
    assert Payload.objects.get(pk=pk).data == {"async": True, "y": {"z": None}}


def test_sync_and_async_in_one_worker_run():
    call_command("absurd_sync_queues")
    rs = echo.enqueue({"mixed": "sync"})
    ra = aecho.enqueue({"mixed": "async"})
    run_absurd_worker()
    assert get_task_result(rs.id).result == {"mixed": "sync"}
    assert get_task_result(ra.id).result == {"mixed": "async"}


def test_worker_does_not_poison_jsonfield_reads():
    # The worker's loader is on its dedicated AsyncConnection; a Django JSONField read
    # on the shared connection after a worker run must still decode (no SP6-style poison).
    call_command("absurd_sync_queues")
    aecho.enqueue("x")
    run_absurd_worker()
    obj = Payload.objects.create(data={"k": "v", "n": 7})
    assert Payload.objects.get(pk=obj.pk).data == {"k": "v", "n": 7}


def test_async_concurrency_is_not_serial():
    call_command("absurd_sync_queues")
    for _ in range(4):
        asleeper.enqueue(0.5)
    start = time.monotonic()
    run_absurd_worker(concurrency=4)  # burst now drains CONCURRENTLY (gather)
    elapsed = time.monotonic() - start
    assert elapsed < 1.5  # 4 * 0.5s serial == 2.0s; concurrent ~0.5s (well under)
