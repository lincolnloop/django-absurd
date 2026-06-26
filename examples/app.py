"""Single-file nanodjango demo for django-absurd.

django-absurd plugs Absurd — a Postgres-native workflow engine — into Django's
TASKS framework. This app enqueues a task from a web form, the worker process
runs it, and the queue tables are exposed read-only through the Django admin
(auto-registered by django-absurd) at /admin/.

One command:  docker compose up
Then open:
    http://localhost:8000/         enqueue add(a, b)
    http://localhost:8000/admin/   browse Tasks / Runs / Checkpoints / Events /
                                   Waits / Queues  (superuser: admin / admin)

HARD REQUIREMENT: PostgreSQL via the psycopg (v3) backend. The Absurd SDK reuses
Django's connection and will not work on sqlite or psycopg2; nanodjango defaults
to sqlite, so DATABASES is overridden below.
"""

import dataclasses
import html
import os
import pprint

from django import forms
from django.http import HttpRequest, HttpResponse
from django.middleware.csrf import get_token
from django.shortcuts import redirect
from django.tasks import TaskResultStatus, default_task_backend, task
from django.tasks.exceptions import TaskResultDoesNotExist
from nanodjango import Django

app = Django(
    # Mounting the admin at /admin/ is all django-absurd needs: it auto-registers
    # its read-only queue models on the admin site when django.contrib.admin is
    # installed (nanodjango installs it by default).
    ADMIN_URL="admin/",
    EXTRA_APPS=["django_absurd"],
    DATABASES={
        "default": {
            "ENGINE": "django.db.backends.postgresql",  # psycopg3
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
            "QUEUES": ["default"],
        }
    },
    # Surface django-absurd's per-task worker logging on the console.
    LOGGING={
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {"console": {"class": "logging.StreamHandler"}},
        "loggers": {"django_absurd": {"handlers": ["console"], "level": "INFO"}},
    },
)


@task
def add(a: str, b: str) -> float:
    """Run in the worker, not the web request. Coerces here so non-numeric input
    fails the task (-> FAILED) rather than being rejected up front."""
    return float(a) + float(b)


class AddForm(forms.Form):
    a = forms.CharField(label="A")
    b = forms.CharField(label="B")


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
        <p><a href="/admin/">Browse the queues in the admin</a> (admin / admin)</p>
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
