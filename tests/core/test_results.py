import asyncio
import datetime as dt
import typing as t
import uuid

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connections, transaction
from django.tasks import TaskResultStatus
from django.tasks.exceptions import TaskResultDoesNotExist

from django_absurd.backends import AbsurdBackend, get_absurd_backends
from django_absurd.params import AbsurdSpawnParams
from django_absurd.queues import get_absurd_client
from tests.models import Payload
from tests.tasks import add, boom, echo

pytestmark = pytest.mark.django_db(transaction=True)


def backend() -> AbsurdBackend:
    return get_absurd_backends()["default"]


def run_absurd_worker(queue: str = "default") -> None:
    call_command("absurd_worker", queue=queue, burst=True)


def test_get_result_pending() -> None:
    call_command("absurd_sync_queues")
    r = add.enqueue(2, 3)
    got = backend().get_result(r.id)
    assert got.id == r.id
    assert got.status == TaskResultStatus.READY
    assert got.args == [2, 3]
    assert got.kwargs == {}
    assert got.enqueued_at is not None
    assert got.task.module_path == "tests.tasks.add"


def test_get_result_successful() -> None:
    call_command("absurd_sync_queues")
    r = add.enqueue(2, 3)
    run_absurd_worker()
    got = backend().get_result(r.id)
    assert got.status == TaskResultStatus.SUCCESSFUL
    assert got.return_value == 5
    assert got.finished_at is not None
    assert got.last_attempted_at is not None
    assert got.worker_ids  # non-empty


def test_refresh_round_trip() -> None:
    call_command("absurd_sync_queues")
    r = add.enqueue(2, 3)
    assert r.status == TaskResultStatus.READY  # prior to running
    run_absurd_worker()
    r.refresh()
    assert r.status == TaskResultStatus.SUCCESSFUL  # type: ignore[comparison-overlap]
    assert r.return_value == 5


def test_get_result_failed_has_errors() -> None:
    call_command("absurd_sync_queues")
    r = boom.enqueue(
        absurd_spawn_params=AbsurdSpawnParams(max_attempts=1)  # type: ignore[call-arg]
    )
    run_absurd_worker()
    got = backend().get_result(r.id)
    assert got.status == TaskResultStatus.FAILED
    assert len(got.errors) == 1
    assert "ValueError" in got.errors[0].exception_class_path
    assert got.errors[0].traceback


def test_via_task_get_result() -> None:
    call_command("absurd_sync_queues")
    r = add.enqueue(2, 3)
    got = add.get_result(r.id)  # public path; must not raise TaskResultMismatch
    assert got.id == r.id


def test_unknown_id_raises_does_not_exist() -> None:
    call_command("absurd_sync_queues")
    with pytest.raises(TaskResultDoesNotExist):
        backend().get_result(f"default:{uuid.uuid4()}")


def test_malformed_id_raises_does_not_exist() -> None:
    call_command("absurd_sync_queues")
    with pytest.raises(TaskResultDoesNotExist):
        backend().get_result("nocolon")


def test_get_result_inside_atomic_does_not_poison_txn() -> None:
    call_command("absurd_sync_queues")
    with connections["default"].cursor() as cur:
        cur.execute("DROP TABLE absurd.t_other CASCADE")
    with transaction.atomic():
        with pytest.raises(TaskResultDoesNotExist):
            backend().get_result(f"other:{uuid.uuid4()}")
        # savepoint rolled back on ProgrammingError; outer transaction still usable
        assert get_absurd_client().list_queues()


def test_removed_task_raises_improperly_configured() -> None:
    call_command("absurd_sync_queues")
    # spawn a row whose task_name does not import
    spawn = get_absurd_client().spawn(
        "tests.tasks.does_not_exist", {"args": [], "kwargs": {}}, queue="default"
    )
    rid = f"default:{spawn['task_id']}"
    with pytest.raises(ImproperlyConfigured):
        backend().get_result(rid)


def test_injection_in_queue_segment_is_safe() -> None:
    call_command("absurd_sync_queues")
    evil = 'default"; drop table absurd.queues; --'
    with pytest.raises(TaskResultDoesNotExist):
        backend().get_result(f"{evil}:{uuid.uuid4()}")
    # the queues table still exists
    assert "default" in get_absurd_client().list_queues()


