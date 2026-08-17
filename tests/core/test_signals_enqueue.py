import asyncio
import datetime as dt
import logging
import typing as t

import pytest
from django.tasks import TaskResultStatus
from django.tasks.signals import task_enqueued, task_finished, task_started
from django.utils import timezone

from django_absurd.backends import AbsurdBackend
from django_absurd.test import AbsurdTestRuntime
from tests import tasks, utils


def test_enqueue_sends_task_enqueued(caplog: pytest.LogCaptureFixture) -> None:
    receiver = utils.RecordingReceiver()
    # tests/settings.py sets no LOGGING, so Django's default puts "django" at INFO and
    # the DEBUG record this asserts on is dropped at the logger before any handler sees
    # it.
    caplog.set_level(logging.DEBUG, logger="django.tasks")
    with utils.connect_receiver(task_enqueued, receiver, sender=AbsurdBackend):
        result = tasks.add.enqueue(1, 2)

    assert [r.id for r in receiver.results] == [result.id]
    assert receiver.results[0].status == TaskResultStatus.READY
    assert receiver.results[0].task.module_path == tasks.add.module_path

    records = [r for r in caplog.records if r.name == "django.tasks"]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    assert records[0].getMessage() == (
        f"Task id={result.id} path={tasks.add.module_path} enqueued "
        f"backend={result.backend}"
    )


@pytest.mark.django_db(transaction=True)
def test_aenqueue_sends_one_task_enqueued() -> None:
    receiver = utils.RecordingReceiver()
    with utils.connect_receiver(task_enqueued, receiver, sender=AbsurdBackend):
        result = asyncio.run(tasks.add.aenqueue(1, 2))

    assert [r.id for r in receiver.results] == [result.id]


def test_enqueue_survives_a_receiver_that_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def explode(sender: type, task_result: t.Any, **kwargs: t.Any) -> None:
        msg = "receiver is broken"
        raise RuntimeError(msg)

    with (
        utils.connect_receiver(task_enqueued, explode, sender=AbsurdBackend),
        caplog.at_level(logging.ERROR, logger="django_absurd"),
    ):
        result = tasks.add.enqueue(1, 2)

    assert result.status == TaskResultStatus.READY
    errors = [r for r in caplog.records if r.name == "django_absurd.dispatch"]
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert errors[0].getMessage() == (
        f'task_enqueued receiver failed for task result id="{result.id}"'
    )


def test_absurd_sender_filter_ignores_another_backend() -> None:
    receiver = utils.RecordingReceiver()
    on_immediate = tasks.add.using(backend="immediate")
    with utils.connect_receiver(task_enqueued, receiver, sender=AbsurdBackend):
        on_immediate.enqueue(1, 2)

    assert receiver.results == []


@pytest.mark.django_db(transaction=True)
def test_a_deferred_enqueue_sends_two_signals(dj_absurd: AbsurdTestRuntime) -> None:
    enqueued = utils.RecordingReceiver()
    started = utils.RecordingReceiver()
    finished = utils.RecordingReceiver()
    with (
        utils.connect_receiver(task_enqueued, enqueued, sender=AbsurdBackend),
        utils.connect_receiver(task_started, started, sender=AbsurdBackend),
        utils.connect_receiver(task_finished, finished, sender=AbsurdBackend),
        dj_absurd.freeze_time() as frozen_time,
    ):
        due = timezone.now() + dt.timedelta(hours=1)
        wrapper = tasks.add.using(run_after=due).enqueue(1, 2)
        assert [r.id for r in enqueued.results] == [wrapper.id]

        frozen_time.shift(dt.timedelta(hours=2))
        dj_absurd.drain()

    sent = enqueued.results
    assert len(sent) == 2
    assert sent[0].task.run_after is not None
    assert sent[1].task.run_after is None
    assert sent[0].id != sent[1].id
    # The wrapper's id is a permanent orphan: the wrapper handler sends nothing, so only
    # the real task's id ever starts or finishes. A refactor breaks this silently. The
    # positive assertion is what keeps the absence a claim rather than a tautology.
    assert wrapper.id not in [r.id for r in started.results]
    assert wrapper.id not in [r.id for r in finished.results]
    assert sent[1].id in [r.id for r in started.results]
