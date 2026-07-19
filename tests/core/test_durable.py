import asyncio
import logging
import time

import pytest
from django.core.management import call_command

from django_absurd import aget_absurd_context, get_absurd_context
from django_absurd.params import AbsurdSpawnParams
from tests.atasks import (
    DURABLE_STEP_CALLS,
    aheaders_tenant,
    aheartbeat_then_return,
    asleep_for_once,
    asleep_until_once,
    astep_echo,
)
from tests.tasks import (
    SYNC_STEP_CALLS,
    scoverage,
    ssleep_for_once,
    ssleep_until_once,
    sstep_echo,
)
from tests.worker_support import get_task_result, run_absurd_worker

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


def test_async_step_runs_and_returns_value() -> None:
    call_command("absurd_sync_queues")
    result = astep_echo.enqueue("hi")
    run_absurd_worker()
    snap = get_task_result(result.id)
    assert snap is not None
    assert snap.state == "completed"
    assert snap.result == "hi"


def test_async_headers_readable_from_ctx() -> None:
    call_command("absurd_sync_queues")
    result = aheaders_tenant.enqueue(  # type: ignore[call-arg]
        absurd_spawn_params=AbsurdSpawnParams(headers={"tenant": "acme"})
    )
    run_absurd_worker()
    snap = get_task_result(result.id)
    assert snap is not None
    assert snap.result == "acme"


def test_async_heartbeat_is_callable() -> None:
    call_command("absurd_sync_queues")
    result = aheartbeat_then_return.enqueue("ok")
    run_absurd_worker()
    snap = get_task_result(result.id)
    assert snap is not None
    assert snap.state == "completed"
    assert snap.result == "ok"


def test_async_sleep_for_suspends_then_resumes_replaying_step() -> None:
    call_command("absurd_sync_queues")
    DURABLE_STEP_CALLS["n"] = 0
    result = asleep_for_once.enqueue("k")

    run_absurd_worker()  # drain 1: bump runs, then sleep -> suspend
    suspended = get_task_result(result.id)
    assert suspended is not None
    assert suspended.state == "sleeping"

    time.sleep(2)  # past wake (Python-clock wake vs DB-clock claim; wide margin)
    run_absurd_worker()  # drain 2: body replays, bump cached, completes
    done = get_task_result(result.id)
    assert done is not None
    assert done.state == "completed"
    assert done.result == 1
    assert DURABLE_STEP_CALLS["n"] == 1  # step body ran once across the replay


def test_async_sleep_until_suspends_then_resumes() -> None:
    call_command("absurd_sync_queues")
    result = asleep_until_once.enqueue("k")
    run_absurd_worker()
    suspended = get_task_result(result.id)
    assert suspended is not None
    assert suspended.state == "sleeping"
    time.sleep(2)
    run_absurd_worker()
    done = get_task_result(result.id)
    assert done is not None
    assert done.state == "completed"
    assert done.result == "woke"


def test_sync_step_runs_and_returns_value() -> None:
    call_command("absurd_sync_queues")
    result = sstep_echo.enqueue("hi")
    run_absurd_worker()
    snap = get_task_result(result.id)
    assert snap is not None
    assert snap.result == "hi"


def test_sync_headers_heartbeat_and_run_step_forms() -> None:
    call_command("absurd_sync_queues")
    result = scoverage.enqueue(  # type: ignore[call-arg]
        absurd_spawn_params=AbsurdSpawnParams(headers={"tenant": "acme"})
    )
    run_absurd_worker()
    snap = get_task_result(result.id)
    assert snap is not None
    assert snap.result == {
        "bare": "bare-val",
        "derived": "derived-val",
        "named": "named-val",
        "tenant": "acme",
    }


def test_sync_sleep_for_suspends_then_resumes_replaying_step() -> None:
    call_command("absurd_sync_queues")
    SYNC_STEP_CALLS["n"] = 0
    result = ssleep_for_once.enqueue("k")

    run_absurd_worker()
    suspended = get_task_result(result.id)
    assert suspended is not None
    assert suspended.state == "sleeping"

    time.sleep(2)
    run_absurd_worker()
    done = get_task_result(result.id)
    assert done is not None
    assert done.state == "completed"
    assert done.result == 1
    assert SYNC_STEP_CALLS["n"] == 1


def test_sync_sleep_until_suspends_then_resumes() -> None:
    call_command("absurd_sync_queues")
    result = ssleep_until_once.enqueue("k")
    run_absurd_worker()
    suspended = get_task_result(result.id)
    assert suspended is not None
    assert suspended.state == "sleeping"
    time.sleep(2)
    run_absurd_worker()
    done = get_task_result(result.id)
    assert done is not None
    assert done.state == "completed"
    assert done.result == "woke"


def test_suspend_logged_as_lifecycle_not_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    call_command("absurd_sync_queues")
    DURABLE_STEP_CALLS["n"] = 0
    asleep_for_once.enqueue("k")
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        run_absurd_worker()
    assert (
        "django-absurd task suspended: name=tests.atasks.asleep_for_once" in caplog.text
    )
    assert "task failed" not in caplog.text
