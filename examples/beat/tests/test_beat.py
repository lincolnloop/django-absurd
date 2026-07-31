"""Tests for the beat demo: the index redirect, and the scheduled `tick` task run
end-to-end through the `dj_absurd` fixture's `drain()` (the beat process itself isn't
exercised — its cadence is time-driven; here we drive the task the beat would
enqueue)."""

import pytest
from app import tick
from django.test import Client

from django_absurd.test import AbsurdTestRuntime


def test_index_redirects_to_admin(client: Client) -> None:
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/admin/"


@pytest.mark.django_db(transaction=True)
def test_tick_task_runs_via_drain(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    result = tick.enqueue()
    with caplog.at_level("INFO", logger="demo"):
        assert [run.state for run in dj_absurd.drain()] == ["completed"]
    result.refresh()
    assert result.status == "SUCCESSFUL"
    assert result.return_value is None
    assert any("tock" in record.message for record in caplog.records)
