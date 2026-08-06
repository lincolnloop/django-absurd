import datetime as dt
import logging
import re

import psycopg
import pytest
from django.tasks import task

from django_absurd import get_absurd_context
from django_absurd import params as params_module
from django_absurd.queues import get_absurd_client
from django_absurd.test import AbsurdTestRuntime
from tests import tasks, utils

pytestmark = pytest.mark.django_db(transaction=True)


@task
def cancel_itself_then_heartbeat() -> None:
    """Cancel this very task, then reach for Absurd again so it says so.

    The heartbeat is where it ends: a cancelled run's next durable call raises
    ``CancelledTask``, which is Absurd ending the run rather than this code failing.
    Were it ever to return instead, the run would complete and the WARNING its test
    counts would not be there.
    """
    context = get_absurd_context()
    get_absurd_client().cancel_task(str(context.absurd_ctx.task_id))
    context.heartbeat()


@task
def fail_its_own_run_then_heartbeat() -> None:
    """Leave this run's row ``failed`` — the state another worker's failure would
    leave behind after a claim it had expired — then heartbeat, which raises
    ``FailedTask``."""
    context = get_absurd_context()
    conn = psycopg.connect(**utils.get_absurd_connection_params(), autocommit=True)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "update absurd.r_default set state = 'failed'"
                " where task_id = %s and state = 'running'",
                [str(context.absurd_ctx.task_id)],
            )
    finally:
        conn.close()
    context.heartbeat()


def read_hook_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "django_absurd.hooks"]


DURATION = r"\d+\.\d{3}s"


