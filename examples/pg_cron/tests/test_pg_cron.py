"""Tests for the pg_cron demo: the index redirect, and the scheduled `ping` task run
end-to-end through `absurd_drain_queue`. pg_cron's own firing (Postgres-side, cadence-
driven) isn't exercised here — we drive the task pg_cron would enqueue."""

import typing as t

import pytest
from app import ping
from django.test import Client


def test_index_redirects_to_admin(client: Client) -> None:
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/admin/"


@pytest.mark.django_db(transaction=True)
def test_ping_task_runs_via_drain(
    absurd_drain_queue: t.Callable[..., None],
) -> None:
    result = ping.enqueue()
    absurd_drain_queue()
    result.refresh()
    assert result.status == "SUCCESSFUL"
    assert result.return_value == "pong"