def test_aget_result_matches_get_result() -> None:
    call_command("absurd_sync_queues")
    r = add.enqueue(2, 3)
    got = asyncio.run(backend().aget_result(r.id))
    assert got.id == r.id
    assert got.status == TaskResultStatus.READY


def test_get_result_does_not_poison_jsonfield_reads() -> None:
    # get_result must not register a connection-scoped jsonb decoder: a later
    # ORM JSONField read on the same connection would then receive an
    # already-decoded dict and raise TypeError in json.loads.
    #
    # Start from a fresh psycopg connection with no prior loader registrations.
    conn = connections["default"]
    conn.close()
    # Provision a queue and spawn a task via raw SQL, bypassing build_absurd_client.
    with conn.cursor() as cursor:
        cursor.execute("SELECT absurd.create_queue(%s)", ["default"])
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT (absurd.spawn_task(%s, %s, %s::jsonb)).task_id",
            ["default", "tests.tasks.add", '{"args": [2, 3], "kwargs": {}}'],
        )
        task_id = cursor.fetchone()[0]
    result_id = f"default:{task_id}"
    # Verify JSONField reads work on the fresh connection BEFORE get_result.
    payload = Payload.objects.create(data={"key": "value", "n": 42})
    loaded = Payload.objects.get(pk=payload.pk)
    assert loaded.data == {"key": "value", "n": 42}
    # Call get_result; with the old connection-scoped loader this would poison
    # the Django connection so the assertion below would raise TypeError.
    backend().get_result(result_id)
    # JSONField reads must still succeed on the same connection after get_result.
    loaded_after = Payload.objects.get(pk=payload.pk)
    assert loaded_after.data == {"key": "value", "n": 42}


def test_enqueue_does_not_poison_jsonfield_reads() -> None:
    # Regression: build_absurd_client previously registered the jsonb loader at
    # connection scope, so enqueue() (which calls build_absurd_client) poisoned the
    # shared Django connection. A JSONField read on the same connection after any
    # enqueue call would raise TypeError. This test fails pre-fix.
    call_command("absurd_sync_queues")
    add.enqueue(1, 2)
    payload = Payload.objects.create(data={"key": "value", "n": 99})
    loaded = Payload.objects.get(pk=payload.pk)
    assert loaded.data == {"key": "value", "n": 99}


@pytest.mark.parametrize(
    "value",
    [
        None,
        0,
        False,
        "",
        [],
        {},
        {"nested": [1, 2, {"a": None, "b": "ünïçødé"}]},
    ],
)
def test_echo_return_value_round_trips(value: t.Any) -> None:
    call_command("absurd_sync_queues")
    r = echo.enqueue(value)
    run_absurd_worker()
    got = backend().get_result(r.id)
    assert got.status == TaskResultStatus.SUCCESSFUL
    assert got.return_value == value


def test_echo_args_round_trip() -> None:
    call_command("absurd_sync_queues")
    nested = {"items": [1, None, False, "ünïçødé"], "sub": {"x": 0}}
    r = echo.enqueue(nested)
    run_absurd_worker()
    got = backend().get_result(r.id)
    assert got.status == TaskResultStatus.SUCCESSFUL
    assert got.return_value == nested


def test_echo_kwargs_round_trip() -> None:
    call_command("absurd_sync_queues")
    r = echo.using(queue_name="default").enqueue(value={"k": [True, None, 42]})
    run_absurd_worker()
    got = backend().get_result(r.id)
    assert got.status == TaskResultStatus.SUCCESSFUL
    assert got.return_value == {"k": [True, None, 42]}


def test_non_json_serializable_arg_rejected_at_enqueue() -> None:
    # datetime is not JSON-serializable; the backend must reject it at enqueue
    # time rather than silently producing a broken task row.
    call_command("absurd_sync_queues")
    with pytest.raises((TypeError, ValueError)):
        echo.enqueue(dt.datetime.now(tz=dt.UTC))
