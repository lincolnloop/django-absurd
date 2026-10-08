"""The durable-sleep feature itself: each context.sleep_for is a real
suspension. dj_absurd.freeze_time lets the test move Postgres's own clock
forward instead of actually waiting wait_seconds, per AGENTS.md's "Move
durable time" — enter the block before enqueuing so the deadlines it sets are
controlled by the same clock the shifts move."""

import datetime as dt

import pytest
from django.test import Client

from django_absurd.test import AbsurdTestRuntime


@pytest.mark.django_db(transaction=True)
def test_sequence_sleeps_between_each_message_then_completes(
    client: Client, dj_absurd: AbsurdTestRuntime
) -> None:
    with dj_absurd.freeze_time() as frozen_time:
        resp = client.post("/", {"email": "a@example.com", "wait_seconds": "60"})
        assert resp.status_code == 302
        task_url = resp.headers["Location"]

        # welcome sends, then sleeps for cooldown-1
        assert [run.state for run in dj_absurd.drain()] == ["sleeping"]
        body = client.get(task_url).content.decode()
        assert "welcome" in body
        assert "sent" in body
        assert "asleep" in body

        frozen_time.shift(dt.timedelta(seconds=60))

        # cooldown-1 wakes, tip sends, then sleeps for cooldown-2
        assert [run.state for run in dj_absurd.drain()] == ["sleeping"]
        body = client.get(task_url).content.decode()
        assert body.count("woke up") == 1  # cooldown-1 only
        assert "asleep" in body  # cooldown-2, now

        frozen_time.shift(dt.timedelta(seconds=60))

        # cooldown-2 wakes, nudge sends, sequence completes
        assert [run.state for run in dj_absurd.drain()] == ["completed"]
        body = client.get(task_url).content.decode()
        assert ">SUCCESSFUL<" in body
        assert "nudge email sent to a@example.com" in body
        assert "asleep" not in body


@pytest.mark.django_db(transaction=True)
def test_a_short_sleep_actually_suspends_the_run(
    client: Client, dj_absurd: AbsurdTestRuntime
) -> None:
    """Without moving time forward, drain() stops at the first sleep — it
    does not busy-wait or run ahead of the clock."""
    client.post("/", {"email": "b@example.com", "wait_seconds": "3600"})
    assert [run.state for run in dj_absurd.drain()] == ["sleeping"]
    assert dj_absurd.drain() == []  # nothing newly claimable: still asleep
