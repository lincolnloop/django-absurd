import datetime as dt
import logging
import re

import pytest

from django_absurd import params as params_module
from django_absurd.test import AbsurdTestRuntime
from tests import atasks, tasks

pytestmark = pytest.mark.django_db(transaction=True)

DURATION = r"\d+\.\d{3}s"


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
        rf"step completed: name=echo task_id={task_id} duration={DURATION}",
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
        rf"step completed: name=charge task_id={task_id} duration={DURATION}",
        completed[0],
    )
    assert messages.count(f"step replayed: name=charge task_id={task_id}") == 1
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
        rf"step completed: name=echo task_id={task_id} duration={DURATION}",
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
    assert messages.count(f"step replayed: name=charge task_id={task_id}") == 1
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
            f"sleep suspended: step=nap task_id={task_id} for={tasks.WEEK_SECONDS}s"
        )
        == 1
    )
    assert messages.count(f"sleep resumed: step=nap task_id={task_id}") == 1


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
            f"sleep suspended: step=nap task_id={task_id} for={tasks.WEEK_SECONDS}s"
        )
        == 1
    )
    assert messages.count(f"sleep resumed: step=nap task_id={task_id}") == 1


def test_a_sleep_until_reports_its_wake_time(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        result = tasks.ssleep_until_once.enqueue("k")
        dj_absurd.drain()

    task_id = result.id.rsplit(":", 1)[-1]
    suspended = [
        m for m in read_context_messages(caplog) if m.startswith("sleep suspended: ")
    ]
    assert len(suspended) == 1
    assert re.fullmatch(
        rf"sleep suspended: step=nap task_id={task_id} until=\S+", suspended[0]
    )
