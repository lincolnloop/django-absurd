import logging

import pytest
from absurd_sdk import RetryStrategy
from django.tasks import TaskResultStatus, task_backends
from django.tasks.signals import task_finished, task_started

from django_absurd import params as params_module
from django_absurd.backends import AbsurdBackend
from django_absurd.test import AbsurdTestRuntime
from tests import tasks, utils

pytestmark = pytest.mark.django_db(transaction=True)


def test_only_the_terminal_attempt_reports_a_failure(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    finished = utils.RecordingReceiver()
    started = utils.RecordingReceiver()
    two_attempts = params_module.absurd_params(max_attempts=2).bind(tasks.boom)
    caplog.set_level(logging.DEBUG, logger="django.tasks")
    with (
        utils.connect_receiver(task_started, started, sender=AbsurdBackend),
        utils.connect_receiver(task_finished, finished, sender=AbsurdBackend),
        dj_absurd.freeze_time(),
    ):
        two_attempts.enqueue()
        # Default retry strategy is kind 'none' -> delay 0, so both attempts are
        # claimable inside one drain; no clock movement needed.
        dj_absurd.drain()

    assert len(started.results) == 2
    assert len(finished.results) == 1
    assert finished.results[0].status == TaskResultStatus.FAILED
    assert len(finished.results[0].errors) == 1

    errors = [
        r
        for r in caplog.records
        if r.name == "django.tasks" and r.levelno >= logging.WARNING
    ]
    assert len(errors) == 1
    assert errors[0].levelno == logging.ERROR
    failed = finished.results[0]
    assert errors[0].getMessage() == (
        f"Task id={failed.id} path={tasks.boom.module_path} state=FAILED"
    )
    assert errors[0].exc_info is not None
    # The line names the task's OWN exception: the send happens inside the handler's
    # except arm, so sys.exc_info() is what the task raised.
    assert errors[0].exc_info[0] is ValueError


def test_unbounded_attempts_never_report_terminal(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    finished = utils.RecordingReceiver()
    # A backoff is mandatory here: with max_attempts=None and the default delay-0
    # strategy, the burst drain re-claims the failing task forever and the test hangs
    # until pytest-timeout kills it.
    unbounded = params_module.absurd_params(
        max_attempts=None,
        retry_strategy=RetryStrategy(kind="fixed", base_seconds=3600),
    ).bind(tasks.boom)
    with (
        utils.connect_receiver(task_finished, finished, sender=AbsurdBackend),
        dj_absurd.freeze_time(),
    ):
        unbounded.enqueue()
        dj_absurd.drain()

    assert finished.results == []


def test_the_persisted_traceback_holds_only_the_tasks_own_failure(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    """The SDK formats the live exception's __traceback__ into failure_reason, so
    anything django-absurd runs while REPORTING that failure must stay off it.
    """
    one_attempt = params_module.absurd_params(max_attempts=1).bind(tasks.boom)
    with dj_absurd.freeze_time():
        result = one_attempt.enqueue()
        dj_absurd.drain()

    failure = dj_absurd.get_result(result.id).failure
    assert failure is not None
    assert "send_finished_if_terminal" not in failure["traceback"]
    assert "send_task_signal" not in failure["traceback"]
    # Ends at the task's own exception, so nothing was appended after the handler saw
    # it.
    assert failure["traceback"].strip().endswith("ValueError: boom")
    assert task_backends["default"].get_result(result.id).errors[0].traceback
