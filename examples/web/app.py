"""Single-file nanodjango demo: django-absurd enqueue + result.

Enqueue add(a, b) from a form; the worker runs it; watch the result page and
browse the read-only queue tables in the admin (auto-registered by django-absurd).

Also demonstrates Steps (checkpoints) + Sleep: enqueue an order-fulfillment
workflow that checkpoints each step and suspends between them.

    docker compose up
    http://localhost:8000/         enqueue add(a, b) or the order workflow
    http://localhost:8000/admin/   Tasks / Runs / Checkpoints / … (admin / admin)

psycopg (v3) backend required — DATABASES is overridden (nanodjango defaults to sqlite).
"""

import dataclasses
import html
import logging
import os
import pprint

from django import forms
from django.http import HttpRequest, HttpResponse
from django.middleware.csrf import get_token
from django.shortcuts import redirect
from django.tasks import TaskResultStatus, default_task_backend, task
from django.tasks.exceptions import TaskResultDoesNotExist
from nanodjango import Django

from django_absurd import get_absurd_context

app = Django(
    ADMIN_URL="admin/",
    EXTRA_APPS=["django_absurd"],
    DATABASES={
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("PGDATABASE", "postgres"),
            "USER": os.environ.get("PGUSER", "postgres"),
            "PASSWORD": os.environ.get("PGPASSWORD", "postgres"),
            "HOST": os.environ.get("PGHOST", "localhost"),
            "PORT": os.environ.get("PGPORT", "5432"),
        }
    },
    TASKS={
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"QUEUES": {"default": {}}},
        }
    },
    LOGGING={
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {"console": {"class": "logging.StreamHandler"}},
        "loggers": {"django_absurd": {"handlers": ["console"], "level": "INFO"}},
    },
)

logger = logging.getLogger("demo")


@task
def add(a: str, b: str) -> float:
    """Runs in the worker. Coerces here so non-numeric input FAILS the task
    (rather than being rejected up front)."""
    return float(a) + float(b)


@task
def fulfill_order(order: str) -> str:
    """Order-fulfillment workflow: charge, reserve inventory, wait, notify.

    Mirrors the shape of Absurd's headline order-fulfillment example
    (https://github.com/earendil-works/absurd#readme): step(charge) →
    step(reserve inventory) → wait for the warehouse to pack → step(notify).
    Absurd's example awaits a "warehouse packed" event; Events aren't shipped
    here yet, so `sleep_for` stands in for that wait — it becomes
    `await_event("warehouse.packed")` once the Events pillar lands.

    Each step is a checkpoint: check the admin's Checkpoints and Runs pages to
    see the steps and the suspended state while it waits.

    Shows both step forms: ``context.step(name, fn)`` and the ``run_step``
    decorator (sync only), which runs the function once and replaces it with the
    step's return value.
    """
    context = get_absurd_context()
    context.step("charge", lambda: f"charged: {order}")
    context.step("reserve-inventory", lambda: f"reserved: {order}")
    context.sleep_for("await-warehouse", 5)

    @context.run_step("notify")
    def notify() -> str:
        return f"notified: {order}"

    return notify


class AddForm(forms.Form):
    a = forms.CharField(label="A")
    b = forms.CharField(label="B")


class WorkflowForm(forms.Form):
    order = forms.CharField(label="Order", initial="order-42")


@app.route("/")
def index(request: HttpRequest) -> HttpResponse | str:
    if request.method == "POST":
        form = AddForm(request.POST)
        if form.is_valid():
            result = add.enqueue(**form.cleaned_data)
            return redirect(f"/tasks/{result.id}/")
    else:
        form = AddForm()
    return f"""
        <h1>django-absurd demo</h1>
        <p>Enqueue <code>add(a, b)</code>; the worker runs it.</p>
        <form method="post">
          <input type="hidden" name="csrfmiddlewaretoken" value="{get_token(request)}">
          {form.as_p()}
          <button type="submit">Add</button>
        </form>
        <p>
          <a href="/workflow/">Try the order-fulfillment workflow</a>
          — checkpointed steps with a wait between them.
        </p>
        <p><a href="/admin/">Browse the queues in the admin</a> (admin / admin)</p>
    """


@app.route("/workflow/")
def workflow_view(request: HttpRequest) -> HttpResponse | str:
    if request.method == "POST":
        form = WorkflowForm(request.POST)
        if form.is_valid():
            result = fulfill_order.enqueue(**form.cleaned_data)
            return redirect(f"/tasks/{result.id}/")
    else:
        form = WorkflowForm()
    return f"""
        <h1>Order-fulfillment workflow</h1>
        <p>
          Mirrors Absurd's
          <a href="https://github.com/earendil-works/absurd#readme">order-fulfillment
          example</a>: <em>charge</em>, <em>reserve-inventory</em>, wait for the
          warehouse (a 5s sleep standing in for a "warehouse packed" event),
          <em>notify</em>. While waiting, check
          <a href="/admin/django_absurd/run/">Runs</a> and
          <a href="/admin/django_absurd/checkpoint/">Checkpoints</a> in the admin.
        </p>
        <form method="post">
          <input type="hidden" name="csrfmiddlewaretoken" value="{get_token(request)}">
          {form.as_p()}
          <button type="submit">Run workflow</button>
        </form>
        <p><a href="/">Back</a></p>
    """


@app.route("/tasks/<str:result_id>/")
def task_detail(request: HttpRequest, result_id: str) -> HttpResponse | str:
    try:
        result = default_task_backend.get_result(result_id)
    except TaskResultDoesNotExist:
        return HttpResponse(f"<h1>Unknown task {result_id}</h1>", status=404)

    finished = result.status in (TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED)
    refresh = "" if finished else '<meta http-equiv="refresh" content="1">'
    if result.status == TaskResultStatus.SUCCESSFUL:
        body = f"<p>Result: <strong>{result.return_value}</strong></p>"
    elif result.status == TaskResultStatus.FAILED:
        body = f"<p>Failed: {result.errors}</p>"
    else:
        body = "<p>Working… (auto-refreshing)</p>"

    fields = {f.name: getattr(result, f.name) for f in dataclasses.fields(result)}
    dump = html.escape(pprint.pformat(fields))
    return f"""
        {refresh}
        <h1>Task {result.id}</h1>
        <p>Status: <strong>{result.status.name}</strong></p>
        {body}
        <pre><code>{dump}</code></pre>
        <p><a href="/">Add another</a> · <a href="/admin/">Admin</a></p>
    """


if __name__ == "__main__":
    app.run()
