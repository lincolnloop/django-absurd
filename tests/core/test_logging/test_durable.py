import datetime as dt
import logging
import re

import pytest

from django_absurd import params as params_module
from django_absurd.test import AbsurdTestRuntime
from tests import atasks, tasks

pytestmark = pytest.mark.django_db(transaction=True)

DURATION = r"\d+\.\d{3}"


def read_context_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "django_absurd.context"]


def test_an_async_step_logs_completed_with_a_duration(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        result = atasks.astep_echo.enqueue("v")
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    messages = read_context_messages(caplog)
    assert len(messages) == 1
    assert re.fullmatch(
        rf"step completed: name=\"echo\" task_id=\"{task_id}\" duration={DURATION}",
        messages[0],
    )


def test_an_async_replayed_step_logs_replayed_not_completed(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    """Attempt 1 completes the step then fails; attempt 2 must skip the body."""
    two_attempts = params_module.absurd_params(max_attempts=2).bind(
        atasks.acharge_then_fail_once
    )
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        result = two_attempts.enqueue()
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    messages = read_context_messages(caplog)
    completed = [m for m in messages if m.startswith("step completed: ")]
    assert len(completed) == 1
    assert re.fullmatch(
        rf"step completed: name=\"charge\" task_id=\"{task_id}\" duration={DURATION}",
        completed[0],
    )
    assert messages.count(f'step replayed: name="charge" task_id="{task_id}"') == 1
    assert len(messages) == 2


def test_a_sync_step_logs_completed_with_a_duration(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        result = tasks.sstep_echo.enqueue("v")
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    messages = read_context_messages(caplog)
    assert len(messages) == 1
    assert re.fullmatch(
        rf"step completed: name=\"echo\" task_id=\"{task_id}\" duration={DURATION}",
        messages[0],
    )


def test_a_sync_replayed_step_logs_replayed_not_completed(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    two_attempts = params_module.absurd_params(max_attempts=2).bind(
        tasks.scharge_then_fail_once
    )
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        result = two_attempts.enqueue()
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    messages = read_context_messages(caplog)
    assert messages.count(f'step replayed: name="charge" task_id="{task_id}"') == 1
    assert len([m for m in messages if m.startswith("step completed: ")]) == 1
    assert len(messages) == 2


def test_an_async_sleep_logs_suspended_then_resumed(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time() as frozen_time,
    ):
        result = atasks.asleep_for_once.enqueue("k")
        dj_absurd.drain()
        frozen_time.shift(dt.timedelta(days=8))
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    messages = read_context_messages(caplog)
    assert (
        messages.count(
            f'sleep suspended: step="nap" task_id="{task_id}" for={tasks.WEEK_SECONDS}'
        )
        == 1
    )
    assert messages.count(f'sleep resumed: step="nap" task_id="{task_id}"') == 1


def test_a_sync_sleep_logs_suspended_then_resumed(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time() as frozen_time,
    ):
        result = tasks.ssleep_for_once.enqueue("k")
        dj_absurd.drain()
        frozen_time.shift(dt.timedelta(days=8))
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    messages = read_context_messages(caplog)
    assert (
        messages.count(
            f'sleep suspended: step="nap" task_id="{task_id}" for={tasks.WEEK_SECONDS}'
        )
        == 1
    )
    assert messages.count(f'sleep resumed: step="nap" task_id="{task_id}"') == 1


def test_an_async_sleep_until_reports_its_wake_time(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time() as frozen_time,
    ):
        result = atasks.asleep_until_once.enqueue("k")
        dj_absurd.drain()
        frozen_time.shift(dt.timedelta(days=8))
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    messages = read_context_messages(caplog)
    suspended = [m for m in messages if m.startswith("sleep suspended: ")]
    assert len(suspended) == 1
    assert re.fullmatch(
        rf"sleep suspended: step=\"nap\" task_id=\"{task_id}\" until=\S+", suspended[0]
    )
    assert messages.count(f'sleep resumed: step="nap" task_id="{task_id}"') == 1


def test_a_sleep_until_reports_its_wake_time(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time() as frozen_time,
    ):
        result = tasks.ssleep_until_once.enqueue("k")
        dj_absurd.drain()
        frozen_time.shift(dt.timedelta(days=8))
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    messages = read_context_messages(caplog)
    suspended = [m for m in messages if m.startswith("sleep suspended: ")]
    assert len(suspended) == 1
    assert re.fullmatch(
        rf"sleep suspended: step=\"nap\" task_id=\"{task_id}\" until=\S+", suspended[0]
    )
    assert messages.count(f'sleep resumed: step="nap" task_id="{task_id}"') == 1


def test_an_async_event_wait_logs_awaiting_then_received(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        result = atasks.aawait_the_probe_event.enqueue()
        dj_absurd.drain()
        emitter_result = atasks.aemit_the_probe_event.enqueue()
        dj_absurd.drain()
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    emitter_task_id = emitter_result.id.rsplit(":", 1)[-1]
    messages = read_context_messages(caplog)
    assert (
        messages.count(
            f'event awaiting: name="probe.go" task_id="{task_id}" timeout=3600'
        )
        == 1
    )
    assert messages.count(f'event received: name="probe.go" task_id="{task_id}"') == 1
    emitted = [m for m in messages if m.startswith("event emitted: ")]
    assert len(emitted) == 1
    assert emitted[0] == f'event emitted: name="probe.go" task_id="{emitter_task_id}"'


def test_a_sync_event_wait_logs_awaiting_then_received(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        result = tasks.await_the_probe_event.enqueue()
        dj_absurd.drain()
        emitter_result = tasks.emit_the_probe_event.enqueue()
        dj_absurd.drain()
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    emitter_task_id = emitter_result.id.rsplit(":", 1)[-1]
    messages = read_context_messages(caplog)
    assert (
        messages.count(
            f'event awaiting: name="probe.go" task_id="{task_id}" timeout=3600'
        )
        == 1
    )
    assert messages.count(f'event received: name="probe.go" task_id="{task_id}"') == 1
    emitted = [m for m in messages if m.startswith("event emitted: ")]
    assert len(emitted) == 1
    assert emitted[0] == f'event emitted: name="probe.go" task_id="{emitter_task_id}"'


def test_no_durable_primitive_log_record_contains_a_non_ascii_character(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    """Scoped to text this package authors: a caller's own step and event names travel
    verbatim, so the fixture task here (a step, then a sleep) uses ASCII names only.
    """
    with (
        caplog.at_level(logging.DEBUG, logger="django_absurd"),
        dj_absurd.freeze_time() as frozen_time,
    ):
        atasks.asleep_for_once.enqueue("k")
        dj_absurd.drain()
        frozen_time.shift(dt.timedelta(days=8))
        dj_absurd.drain()

    absurd_records = [r for r in caplog.records if r.name.startswith("django_absurd")]
    assert absurd_records
    for record in absurd_records:
        message = record.getMessage()
        assert message.isascii(), f"non-ASCII log record: {message!r}"
