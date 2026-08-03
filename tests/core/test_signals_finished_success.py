import logging

import pytest
from django.tasks import TaskResultStatus, task, task_backends
from django.tasks.signals import task_finished

from django_absurd.backends import AbsurdBackend
from django_absurd.test import AbsurdTestRuntime
from tests import tasks, utils

pytestmark = pytest.mark.django_db(transaction=True)


@task
def return_a_tuple() -> tuple[int, int]:
    return (1, 2)


def test_finished_reports_success_and_the_return_value(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    receiver = utils.RecordingReceiver()
    caplog.set_level(logging.DEBUG, logger="django.tasks")
    with (
        utils.connect_receiver(task_finished, receiver, sender=AbsurdBackend),
        dj_absurd.freeze_time(),
    ):
        result = tasks.add.enqueue(1, 2)
        dj_absurd.drain()

    assert [r.id for r in receiver.results] == [result.id]
    sent = receiver.results[0]
    assert sent.status == TaskResultStatus.SUCCESSFUL
    assert sent.finished_at is not None
    # An unset _return_value reads as None here — silently wrong rather than raising.
    assert sent.return_value == 3
    assert task_backends["default"].get_result(result.id).return_value == 3

    records = [r for r in caplog.records if r.name == "django.tasks"]
    assert [r.levelno for r in records] == [logging.DEBUG, logging.INFO, logging.INFO]
    assert [r.getMessage() for r in records] == [
        (
            f"Task id={result.id} path={tasks.add.module_path} enqueued "
            f"backend={result.backend}"
        ),
        f"Task id={result.id} path={tasks.add.module_path} state=RUNNING",
        f"Task id={result.id} path={tasks.add.module_path} state=SUCCESSFUL",
    ]


def test_finished_normalizes_the_return_value(dj_absurd: AbsurdTestRuntime) -> None:
    """A raw tuple would reach the receiver as a tuple while the store reads back a
    list — the same value in two shapes depending on where you read it.
    """
    receiver = utils.RecordingReceiver()
    with (
        utils.connect_receiver(task_finished, receiver, sender=AbsurdBackend),
        dj_absurd.freeze_time(),
    ):
        result = return_a_tuple.enqueue()
        dj_absurd.drain()

    assert receiver.results[0].return_value == [1, 2]
    assert task_backends["default"].get_result(result.id).return_value == [1, 2]


def test_a_suspended_run_reports_nothing(dj_absurd: AbsurdTestRuntime) -> None:
    """A durable sleep is not an ending, so the handler's ``SuspendTask`` arm sends
    nothing. Were it to fall through to the ``except Exception`` arm instead, the
    suspension would be reported as the task failing.
    """
    receiver = utils.RecordingReceiver()
    with (
        utils.connect_receiver(task_finished, receiver, sender=AbsurdBackend),
        dj_absurd.freeze_time(),
    ):
        tasks.sleep_a_week.enqueue()
        dj_absurd.drain()

    assert receiver.results == []
