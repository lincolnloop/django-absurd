"""The ``dj_absurd`` facade driven from ``async def`` tests — same names, no ``await``.

Parity, not new behavior: every test here has a sync counterpart elsewhere in this
suite (``test_durable_clock.py``, ``test_events.py``, ``test_absurd_fixture.py``), and
the point of the duplication is that only the ``async def`` differs. The loop is what
matters, not the plugin that starts one, so nothing in ``django_absurd`` knows
pytest-asyncio exists.
"""

import datetime as dt
import typing as t

import pytest

from django_absurd.test import AbsurdTestRuntime
from tests import atasks
from tests.models import Payload

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]

FROZEN = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)


async def test_a_week_long_sleep_resumes_after_shifting_a_week(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    atasks.DURABLE_STEP_CALLS["n"] = 0

    with dj_absurd.freeze_time(FROZEN) as frozen_time:
        await atasks.asleep_for_once.aenqueue("k")
        assert [run.state for run in dj_absurd.drain()] == ["sleeping"]

        frozen_time.shift(dt.timedelta(days=8))

        assert dj_absurd.now == FROZEN + dt.timedelta(days=8)
        assert [(run.state, run.result) for run in dj_absurd.drain()] == [
            ("completed", 1)
        ]


async def test_an_emitted_event_resolves_an_await_event_waiter(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    await atasks.aawait_event_once.aenqueue("order.packed:async-test")
    assert [run.state for run in dj_absurd.drain()] == ["sleeping"]

    dj_absurd.emit("order.packed:async-test", {"shipped": True})

    assert [(run.state, run.result) for run in dj_absurd.drain()] == [
        ("completed", {"shipped": True})
    ]


async def test_get_result_reports_a_completed_task(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    result = await atasks.aecho.aenqueue("round-trip")

    dj_absurd.drain()

    snapshot = dj_absurd.get_result(result.id)
    assert snapshot.task_name == "tests.atasks.aecho"
    assert snapshot.args == ["round-trip"]
    assert snapshot.state == "completed"
    assert snapshot.result == "round-trip"


async def test_sync_queues_reprovisions_before_a_task_writes_a_row(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()

    result = await atasks.acreate_payload.aenqueue({"async": True})
    dj_absurd.drain()

    pk = t.cast("int", dj_absurd.get_result(result.id).result)
    assert (await Payload.objects.aget(pk=pk)).data == {"async": True}
