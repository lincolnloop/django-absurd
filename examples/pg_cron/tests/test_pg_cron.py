"""Tests for the pg_cron demo: the index redirect, and the scheduled `ping` task run
end-to-end through the `dj_absurd` fixture's `drain()`. pg_cron's own firing
(Postgres-side, cadence-driven) isn't exercised here — we drive the task pg_cron would
enqueue."""

import pytest
from app import ping
from django.test import Client

from django_absurd.test import AbsurdTestRuntime


def test_index_redirects_to_admin(client: Client) -> None:
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/admin/"


@pytest.mark.django_db(transaction=True)
def test_ping_task_runs_via_drain(dj_absurd: AbsurdTestRuntime) -> None:
    result = ping.enqueue()
    assert [run.state for run in dj_absurd.drain()] == ["completed"]
    result.refresh()
    assert result.status == "SUCCESSFUL"
    assert result.return_value == "pong"
