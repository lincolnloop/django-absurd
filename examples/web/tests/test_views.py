"""High-level HTTP tests for the web demo — drive the real nanodjango views through
the Django test client, asserting observable responses. The `add` task and the
`fulfill_order` workflow are exercised end-to-end via `absurd_drain_queue`."""

import typing as t
import uuid

import pytest
from django.test import Client


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
    absurd_drain_queue: t.Callable[..., None], client: Client
) -> None:
    resp = client.post("/", {"a": "2", "b": "3"})
    assert resp.status_code == 302
    task_url = resp.headers["Location"]
    absurd_drain_queue()
    body = client.get(task_url).content.decode()
    assert "SUCCESSFUL" in body
    assert "5.0" in body


@pytest.mark.django_db(transaction=True)
def test_add_fails_on_non_numeric_input(
    absurd_drain_queue: t.Callable[..., None], client: Client
) -> None:
    resp = client.post("/", {"a": "x", "b": "3"})
    task_url = resp.headers["Location"]
    absurd_drain_queue()
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
    absurd_drain_queue: t.Callable[..., None], client: Client
) -> None:
    resp = client.post("/workflow/", {"order": "order-7"})
    assert resp.status_code == 302
    task_url = resp.headers["Location"]

    absurd_drain_queue()  # charge + reserve, then suspends on await_event
    waiting = client.get(task_url).content.decode()
    assert "Working" in waiting
    assert 'action="/workflow/order-7/pack/' in waiting  # pack button present

    pack = client.post("/workflow/order-7/pack/?next=/")
    assert pack.status_code == 302

    absurd_drain_queue()  # event delivered → resumes, runs notify, completes
    done = client.get(task_url).content.decode()
    assert "SUCCESSFUL" in done
    assert "notified: order-7" in done


@pytest.mark.django_db(transaction=True)
def test_pack_view_get_does_not_emit_the_event(
    absurd_drain_queue: t.Callable[..., None], client: Client
) -> None:
    task_url = client.post("/workflow/", {"order": "order-get"}).headers["Location"]
    absurd_drain_queue()  # suspends on await_event

    resp = client.get("/workflow/order-get/pack/")  # GET must NOT emit
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"  # default next

    absurd_drain_queue()
    body = client.get(task_url).content.decode()
    assert "Working" in body  # still suspended — the GET delivered no event
