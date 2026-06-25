import asyncio

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.tasks import TaskResultStatus
from django.tasks.exceptions import InvalidTask

from django_absurd.connection import register_jsonb_loader
from django_absurd.params import AbsurdSpawnParams
from django_absurd.queues import get_absurd_client
from tests.tasks import add, make_group, with_default_attempts

pytestmark = pytest.mark.django_db(transaction=True)


def claim_one():
    client = get_absurd_client()
    register_jsonb_loader(connections["default"].connection)
    return client.claim_tasks(batch_size=1)


def test_enqueue_lands_and_returns_taskresult():
    call_command("absurd_sync_queues")
    result = add.enqueue(1, 2)
    assert isinstance(result.id, str)
    assert result.id
    assert result.status == TaskResultStatus.READY
    assert result.args == [1, 2]
    assert result.kwargs == {}
    assert result.backend == "default"
    claimed = claim_one()
    assert len(claimed) == 1
    assert claimed[0]["task_name"] == "tests.tasks.add"
    assert claimed[0]["params"] == {"args": [1, 2], "kwargs": {}}


def test_enqueue_preserves_kwargs():
    call_command("absurd_sync_queues")
    add.enqueue(a=1, b=2)
    assert claim_one()[0]["params"] == {"args": [], "kwargs": {"a": 1, "b": 2}}


def test_enqueue_rides_django_transaction():
    call_command("absurd_sync_queues")

    class BoomError(Exception):
        pass

    def enqueue_then_roll_back():
        with transaction.atomic():
            add.enqueue(1, 2)
            raise BoomError

    with pytest.raises(BoomError):
        enqueue_then_roll_back()
    assert claim_one() == []


def test_undeclared_queue_rejected():
    call_command("absurd_sync_queues")
    with pytest.raises(InvalidTask):
        add.using(queue_name="nope").enqueue(1, 2)


def test_aenqueue_lands():
    call_command("absurd_sync_queues")
    asyncio.run(add.aenqueue(1, 2))
    assert len(claim_one()) == 1


def test_enqueue_auto_creates_declared_queue_and_runs():
    # 'default' declared but unprovisioned (no absurd_sync_queues). Enqueue auto-creates
    # it; the worker then runs the task end-to-end.
    make_group.enqueue("auto")
    call_command("absurd_worker", queue="default", burst=True)
    assert Group.objects.filter(name="auto").exists()


def test_enqueue_to_undeclared_queue_raises():
    # 'ghost' is not in TASKS QUEUES; validate_task raises InvalidTask naming the queue.
    with pytest.raises(InvalidTask, match="ghost"):
        add.using(queue_name="ghost").enqueue(1, 2)


def test_enqueue_with_empty_queues_reports_undeclared(settings):
    # Empty QUEUES makes validate_task skip its queue check, reaching the backend guard.
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"QUEUES": {}},
        }
    }
    with pytest.raises(ImproperlyConfigured, match="not declared"):
        add.enqueue(1, 2)


def test_enqueue_auto_create_survives_outer_atomic():
    with transaction.atomic():
        make_group.enqueue("inatomic")
        assert Group.objects.count() == 0  # nothing committed yet
    call_command("absurd_worker", queue="default", burst=True)
    assert Group.objects.filter(name="inatomic").exists()


def test_enqueue_with_absent_schema_raises_clear_error():
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS absurd CASCADE")
    try:
        with pytest.raises(ImproperlyConfigured, match="migrate"):
            add.enqueue(1, 2)
    finally:
        call_command("migrate", "django_absurd", "zero", verbosity=0)
        call_command("migrate", "django_absurd", verbosity=0)


def test_max_attempts_uses_backend_default_when_unset():
    call_command("absurd_sync_queues")
    add.enqueue(1, 2)
    assert claim_one()[0]["max_attempts"] == 5


def test_max_attempts_uses_decorator_default():
    call_command("absurd_sync_queues")
    with_default_attempts.enqueue(1, 2)
    assert claim_one()[0]["max_attempts"] == 7


def test_per_call_max_attempts_overrides_decorator_and_backend():
    call_command("absurd_sync_queues")
    with_default_attempts.enqueue(
        1, 2, absurd_spawn_params=AbsurdSpawnParams(max_attempts=9)
    )
    assert claim_one()[0]["max_attempts"] == 9


def test_headers_reach_spawn():
    call_command("absurd_sync_queues")
    add.enqueue(1, 2, absurd_spawn_params=AbsurdSpawnParams(headers={"trace": "abc"}))
    assert claim_one()[0]["headers"] == {"trace": "abc"}


def test_retry_strategy_reaches_spawn():
    call_command("absurd_sync_queues")
    strategy = {
        "kind": "fixed",
        "base_seconds": 1.0,
        "factor": 2.0,
        "max_seconds": 10.0,
    }
    add.enqueue(1, 2, absurd_spawn_params=AbsurdSpawnParams(retry_strategy=strategy))
    assert claim_one()[0]["retry_strategy"] == strategy


def test_idempotency_key_dedups():
    call_command("absurd_sync_queues")
    r1 = add.enqueue(1, 2, absurd_spawn_params=AbsurdSpawnParams(idempotency_key="dup"))
    r2 = add.enqueue(1, 2, absurd_spawn_params=AbsurdSpawnParams(idempotency_key="dup"))
    assert r1.id == r2.id
    register_jsonb_loader(connections["default"].connection)
    claimed = get_absurd_client().claim_tasks(
        batch_size=10
    )  # batch>1 to catch a dup row
    assert len(claimed) == 1
    assert claimed[0]["params"] == {"args": [1, 2], "kwargs": {}}


def test_spawn_params_not_passed_to_task_func():
    call_command("absurd_sync_queues")
    add.enqueue(1, 2, absurd_spawn_params=AbsurdSpawnParams(idempotency_key="x"))
    assert claim_one()[0]["params"] == {"args": [1, 2], "kwargs": {}}


def test_result_id_encodes_queue():
    call_command("absurd_sync_queues")
    result = add.enqueue(1, 2)
    task_id = str(claim_one()[0]["task_id"])
    assert result.id == f"default:{task_id}"
