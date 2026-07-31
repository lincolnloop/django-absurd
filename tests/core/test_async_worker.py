import asyncio
import time
import typing as t

import pytest
from absurd_sdk import JsonValue
from django.tasks import TaskResultStatus

from django_absurd import absurd_params
from django_absurd.backends import get_absurd_backends
from django_absurd.test import AbsurdTestRuntime
from tests import atasks, tasks
from tests.models import Payload
from tests.utils import run_absurd_worker

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "value",
    [None, 0, False, "", [], {}, {"nested": [1, 2, {"a": None, "b": "ünïçødé"}]}],
)
def test_async_return_value_round_trips(
    dj_absurd: AbsurdTestRuntime, value: JsonValue
) -> None:
    r = atasks.aecho.enqueue(value)
    run_absurd_worker()
    snap = dj_absurd.get_result(r.id)
    assert snap.state == "completed"
    assert snap.result == value


def test_async_failure_recorded(dj_absurd: AbsurdTestRuntime) -> None:
    r = absurd_params(max_attempts=1).bind(atasks.aboom).enqueue()
    run_absurd_worker()
    snap = dj_absurd.get_result(r.id)
    assert snap.state == "failed"


def test_async_takes_context_attempt_is_one(dj_absurd: AbsurdTestRuntime) -> None:
    r = atasks.areport_attempt.enqueue()
    run_absurd_worker()
    snap = dj_absurd.get_result(r.id)
    assert snap.result == 1


def test_sync_orm_jsonfield_round_trips(dj_absurd: AbsurdTestRuntime) -> None:
    # ORM in a SYNC task (executor path) — matched pair with the async-ORM test below
    r = tasks.create_payload.enqueue({"sync": True, "x": [9, 8]})
    run_absurd_worker()
    snap = dj_absurd.get_result(r.id)
    pk = t.cast("int", snap.result)
    assert Payload.objects.get(pk=pk).data == {"sync": True, "x": [9, 8]}


def test_async_orm_jsonfield_round_trips(dj_absurd: AbsurdTestRuntime) -> None:
    # ORM in an ASYNC task (loop path) — matched pair with the sync-ORM test above
    r = atasks.acreate_payload.enqueue({"async": True, "y": {"z": None}})
    run_absurd_worker()
    snap = dj_absurd.get_result(r.id)
    pk = t.cast("int", snap.result)
    assert Payload.objects.get(pk=pk).data == {"async": True, "y": {"z": None}}


def test_async_task_queries_payload(dj_absurd: AbsurdTestRuntime) -> None:
    # async QUERY path: a row created in the test, read back by an async task (aget)
    obj = Payload.objects.create(data={"q": [1, {"x": None}], "u": "ünï"})
    r = atasks.aread_payload.enqueue(obj.pk)
    run_absurd_worker()
    snap = dj_absurd.get_result(r.id)
    assert snap.state == "completed"
    assert snap.result == {"q": [1, {"x": None}], "u": "ünï"}


def test_aenqueue_async_task_runs_end_to_end(dj_absurd: AbsurdTestRuntime) -> None:
    # exercise the aenqueue (produce) path for an async task, end-to-end
    # through the worker
    r = asyncio.run(atasks.aecho.aenqueue("via-aenqueue"))
    run_absurd_worker()
    snap = dj_absurd.get_result(r.id)
    assert snap.result == "via-aenqueue"


def test_aenqueue_sync_task_runs_end_to_end(dj_absurd: AbsurdTestRuntime) -> None:
    # aenqueue a SYNC task too — runs via the worker's executor path
    r = asyncio.run(tasks.echo.aenqueue({"via": "aenqueue-sync"}))
    run_absurd_worker()
    snap = dj_absurd.get_result(r.id)
    assert snap.result == {"via": "aenqueue-sync"}


def test_full_async_workflow_aenqueue_to_aget_result() -> None:
    # The whole async pipeline in one flow: aenqueue (async produce) -> async task
    # on the loop doing async ORM (acreate) -> aget_result (async read of the result).
    r = asyncio.run(atasks.acreate_payload.aenqueue({"full": "async", "n": [1, 2]}))
    run_absurd_worker()
    got = asyncio.run(get_absurd_backends()["default"].aget_result(r.id))
    assert got.status == TaskResultStatus.SUCCESSFUL
    assert Payload.objects.filter(pk=got.return_value).exists()


def test_sync_and_async_in_one_worker_run(dj_absurd: AbsurdTestRuntime) -> None:
    rs = tasks.echo.enqueue({"mixed": "sync"})
    ra = atasks.aecho.enqueue({"mixed": "async"})
    run_absurd_worker()
    snap_s = dj_absurd.get_result(rs.id)
    snap_a = dj_absurd.get_result(ra.id)
    assert snap_s.result == {"mixed": "sync"}
    assert snap_a.result == {"mixed": "async"}


def test_worker_does_not_poison_jsonfield_reads() -> None:
    # The worker's loader is on its dedicated AsyncConnection; a Django
    # JSONField read on the shared connection after a worker run must still
    # decode (no SP6-style poison).
    atasks.aecho.enqueue("x")
    run_absurd_worker()
    obj = Payload.objects.create(data={"k": "v", "n": 7})
    assert Payload.objects.get(pk=obj.pk).data == {"k": "v", "n": 7}


def test_async_concurrency_is_not_serial() -> None:
    for _ in range(4):
        atasks.asleeper.enqueue(0.5)
    start = time.monotonic()
    run_absurd_worker(concurrency=4)  # burst now drains CONCURRENTLY (gather)
    elapsed = time.monotonic() - start
    assert elapsed < 1.5  # 4 * 0.5s serial == 2.0s; concurrent ~0.5s (well under)
