"""Tests for the beat demo: the index redirect, and the scheduled `tick` task run
end-to-end through `absurd_drain_queue` (the beat process itself isn't exercised —
its cadence is time-driven; here we drive the task the beat would enqueue)."""

import typing as t

import pytest
from app import tick
from django.test import Client


def test_index_redirects_to_admin(client: Client) -> None:
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/admin/"


@pytest.mark.django_db(transaction=True)
def test_tick_task_runs_via_drain(
    absurd_drain_queue: t.Callable[..., None], caplog: pytest.LogCaptureFixture
) -> None:
    result = tick.enqueue()
    with caplog.at_level("INFO", logger="demo"):
        absurd_drain_queue()
    result.refresh()
    assert result.status == "SUCCESSFUL"
    assert result.return_value is None
    assert any("tock" in record.message for record in caplog.records)
