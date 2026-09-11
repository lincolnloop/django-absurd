"""High-level HTTP tests for the sleep demo — drive the real nanodjango views
through the Django test client. The durable sleep itself (freeze_time + drain
across each sleep_for) is exercised in test_sequence.py."""

import uuid

import pytest
from django.test import Client


def test_index_get_renders_the_form(client: Client) -> None:
    body = client.get("/").content.decode()
    assert "Durable sleep demo" in body
    assert 'name="email"' in body
    assert 'name="wait_seconds"' in body


def test_index_post_invalid_rerenders_the_form(client: Client) -> None:
    resp = client.post("/", {"email": "not-an-email"})  # missing wait_seconds too
    assert resp.status_code == 200
    assert "This field is required." in resp.content.decode()


@pytest.mark.django_db
def test_sequence_detail_unknown_id_returns_404(client: Client) -> None:
    resp = client.get(f"/sequence/default:{uuid.uuid4()}/")
    assert resp.status_code == 404
    assert "Unknown sequence" in resp.content.decode()


@pytest.mark.django_db
def test_sequence_detail_shows_the_timeline_before_anything_runs(
    client: Client,
) -> None:
    resp = client.post("/", {"email": "a@example.com", "wait_seconds": "30"})
    body = client.get(resp.headers["Location"]).content.decode()
    assert 'http-equiv="refresh"' in body
    assert ">READY<" in body
    # nothing has run yet: every step reads "not yet", nothing "sent" or "asleep"
    assert body.count("not yet") == 5
    assert "asleep" not in body
