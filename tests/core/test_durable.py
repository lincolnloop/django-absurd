import pytest
from django.core.management import call_command

from django_absurd.params import AbsurdSpawnParams
from tests.atasks import aheaders_tenant, aheartbeat_then_return, astep_echo
from tests.worker_support import get_task_result, run_absurd_worker

pytestmark = pytest.mark.django_db(transaction=True)


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