def test_a_successful_run_logs_started_then_completed(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        result = tasks.add.enqueue(1, 2)
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    messages = read_hook_messages(caplog)
    started = [
        m
        for m in messages
        if m
        == (
            f"task started: name={tasks.add.module_path} task_id={task_id}"
            " attempt=1 max_attempts=5"
        )
    ]
    assert len(started) == 1
    completed = [m for m in messages if m.startswith("task completed: ")]
    assert len(completed) == 1
    assert re.fullmatch(
        rf"task completed: name={re.escape(tasks.add.module_path)}"
        rf" task_id={task_id} attempt=1 max_attempts=5 duration={DURATION}",
        completed[0],
    )
    assert len(messages) == 2


def test_a_suspended_run_logs_suspended_and_starts_again_on_wake(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time() as frozen_time,
    ):
        result = tasks.sleep_a_week.enqueue()
        dj_absurd.drain()
        frozen_time.shift(dt.timedelta(days=8))
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    messages = read_hook_messages(caplog)
    started_line = (
        f"task started: name={tasks.sleep_a_week.module_path} task_id={task_id}"
        " attempt=1 max_attempts=5"
    )
    assert messages.count(started_line) == 2
    suspended = [m for m in messages if m.startswith("task suspended: ")]
    assert len(suspended) == 1
    assert re.fullmatch(
        rf"task suspended: name={re.escape(tasks.sleep_a_week.module_path)}"
        rf" task_id={task_id} attempt=1 max_attempts=5 duration={DURATION}",
        suspended[0],
    )
    completed = [m for m in messages if m.startswith("task completed: ")]
    assert len(completed) == 1
    assert re.fullmatch(
        rf"task completed: name={re.escape(tasks.sleep_a_week.module_path)}"
        rf" task_id={task_id} attempt=1 max_attempts=5 duration={DURATION}",
        completed[0],
    )
    assert len(messages) == 4


def test_a_retryable_failure_logs_error_with_the_attempt_count(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    two_attempts = params_module.absurd_params(max_attempts=2).bind(tasks.boom)
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        result = two_attempts.enqueue()
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    failures = [
        r
        for r in caplog.records
        if r.name == "django_absurd.hooks" and r.levelno == logging.ERROR
    ]
    assert len(failures) == 2
    assert failures[0].exc_info is not None
    assert re.fullmatch(
        rf"task failed: name={re.escape(tasks.boom.module_path)}"
        rf" task_id={task_id} attempt=1 max_attempts=2 duration={DURATION}",
        failures[0].getMessage(),
    )
    assert failures[1].exc_info is not None
    assert re.fullmatch(
        rf"task failed: name={re.escape(tasks.boom.module_path)}"
        rf" task_id={task_id} attempt=2 max_attempts=2 duration={DURATION}",
        failures[1].getMessage(),
    )


def test_a_cancelled_run_logs_a_warning_not_a_failure(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        result = cancel_itself_then_heartbeat.enqueue()
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    warnings = [
        r
        for r in caplog.records
        if r.name == "django_absurd.hooks" and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert re.fullmatch(
        rf"task cancelled: name={re.escape(cancel_itself_then_heartbeat.module_path)}"
        rf" task_id={task_id} attempt=1 max_attempts=5 duration={DURATION}",
        warnings[0].getMessage(),
    )
    assert not [m for m in read_hook_messages(caplog) if m.startswith("task failed: ")]


def test_a_run_already_failed_elsewhere_logs_a_warning(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        result = fail_its_own_run_then_heartbeat.enqueue()
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    warnings = [
        r
        for r in caplog.records
        if r.name == "django_absurd.hooks" and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert re.fullmatch(
        "run already failed elsewhere: "
        rf"name={re.escape(fail_its_own_run_then_heartbeat.module_path)}"
        rf" task_id={task_id} attempt=1 max_attempts=5 duration={DURATION}",
        warnings[0].getMessage(),
    )


def test_the_deferred_wrapper_is_visible_without_its_own_log_line(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    """The wrapper is a handler like any other, so the hook covers it."""
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time() as frozen_time,
    ):
        due = dj_absurd.now + dt.timedelta(hours=1)
        result = tasks.add.using(run_after=due).enqueue(1, 2)
        frozen_time.shift(dt.timedelta(hours=2))
        dj_absurd.drain()

    wrapper_name = f"{tasks.add.module_path}:run_after"
    wrapper_task_id = result.id.rsplit(":", 1)[-1]
    messages = read_hook_messages(caplog)
    started = [
        m
        for m in messages
        if m
        == (
            f"task started: name={wrapper_name} task_id={wrapper_task_id}"
            " attempt=1 max_attempts=5"
        )
    ]
    assert len(started) == 1
    completed = [
        m for m in messages if m.startswith(f"task completed: name={wrapper_name} ")
    ]
    assert len(completed) == 1
    assert re.fullmatch(
        rf"task completed: name={re.escape(wrapper_name)}"
        rf" task_id={wrapper_task_id} attempt=1 max_attempts=5 duration={DURATION}",
        completed[0],
    )
    assert not [r for r in caplog.records if r.name == "django_absurd.deferred"]
    assert len(messages) == 4


def test_no_hook_log_record_contains_a_non_ascii_character(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    """The guard the whole unit exists for: our own decoration never reaches a log
    record. Scoped to text WE author — a caller's own strings (dedup key, queue and
    schedule names, task paths) travel verbatim, so the key here is ASCII on purpose.
    """
    with (
        caplog.at_level(logging.DEBUG, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        params_module.absurd_params(idempotency_key="dedup-42").bind(tasks.add).enqueue(
            1, 2
        )
        dj_absurd.drain()

    absurd_records = [r for r in caplog.records if r.name.startswith("django_absurd")]
    assert absurd_records
    for record in absurd_records:
        message = record.getMessage()
        assert message.isascii(), f"non-ASCII log record: {message!r}"


def test_a_logging_fault_in_the_hook_does_not_fail_the_task(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    """A raising log call reaches the hook's own try/except, not the SDK's, so the task
    still completes. Scoped to the ``task completed`` event so the earlier lines and the
    fallback still come through — the same asymmetry the SDK itself gives us.
    """
    hooks_logger = logging.getLogger("django_absurd.hooks")
    hooks_logger.addFilter(fail_to_emit_the_completed_line)
    try:
        with (
            caplog.at_level(logging.INFO, logger="django_absurd"),
            dj_absurd.freeze_time(),
        ):
            result = tasks.add.enqueue(1, 2)
            dj_absurd.drain()
    finally:
        hooks_logger.removeFilter(fail_to_emit_the_completed_line)

    assert dj_absurd.get_result(result.id).state == "completed"
    faults = [
        r
        for r in caplog.records
        if r.name == "django_absurd.hooks" and r.levelno == logging.ERROR
    ]
    assert len(faults) == 1
    assert (
        faults[0].getMessage()
        == "failed to log a run lifecycle event: event=task completed"
    )


def fail_to_emit_the_completed_line(record: logging.LogRecord) -> bool:
    """Raise from inside ``Logger.handle`` for the ``task completed`` line only.

    Matched on the lifecycle template, not on the event name alone: the containment
    fallback carries that same name as its own argument, so keying on the name would
    raise a second time from inside the ``except`` and escape into the SDK.
    """
    if (
        record.msg == "%s: %s"
        and isinstance(record.args, tuple)
        and record.args[:1] == ("task completed",)
    ):
        msg = "cannot emit the lifecycle line"
        raise ValueError(msg)
    return True
