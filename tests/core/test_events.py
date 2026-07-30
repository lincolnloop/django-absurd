import psycopg.errors
import pytest
from django.core.management import call_command
from django.db import connection
from pytest_django.fixtures import SettingsWrapper

from django_absurd import emit_event
from django_absurd.exceptions import (
    BackendNotConfiguredError,
    QueueNotDeclaredError,
    QueueNotProvisionedError,
)
from django_absurd.test import AbsurdTestRuntime
from tests import atasks, tasks

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("_isolate_queues"),
]


def test_top_level_emit_event_unknown_queue_raises() -> None:
    with pytest.raises(QueueNotDeclaredError) as exc:
        emit_event("whatever", queue="ghost")
    assert str(exc.value) == (
        "Queue 'ghost' is not declared for backend 'default'. "
        "Valid queues: default, other, reports. "
        "Add it to the QUEUES list in your TASKS backend settings."
    )


def test_top_level_emit_event_no_backend_configured_raises(
    settings: SettingsWrapper,
) -> None:
    settings.TASKS = {"x": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}}
    with pytest.raises(BackendNotConfiguredError) as exc:
        emit_event("whatever")
    assert str(exc.value) == "No Absurd backend configured."


def test_top_level_emit_event_unsynced_queue_raises(
    settings: SettingsWrapper,
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"QUEUES": {"default": {}, "unsynced": {}}},
        }
    }
    with pytest.raises(
        QueueNotProvisionedError,
        match=(
            r"Queue 'unsynced' is declared but its Absurd table is not provisioned\. "
            r"Run: manage\.py absurd_sync_queues"
        ),
    ):
        emit_event("whatever", queue="unsynced")


def test_emit_event_does_not_relabel_an_unrelated_missing_relation() -> None:
    """Mirrors test_drain_queue_does_not_relabel_an_unrelated_missing_relation
    (tests/core/test_worker.py): a missing relation that is NOT one of this queue's
    own Absurd tables surfaces as itself, chained, rather than as the curated
    unprovisioned-queue error.
    """
    call_command("absurd_sync_queues")
    try:
        with connection.cursor() as cur:
            cur.execute(
                "create or replace function absurd.record_audit_probe() "
                "returns trigger language plpgsql as $$ begin "
                "insert into absurd.audit_probe (run_id) values (new.run_id); "
                "return new; end; $$"
            )
            cur.execute(
                "create trigger audit_probe_after_emit after insert "
                "on absurd.e_default for each row "
                "execute function absurd.record_audit_probe()"
            )

        with pytest.raises(psycopg.errors.UndefinedTable) as undefined:
            emit_event("order.packed:audit-probe", queue="default")

        assert (
            undefined.value.diag.message_primary
            == 'relation "absurd.audit_probe" does not exist'
        )
    finally:
        with connection.cursor() as cur:
            cur.execute("drop function if exists absurd.record_audit_probe() cascade")


def test_sync_await_event_suspends_then_top_level_emit_resumes(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    tasks.sawait_event_once.enqueue("order.packed:sync-1")

    assert [run.state for run in dj_absurd.drain()] == ["sleeping"]

    emit_event("order.packed:sync-1", {"tracking": "abc"}, queue="default")

    assert [(run.state, run.result) for run in dj_absurd.drain()] == [
        ("completed", {"tracking": "abc"})
    ]


def test_async_await_event_suspends_then_top_level_emit_resumes(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    atasks.aawait_event_once.enqueue("order.packed:async-1")

    assert [run.state for run in dj_absurd.drain()] == ["sleeping"]

    emit_event("order.packed:async-1", {"tracking": "abc"}, queue="default")

    assert [(run.state, run.result) for run in dj_absurd.drain()] == [
        ("completed", {"tracking": "abc"})
    ]


def test_emit_before_await_returns_immediately_no_suspend(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    emit_event("order.packed:before-1", {"tracking": "xyz"}, queue="default")

    tasks.sawait_event_once.enqueue("order.packed:before-1")
    assert [(run.state, run.result) for run in dj_absurd.drain()] == [
        ("completed", {"tracking": "xyz"})
    ]


def test_first_emit_per_name_wins(dj_absurd: AbsurdTestRuntime) -> None:
    dj_absurd.sync_queues()
    emit_event("order.packed:first-wins", {"tracking": "first"}, queue="default")
    emit_event("order.packed:first-wins", {"tracking": "second"}, queue="default")

    tasks.sawait_event_once.enqueue("order.packed:first-wins")
    assert [run.result for run in dj_absurd.drain()] == [{"tracking": "first"}]


def test_in_task_emit_event_wakes_a_separately_enqueued_waiter(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    tasks.semit_event_once.enqueue("order.packed:in-task", {"tracking": "in-task"})
    assert [run.state for run in dj_absurd.drain()] == ["completed"]

    tasks.sawait_event_once.enqueue("order.packed:in-task")
    assert [run.result for run in dj_absurd.drain()] == [{"tracking": "in-task"}]


def test_async_in_task_emit_event_wakes_a_separately_enqueued_waiter(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    atasks.aemit_event_once.enqueue("order.packed:in-task-async", {"tracking": "async"})
    assert [run.state for run in dj_absurd.drain()] == ["completed"]

    atasks.aawait_event_once.enqueue("order.packed:in-task-async")
    assert [run.result for run in dj_absurd.drain()] == [{"tracking": "async"}]


def test_uncaught_timeout_raises_absurd_sdk_timeout_error_and_is_catchable(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    tasks.sawait_event_timeout.enqueue("order.packed:never-arrives", timeout=0)
    assert [(run.state, run.result) for run in dj_absurd.drain()] == [
        ("sleeping", None),
        ("completed", "timed-out"),
    ]


def test_event_already_present_returns_no_timeout_before_deadline(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    emit_event("order.packed:before-timeout-1", {"tracking": "xyz"}, queue="default")

    tasks.sawait_event_timeout.enqueue("order.packed:before-timeout-1", timeout=60)
    assert [(run.state, run.result) for run in dj_absurd.drain()] == [
        ("completed", "no-timeout")
    ]
