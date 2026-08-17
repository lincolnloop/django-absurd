import datetime as dt
import logging
import typing as t

import pytest
from django.tasks import TaskResultStatus
from django.tasks.signals import task_started

from django_absurd.backends import AbsurdBackend
from django_absurd.test import AbsurdTestRuntime
from tests import tasks, utils

pytestmark = pytest.mark.django_db(transaction=True)


def test_started_fires_once_per_successful_run(dj_absurd: AbsurdTestRuntime) -> None:
    receiver = utils.RecordingReceiver()
    with (
        utils.connect_receiver(task_started, receiver, sender=AbsurdBackend),
        dj_absurd.freeze_time(),
    ):
        result = tasks.add.enqueue(1, 2)
        dj_absurd.drain()

    assert [r.id for r in receiver.results] == [result.id]
    # Through statuses, not results[0].status: one TaskResult is mutated across the
    # lifecycle, so by now that object reports the task's final status instead.
    assert receiver.statuses == [TaskResultStatus.RUNNING]
    assert receiver.results[0].attempts == 1
    assert receiver.results[0].started_at is not None
    # One handler entry IS one attempt, so the two instants are the same one.
    assert receiver.results[0].last_attempted_at == receiver.results[0].started_at
    assert receiver.results[0].enqueued_at is None


def test_started_fires_per_handler_entry_across_a_sleep(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    receiver = utils.RecordingReceiver()
    caplog.set_level(logging.DEBUG, logger="django.tasks")
    with (
        utils.connect_receiver(task_started, receiver, sender=AbsurdBackend),
        dj_absurd.freeze_time() as frozen_time,
    ):
        result = tasks.sleep_a_week.enqueue()
        dj_absurd.drain()  # entry 1: suspends on the durable sleep
        assert len(receiver.results) == 1

        frozen_time.shift(dt.timedelta(days=8))
        dj_absurd.drain()  # entry 2: replays and finishes

    assert len(receiver.results) == 2
    # One attempt, two entries: the run is rescheduled, never re-created.
    assert [r.attempts for r in receiver.results] == [1, 1]
    assert dj_absurd.get_result(result.id).state == "completed"

    path = tasks.sleep_a_week.module_path
    messages = [r.getMessage() for r in caplog.records if r.name == "django.tasks"]
    assert messages == [
        f"Task id={result.id} path={path} enqueued backend={result.backend}",
        f"Task id={result.id} path={path} state=RUNNING",
        f"Task id={result.id} path={path} state=RUNNING",
        f"Task id={result.id} path={path} state=SUCCESSFUL",
    ]


def test_a_raising_receiver_does_not_fail_the_task(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    """Uncontained, this exception escapes the handler and the SDK reads it as the TASK
    failing — an audit receiver would burn every attempt of every task.
    """

    def explode(sender: type, task_result: t.Any, **kwargs: t.Any) -> None:
        msg = "receiver is broken"
        raise RuntimeError(msg)

    with (
        utils.connect_receiver(task_started, explode, sender=AbsurdBackend),
        caplog.at_level(logging.ERROR, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        result = tasks.add.enqueue(1, 2)
        dj_absurd.drain()

    assert dj_absurd.get_result(result.id).state == "completed"
    contained = [
        r
        for r in caplog.records
        if r.name == "django_absurd.dispatch" and r.levelno >= logging.ERROR
    ]
    assert len(contained) == 1
    assert contained[0].getMessage() == (
        f'task_started receiver failed for task result id="{result.id}"'
    )
