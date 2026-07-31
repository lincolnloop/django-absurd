import uuid

import psycopg
import psycopg.errors
import pytest
from django.db import connections, transaction
from django.db.utils import ProgrammingError
from pytest_django.fixtures import SettingsWrapper

from django_absurd.exceptions import (
    BackendNotConfiguredError,
    QueueNotProvisionedError,
    TaskIdQueueMismatchError,
    TaskNotFoundError,
)
from django_absurd.test import AbsurdTestRuntime
from tests import tasks, utils

pytestmark = pytest.mark.django_db(transaction=True)


def test_get_result_reports_a_completed_task(dj_absurd: AbsurdTestRuntime) -> None:
    result = tasks.add.enqueue(2, 3)
    utils.run_absurd_worker()

    snapshot = dj_absurd.get_result(result.id)

    assert snapshot.queue == "default"
    assert snapshot.task_name == "tests.tasks.add"
    assert snapshot.args == [2, 3]
    assert snapshot.kwargs == {}
    assert snapshot.state == "completed"
    assert snapshot.result == 5
    assert snapshot.failure is None
    assert snapshot.attempts == 1
    assert isinstance(snapshot.task_id, uuid.UUID)


def test_get_result_reports_a_failed_task_with_its_failure(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    result = tasks.boom.enqueue()
    utils.run_absurd_worker()

    snapshot = dj_absurd.get_result(result.id)

    assert snapshot.state == "failed"
    assert snapshot.result is None
    assert snapshot.failure is not None
    assert snapshot.failure["name"] == "ValueError"
    assert snapshot.failure["message"] == "boom"


def test_get_result_decodes_jsonb_to_python_objects(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    result = tasks.create_payload.enqueue({"k": "v", "n": 7})
    utils.run_absurd_worker()

    snapshot = dj_absurd.get_result(result.id)

    assert snapshot.args == [{"k": "v", "n": 7}]
    assert not isinstance(snapshot.args[0], str)
    assert isinstance(snapshot.result, int)


def test_get_result_reads_a_task_suspended_on_an_indefinite_await_event(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    """An indefinite ``await_event`` parks the last attempt's run at Postgres's
    ``'infinity'`` ``available_at`` — a value psycopg cannot decode — so the read must
    never select that column.
    """
    result = tasks.sawait_event_once.enqueue("order.packed:never-arrives")
    assert [run.state for run in dj_absurd.drain()] == ["sleeping"]

    snapshot = dj_absurd.get_result(result.id)

    assert snapshot.state == "sleeping"
    assert snapshot.attempts == 1
    assert snapshot.result is None
    assert snapshot.failure is None


def test_get_result_raises_for_an_unknown_task(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    task_id = uuid.uuid4()

    with pytest.raises(TaskNotFoundError) as exc:
        dj_absurd.get_result(task_id)

    assert str(exc.value) == (
        f"No task '{task_id}' found on queue 'default'. A bare uuid resolves to "
        "queue 'default'; pass queue=... if the task ran on another queue."
    )


def test_get_result_raises_queue_not_provisioned_for_the_queues_own_missing_table(
    dj_absurd: AbsurdTestRuntime, settings: SettingsWrapper
) -> None:
    """Same facade ``drain()`` and ``emit()`` give this condition — Django's own
    ``ProgrammingError`` (chained from the psycopg ``UndefinedTable`` on
    ``exc.__cause__``) never leaks through this read.
    """
    settings.TASKS = utils.make_tasks_settings(
        queues={**utils.DECLARED_QUEUES, "unsynced": {}}
    )

    with pytest.raises(QueueNotProvisionedError) as exc:
        dj_absurd.get_result(f"unsynced:{uuid.uuid4()}")

    assert str(exc.value) == (
        "Queue 'unsynced' is declared but its Absurd table is not provisioned. "
        "Run: manage.py absurd_sync_queues"
    )


def test_get_result_does_not_relabel_an_unrelated_missing_relation(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    """Mirrors ``test_drain_queue_does_not_relabel_an_unrelated_missing_relation``
    (``tests/core/test_worker.py``): a missing relation that is NOT one of this
    queue's own Absurd tables surfaces as itself, chained, not as the curated
    unprovisioned-queue error. ``get_result`` is a plain SELECT — Postgres has no
    SELECT trigger to hook, so a view stands in for ``t_default`` instead.
    """
    result = tasks.add.enqueue(2, 3)
    utils.run_absurd_worker()
    try:
        with connections["default"].cursor() as cur:
            cur.execute("alter table absurd.t_default rename to t_default_probe")
            cur.execute(
                "create function absurd.record_get_result_probe(p_task_id uuid) "
                "returns uuid language plpgsql as $$ begin "
                "perform 1 from absurd.audit_probe where run_id = p_task_id; "
                "return p_task_id; end; $$"
            )
            cur.execute(
                "create view absurd.t_default as select "
                "absurd.record_get_result_probe(task_id) as task_id, task_name, "
                "params, headers, retry_strategy, max_attempts, cancellation, "
                "enqueue_at, first_started_at, state, attempts, last_attempt_run, "
                "completed_payload, cancelled_at, idempotency_key "
                "from absurd.t_default_probe"
            )

        with pytest.raises(ProgrammingError) as exc:
            dj_absurd.get_result(result.id)

        cause = exc.value.__cause__
        assert isinstance(cause, psycopg.errors.UndefinedTable)
        assert (
            cause.diag.message_primary == 'relation "absurd.audit_probe" does not exist'
        )
    finally:
        with connections["default"].cursor() as cur:
            cur.execute("drop view if exists absurd.t_default")
            cur.execute("alter table absurd.t_default_probe rename to t_default")
            cur.execute(
                "drop function if exists absurd.record_get_result_probe(uuid) cascade"
            )


def test_get_result_tolerates_params_that_are_not_our_shape(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    """A queue shared with raw-SDK producers can hold any JSON in params."""
    result = tasks.add.enqueue(2, 3)
    utils.run_absurd_worker()
    task_id = str(result.id).rsplit(":", 1)[-1]
    params = connections["default"].get_connection_params()
    params.pop("cursor_factory", None)
    conn = psycopg.connect(**params, autocommit=True)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "update absurd.t_default set params = %s where task_id = %s",
                ["[1, 2, 3]", task_id],
            )
    finally:
        conn.close()

    snapshot = dj_absurd.get_result(result.id)

    assert snapshot.args == []
    assert snapshot.kwargs == {}


def test_get_result_accepts_a_bare_uuid(dj_absurd: AbsurdTestRuntime) -> None:
    result = tasks.add.enqueue(2, 3)
    utils.run_absurd_worker()
    bare = str(result.id).rsplit(":", 1)[-1]

    snapshot = dj_absurd.get_result(bare)

    assert snapshot.state == "completed"


def test_get_result_honours_a_non_default_queue_prefix_on_the_id(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    result = tasks.on_reports.enqueue()  # on_reports is @task(queue_name="reports")
    utils.run_absurd_worker(queue="reports")

    snapshot = dj_absurd.get_result(result.id)  # no queue= passed; the prefix must win

    assert snapshot.queue == "reports"
    assert snapshot.state == "completed"
    assert snapshot.result == "on_reports"


def test_get_result_accepts_a_bare_uuid_with_an_explicit_non_default_queue(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    result = tasks.routed.enqueue()  # routed is @task(queue_name="other")
    utils.run_absurd_worker(queue="other")
    bare = str(result.id).rsplit(":", 1)[-1]

    snapshot = dj_absurd.get_result(bare, queue="other")

    assert snapshot.queue == "other"
    assert snapshot.state == "completed"


@pytest.mark.parametrize("queue", ["default", "other"])
def test_get_result_raises_when_queue_disagrees_with_the_id_prefix(
    dj_absurd: AbsurdTestRuntime, queue: str
) -> None:
    # queue="default" is the case an unpassed argument alone could never catch: the
    # caller spelled out the same value the parameter would resolve to anyway.
    # Distinguishing it from "not passed at all" is why queue defaults to the
    # NOT_SET sentinel, not the literal string "default".
    result = tasks.on_reports.enqueue()  # id prefix is "reports"

    with pytest.raises(TaskIdQueueMismatchError) as exc:
        dj_absurd.get_result(result.id, queue=queue)

    assert str(exc.value) == (
        f"get_result(): task id '{result.id}' names queue "
        f"'reports', but queue='{queue}' was also passed and disagrees. Pass "
        "only one, or make them agree."
    )


@pytest.mark.parametrize("operation", ["drain", "emit", "get_result", "sync_queues"])
def test_an_operation_inside_an_open_transaction_raises(
    dj_absurd: AbsurdTestRuntime, operation: str
) -> None:
    # One guard (guard_against_open_transaction), four real enforcing entrypoints.
    def invoke() -> None:
        if operation == "drain":
            dj_absurd.drain()
        elif operation == "emit":
            dj_absurd.emit("order.packed:never-emitted")
        elif operation == "get_result":
            dj_absurd.get_result(uuid.uuid4())
        else:
            dj_absurd.sync_queues()

    with transaction.atomic(), pytest.raises(RuntimeError) as exc:
        invoke()

    assert str(exc.value) == (
        f"django-absurd: {operation}() ran inside an open transaction, where "
        "uncommitted rows are invisible to Absurd's own connection. Use "
        "@pytest.mark.django_db(transaction=True) and call outside "
        "transaction.atomic()."
    )


@pytest.mark.django_db(transaction=False)
def test_get_result_in_a_plain_db_test_raises(dj_absurd: AbsurdTestRuntime) -> None:
    with pytest.raises(
        RuntimeError,
        match=(
            r"django-absurd: get_result\(\) ran inside an open transaction, where "
            r"uncommitted rows are invisible to Absurd's own connection\. Use "
            r"@pytest\.mark\.django_db\(transaction=True\) and call outside "
            r"transaction\.atomic\(\)\."
        ),
    ):
        dj_absurd.get_result(uuid.uuid4())


def test_drain_returns_nothing_when_the_queue_is_empty(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    assert dj_absurd.drain() == []


def test_drain_returns_one_record_per_run_in_claim_order(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    tasks.add.enqueue(2, 3)
    tasks.add.enqueue(4, 5)

    drained = dj_absurd.drain()

    assert [(run.task_name, run.args, run.attempt, run.state) for run in drained] == [
        ("tests.tasks.add", [2, 3], 1, "completed"),
        ("tests.tasks.add", [4, 5], 1, "completed"),
    ]
    assert [run.result for run in drained] == [5, 9]


def test_drain_reports_a_suspended_run_as_sleeping(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    tasks.ssleep_for_once.enqueue("k")

    drained = dj_absurd.drain()

    assert [(run.task_name, run.state) for run in drained] == [
        ("tests.tasks.ssleep_for_once", "sleeping")
    ]


def test_drain_returns_a_spawned_child_in_the_same_drain(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    tasks.spawn_child_then_return.enqueue(21)

    drained = dj_absurd.drain()

    assert [run.task_name for run in drained] == [
        "tests.tasks.spawn_child_then_return",
        "tests.tasks.run_child",
    ]
    assert drained[1].result == 42
    assert dj_absurd.drain() == []


def test_drain_returns_every_attempt_of_a_default_retry_burn(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    result = tasks.boom.enqueue()

    drained = dj_absurd.drain()

    assert [run.attempt for run in drained] == [1, 2, 3, 4, 5]
    assert {run.state for run in drained} == {"failed"}
    assert drained[0].failure is not None
    snapshot = dj_absurd.get_result(result.id)
    assert snapshot.state == "failed"
    assert snapshot.attempts == 5


def test_drain_reports_each_attempt_of_a_retry_sequence(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    tasks.RETRY_CALLS["n"] = 0
    tasks.fail_twice_then_succeed.enqueue()

    drained = dj_absurd.drain()

    assert [(run.attempt, run.state) for run in drained] == [
        (1, "failed"),
        (2, "failed"),
        (3, "completed"),
    ]
    assert [
        None if run.failure is None else run.failure["message"] for run in drained
    ] == ["attempt 1 fails", "attempt 2 fails", None]
    assert drained[2].result == "third-time-lucky"


def test_drain_returns_the_same_run_twice_when_an_emit_wakes_it(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    tasks.sawait_event_once.enqueue("order.packed:same-drain")
    tasks.semit_event_once.enqueue("order.packed:same-drain", {"tracking": "abc"})

    drained = dj_absurd.drain()

    waiter_runs = [
        run for run in drained if run.task_name.endswith("sawait_event_once")
    ]
    assert [run.state for run in waiter_runs] == ["sleeping", "completed"]
    assert waiter_runs[0].run_id == waiter_runs[1].run_id
    # The SDK's ClaimedTask stub types run_id ``str``; psycopg deserializes the column,
    # so the identity above is a uuid comparison, not a string one.
    assert isinstance(waiter_runs[0].run_id, uuid.UUID)


def test_emit_resolves_a_waiting_task(dj_absurd: AbsurdTestRuntime) -> None:
    result = tasks.sawait_event_once.enqueue("order.packed:via-fixture")
    assert [run.state for run in dj_absurd.drain()] == ["sleeping"]

    dj_absurd.emit("order.packed:via-fixture", {"tracking": "abc"})

    assert [run.state for run in dj_absurd.drain()] == ["completed"]
    snapshot = dj_absurd.get_result(result.id)
    assert snapshot.result == {"tracking": "abc"}


@pytest.mark.usefixtures("_isolate_queues")
def test_sync_queues_provisions_the_declared_queues(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    """``_isolate_queues`` has dropped every queue's topology, so the drain that opens
    this test is the proof the sync below is what puts it back — the common case needs
    no sync at all, since ``migrate`` already provisions the declared catalog.
    """
    with pytest.raises(QueueNotProvisionedError) as exc:
        dj_absurd.drain()
    assert str(exc.value) == (
        "Queue 'default' is declared but its Absurd table is not provisioned. "
        "Run: manage.py absurd_sync_queues"
    )

    dj_absurd.sync_queues()

    assert dj_absurd.drain() == []
    tasks.on_reports.enqueue()
    assert [run.result for run in dj_absurd.drain("reports")] == ["on_reports"]


def test_sync_queues_with_no_absurd_backend_raises(
    dj_absurd: AbsurdTestRuntime, settings: SettingsWrapper
) -> None:
    settings.TASKS = {"x": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}}

    with pytest.raises(BackendNotConfiguredError) as exc:
        dj_absurd.sync_queues()

    assert str(exc.value) == (
        "No Absurd backend configured. Add a django_absurd.backends.AbsurdBackend "
        "entry to TASKS."
    )
