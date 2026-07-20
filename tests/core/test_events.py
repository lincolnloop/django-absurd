import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from pytest_django.fixtures import SettingsWrapper

from django_absurd import emit_event
from tests import atasks, tasks, utils

pytestmark = pytest.mark.django_db(transaction=True)


def test_top_level_emit_event_unknown_queue_raises() -> None:
    with pytest.raises(
        ImproperlyConfigured,
        match=(
            r"Queue 'ghost' is not declared in TASKS QUEUES\. Add it to the QUEUES "
            r"list in your TASKS backend settings\."
        ),
    ):
        emit_event("whatever", queue="ghost")


def test_top_level_emit_event_no_backend_configured_raises(
    settings: SettingsWrapper,
) -> None:
    settings.TASKS = {"x": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}}
    with pytest.raises(
        ImproperlyConfigured, match=r"django-absurd: no Absurd backend configured\."
    ):
        emit_event("whatever")


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
        ImproperlyConfigured,
        match=(
            r"Queue 'unsynced' is declared but its Absurd table is not provisioned\. "
            r"Run: manage\.py absurd_sync_queues"
        ),
    ):
        emit_event("whatever", queue="unsynced")


def test_sync_await_event_suspends_then_top_level_emit_resumes() -> None:
    call_command("absurd_sync_queues")
    result = tasks.sawait_event_once.enqueue("order.packed:sync-1")

    utils.run_absurd_worker()  # drain 1: no event yet -> suspend
    suspended = utils.get_task_result(result.id)
    assert suspended is not None
    assert suspended.state == "sleeping"

    emit_event("order.packed:sync-1", {"tracking": "abc"}, queue="default")

    utils.run_absurd_worker()  # drain 2: resumes with the payload
    done = utils.get_task_result(result.id)
    assert done is not None
    assert done.state == "completed"
    assert done.result == {"tracking": "abc"}


def test_async_await_event_suspends_then_top_level_emit_resumes() -> None:
    call_command("absurd_sync_queues")
    result = atasks.aawait_event_once.enqueue("order.packed:async-1")

    utils.run_absurd_worker()
    suspended = utils.get_task_result(result.id)
    assert suspended is not None
    assert suspended.state == "sleeping"

    emit_event("order.packed:async-1", {"tracking": "abc"}, queue="default")

    utils.run_absurd_worker()
    done = utils.get_task_result(result.id)
    assert done is not None
    assert done.state == "completed"
    assert done.result == {"tracking": "abc"}


def test_emit_before_await_returns_immediately_no_suspend() -> None:
    call_command("absurd_sync_queues")
    emit_event("order.packed:before-1", {"tracking": "xyz"}, queue="default")

    result = tasks.sawait_event_once.enqueue("order.packed:before-1")
    utils.run_absurd_worker()  # single drain: event already there, no suspend
    done = utils.get_task_result(result.id)
    assert done is not None
    assert done.state == "completed"
    assert done.result == {"tracking": "xyz"}


def test_first_emit_per_name_wins() -> None:
    call_command("absurd_sync_queues")
    emit_event("order.packed:first-wins", {"tracking": "first"}, queue="default")
    emit_event("order.packed:first-wins", {"tracking": "second"}, queue="default")

    result = tasks.sawait_event_once.enqueue("order.packed:first-wins")
    utils.run_absurd_worker()
    done = utils.get_task_result(result.id)
    assert done is not None
    assert done.result == {"tracking": "first"}


def test_in_task_emit_event_wakes_a_separately_enqueued_waiter() -> None:
    call_command("absurd_sync_queues")
    tasks.semit_event_once.enqueue("order.packed:in-task", {"tracking": "in-task"})
    utils.run_absurd_worker()

    result = tasks.sawait_event_once.enqueue("order.packed:in-task")
    utils.run_absurd_worker()
    done = utils.get_task_result(result.id)
    assert done is not None
    assert done.result == {"tracking": "in-task"}


def test_async_in_task_emit_event_wakes_a_separately_enqueued_waiter() -> None:
    call_command("absurd_sync_queues")
    atasks.aemit_event_once.enqueue("order.packed:in-task-async", {"tracking": "async"})
    utils.run_absurd_worker()

    result = atasks.aawait_event_once.enqueue("order.packed:in-task-async")
    utils.run_absurd_worker()
    done = utils.get_task_result(result.id)
    assert done is not None
    assert done.result == {"tracking": "async"}


def test_uncaught_timeout_raises_absurd_sdk_timeout_error_and_is_catchable() -> None:
    call_command("absurd_sync_queues")
    result = tasks.sawait_event_timeout.enqueue("order.packed:never-arrives")
    utils.run_absurd_worker()
    done = utils.get_task_result(result.id)
    assert done is not None
    assert done.state == "completed"
    assert done.result == "timed-out"
