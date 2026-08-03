import asyncio
import datetime as dt
import logging

import pytest

from django_absurd import absurd_params, aget_absurd_context, get_absurd_context
from django_absurd.test import AbsurdTestRuntime
from tests import atasks, tasks

pytestmark = pytest.mark.django_db(transaction=True)


def test_get_absurd_context_outside_a_task_raises() -> None:
    with pytest.raises(
        RuntimeError,
        match="get_absurd_context\\(\\) must be called inside a running Absurd task",
    ):
        get_absurd_context()


def test_aget_absurd_context_outside_a_task_raises() -> None:
    with pytest.raises(
        RuntimeError,
        match="aget_absurd_context\\(\\) must be called inside a running Absurd task",
    ):
        aget_absurd_context()


def test_get_absurd_context_on_loop_raises() -> None:
    async def call() -> None:
        get_absurd_context()

    with pytest.raises(
        RuntimeError,
        match="get_absurd_context\\(\\) is for sync tasks; use aget_absurd_context",
    ):
        asyncio.run(call())


def test_async_step_runs_and_returns_value(dj_absurd: AbsurdTestRuntime) -> None:
    atasks.astep_echo.enqueue("hi")
    assert [(run.state, run.result) for run in dj_absurd.drain()] == [
        ("completed", "hi")
    ]


def test_async_headers_readable_from_ctx(dj_absurd: AbsurdTestRuntime) -> None:
    absurd_params(headers={"tenant": "acme"}).bind(atasks.aheaders_tenant).enqueue()
    assert [run.result for run in dj_absurd.drain()] == ["acme"]


def test_async_heartbeat_is_callable(dj_absurd: AbsurdTestRuntime) -> None:
    atasks.aheartbeat_then_return.enqueue("ok")
    assert [(run.state, run.result) for run in dj_absurd.drain()] == [
        ("completed", "ok")
    ]


def test_async_sleep_for_suspends_then_resumes_replaying_step(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    atasks.DURABLE_STEP_CALLS["n"] = 0

    with dj_absurd.freeze_time() as frozen_time:
        atasks.asleep_for_once.enqueue("k")

        # drain 1: bump runs, then sleep -> suspend
        assert [run.state for run in dj_absurd.drain()] == ["sleeping"]

        frozen_time.shift(dt.timedelta(days=8))

        # drain 2: body replays, bump cached, completes
        assert [(run.state, run.result) for run in dj_absurd.drain()] == [
            ("completed", 1)
        ]

    assert atasks.DURABLE_STEP_CALLS["n"] == 1  # step body ran once across the replay


def test_async_sleep_until_suspends_then_resumes(dj_absurd: AbsurdTestRuntime) -> None:
    with dj_absurd.freeze_time() as frozen_time:
        atasks.asleep_until_once.enqueue("k")

        assert [run.state for run in dj_absurd.drain()] == ["sleeping"]

        frozen_time.shift(dt.timedelta(days=8))

        assert [(run.state, run.result) for run in dj_absurd.drain()] == [
            ("completed", "woke")
        ]


def test_sync_step_runs_and_returns_value(dj_absurd: AbsurdTestRuntime) -> None:
    tasks.sstep_echo.enqueue("hi")
    assert [run.result for run in dj_absurd.drain()] == ["hi"]


def test_sync_headers_heartbeat_and_run_step_forms(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    absurd_params(headers={"tenant": "acme"}).bind(tasks.scoverage).enqueue()
    assert [run.result for run in dj_absurd.drain()] == [
        {
            "bare": "bare-val",
            "derived": "derived-val",
            "named": "named-val",
            "tenant": "acme",
        }
    ]


def test_sync_sleep_for_suspends_then_resumes_replaying_step(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    tasks.SYNC_STEP_CALLS["n"] = 0

    with dj_absurd.freeze_time() as frozen_time:
        tasks.ssleep_for_once.enqueue("k")

        assert [run.state for run in dj_absurd.drain()] == ["sleeping"]

        frozen_time.shift(dt.timedelta(days=8))

        assert [(run.state, run.result) for run in dj_absurd.drain()] == [
            ("completed", 1)
        ]

    assert tasks.SYNC_STEP_CALLS["n"] == 1


def test_sync_sleep_until_suspends_then_resumes(dj_absurd: AbsurdTestRuntime) -> None:
    with dj_absurd.freeze_time() as frozen_time:
        tasks.ssleep_until_once.enqueue("k")

        assert [run.state for run in dj_absurd.drain()] == ["sleeping"]

        frozen_time.shift(dt.timedelta(days=8))

        assert [(run.state, run.result) for run in dj_absurd.drain()] == [
            ("completed", "woke")
        ]


def test_suspend_logged_as_lifecycle_not_failure(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    atasks.DURABLE_STEP_CALLS["n"] = 0
    atasks.asleep_for_once.enqueue("k")
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        assert [run.state for run in dj_absurd.drain()] == ["sleeping"]
    assert "task suspended: name=tests.atasks.asleep_for_once" in caplog.text
    assert "task failed" not in caplog.text
