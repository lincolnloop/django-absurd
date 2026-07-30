"""High-level HTTP tests for the web demo — drive the real nanodjango views through
the Django test client, asserting observable responses. The `add` task and the
`fulfill_order` workflow are exercised end-to-end via the `dj_absurd` fixture's
`drain()`."""

import uuid

import pytest
from django.test import Client

from django_absurd.test import AbsurdTestRuntime


def test_index_get_renders_the_add_form(client: Client) -> None:
    body = client.get("/").content.decode()
    assert "django-absurd demo" in body
    assert 'name="a"' in body
    assert 'name="b"' in body


def test_index_post_invalid_rerenders_the_form(client: Client) -> None:
    resp = client.post("/", {"a": "1"})  # missing b → form invalid
    assert resp.status_code == 200
    assert "This field is required." in resp.content.decode()


@pytest.mark.django_db(transaction=True)
def test_add_succeeds_and_the_result_page_shows_the_value(
    client: Client, dj_absurd: AbsurdTestRuntime
) -> None:
    resp = client.post("/", {"a": "2", "b": "3"})
    assert resp.status_code == 302
    task_url = resp.headers["Location"]
    assert [run.state for run in dj_absurd.drain()] == ["completed"]
    body = client.get(task_url).content.decode()
    assert "SUCCESSFUL" in body
    assert "5.0" in body


@pytest.mark.django_db(transaction=True)
def test_add_fails_on_non_numeric_input(
    client: Client, dj_absurd: AbsurdTestRuntime
) -> None:
    resp = client.post("/", {"a": "x", "b": "3"})
    task_url = resp.headers["Location"]
    # Every attempt of the default retry burn is drained, and each one fails.
    assert {run.state for run in dj_absurd.drain()} == {"failed"}
    body = client.get(task_url).content.decode()
    assert "FAILED" in body
    assert "Failed:" in body


@pytest.mark.django_db
def test_task_detail_shows_working_before_the_task_runs(client: Client) -> None:
    resp = client.post("/", {"a": "2", "b": "3"})
    body = client.get(resp.headers["Location"]).content.decode()
    assert "Working" in body
    assert 'http-equiv="refresh"' in body


@pytest.mark.django_db
def test_task_detail_unknown_id_returns_404(client: Client) -> None:
    resp = client.get(f"/tasks/default:{uuid.uuid4()}/")
    assert resp.status_code == 404
    assert "Unknown task" in resp.content.decode()


def test_workflow_get_renders_the_form(client: Client) -> None:
    body = client.get("/workflow/").content.decode()
    assert "Order-fulfillment workflow" in body
    assert 'name="order"' in body


def test_workflow_post_invalid_rerenders_the_form(client: Client) -> None:
    resp = client.post("/workflow/", {"order": ""})
    assert resp.status_code == 200
    assert "This field is required." in resp.content.decode()


@pytest.mark.django_db(transaction=True)
def test_workflow_suspends_on_event_then_completes_after_pack(
    client: Client, dj_absurd: AbsurdTestRuntime
) -> None:
    resp = client.post("/workflow/", {"order": "order-7"})
    assert resp.status_code == 302
    task_url = resp.headers["Location"]

    # charge + reserve, then suspends on await_event
    assert [run.state for run in dj_absurd.drain()] == ["sleeping"]
    waiting = client.get(task_url).content.decode()
    assert "Working" in waiting
    assert 'action="/workflow/order-7/pack/' in waiting  # pack button present

    pack = client.post("/workflow/order-7/pack/?next=/")
    assert pack.status_code == 302

    # event delivered → resumes, runs notify, completes
    assert [run.state for run in dj_absurd.drain()] == ["completed"]
    done = client.get(task_url).content.decode()
    assert "SUCCESSFUL" in done
    assert "notified: order-7" in done


@pytest.mark.django_db(transaction=True)
def test_pack_view_get_does_not_emit_the_event(
    client: Client, dj_absurd: AbsurdTestRuntime
) -> None:
    task_url = client.post("/workflow/", {"order": "order-get"}).headers["Location"]
    assert [run.state for run in dj_absurd.drain()] == ["sleeping"]

    resp = client.get("/workflow/order-get/pack/")  # GET must NOT emit
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"  # default next

    assert dj_absurd.drain() == []  # nothing claimable: no event woke the waiter
    body = client.get(task_url).content.decode()
    assert "Working" in body  # still suspended — the GET delivered no event
