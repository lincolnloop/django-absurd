"""Single-file nanodjango demo: django-absurd durable sleep.

An onboarding email sequence that checkpoints each message and calls
``context.sleep_for`` between them. The sleeps are real suspensions — the
worker holds no thread, no timer, no in-memory state while a sequence is
"asleep"; the wake time lives in Postgres. Try it: start a sequence, then
while it's asleep run ``docker compose restart worker`` — it resumes on
schedule once the worker is back, from wherever it left off.

    docker compose up
    http://localhost:8000/         start a sequence
    http://localhost:8000/admin/   Tasks / Runs / Checkpoints / … (admin / admin)

psycopg (v3) backend required — DATABASES is overridden (nanodjango defaults to sqlite).
"""

import os

import dj_database_url
from django import forms
from django.contrib.admin.utils import quote
from django.http import HttpRequest, HttpResponse
from django.middleware.csrf import get_token
from django.shortcuts import redirect
from django.tasks import TaskResultStatus, default_task_backend, task
from django.tasks.exceptions import TaskResultDoesNotExist
from django.urls import reverse
from nanodjango import Django

from django_absurd import get_absurd_context

app = Django(
    ADMIN_URL="admin/",
    EXTRA_APPS=["django_absurd"],
    # Managed platforms inject DATABASE_URL and rotate the credentials inside it, so
    # PG* vars would go stale.
    DATABASES={
        "default": dj_database_url.parse(
            os.environ.get(
                "DATABASE_URL", "postgres://postgres:postgres@localhost:5432/postgres"
            )
        )
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

# Settings must be configured (done above, by constructing `app`) before any Django
# model class — including django_absurd's — can be imported.
from django_absurd.models import Checkpoint, Run  # noqa: E402

# Sleeps share the checkpoint namespace with steps, so this doubles as the
# Checkpoint rows we look for when rendering the timeline.
SEQUENCE_STEPS = ("welcome", "cooldown-1", "tip", "cooldown-2", "nudge")


@task
def send_onboarding_sequence(email: str, wait_seconds: int) -> str:
    """Three-message onboarding drip, durably slept between each send.

    Each message is a checkpoint; each gap is ``context.sleep_for``. A crash or
    worker restart mid-gap loses nothing — Absurd replays from the last
    checkpoint and resumes the sleep from the wake time Postgres already has.
    """
    context = get_absurd_context()
    context.step("welcome", lambda: f"welcome email sent to {email}")
    context.sleep_for("cooldown-1", wait_seconds)
    context.step("tip", lambda: f"onboarding tip sent to {email}")
    context.sleep_for("cooldown-2", wait_seconds)

    @context.run_step("nudge")
    def nudge() -> str:
        return f"nudge email sent to {email}"

    return nudge


class SequenceForm(forms.Form):
    email = forms.EmailField(label="Email", initial="new-user@example.com")
    wait_seconds = forms.IntegerField(
        label="Wait between messages (seconds)",
        initial=15,
        min_value=1,
        max_value=3600,
    )


@app.route("/")
def index(request: HttpRequest) -> HttpResponse | str:
    if request.method == "POST":
        form = SequenceForm(request.POST)
        if form.is_valid():
            result = send_onboarding_sequence.enqueue(**form.cleaned_data)
            return redirect(f"/sequence/{result.id}/")
    else:
        form = SequenceForm()
    return shell(f"""
        <h1>Durable sleep demo</h1>
        <p>
          Starts an onboarding sequence: <strong>welcome</strong> →
          <em>sleep</em> → <strong>tip</strong> → <em>sleep</em> →
          <strong>nudge</strong>. Each sleep is <code>context.sleep_for</code> —
          a real suspension, not a busy wait.
        </p>
        <form method="post">
          <input type="hidden" name="csrfmiddlewaretoken" value="{get_token(request)}">
          {form.as_p()}
          <button type="submit">Start sequence</button>
        </form>
        <p><a href="/admin/">Browse the queues in the admin</a> (admin / admin)</p>
    """)


@app.route("/sequence/<str:result_id>/")
def sequence_detail(request: HttpRequest, result_id: str) -> HttpResponse | str:
    try:
        result = default_task_backend.get_result(result_id)
    except TaskResultDoesNotExist:
        return HttpResponse(shell(f"<h1>Unknown sequence {result_id}</h1>"), status=404)

    queue, task_id = result_id.split(":", 1)
    finished = result.status in (TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED)
    refresh = "" if finished else '<meta http-equiv="refresh" content="2">'

    # A sleep's checkpoint commits the instant sleep_for is CALLED (so a replay never
    # re-enters it), not when it wakes — so checkpoints alone read as a prefix of
    # SEQUENCE_STEPS that's already one step ahead of what's actually finished sending.
    # The step actually in progress is the last checkpointed one, as long as the task
    # hasn't finished; the live wake time for it lives on the Run row Absurd parks at
    # "sleeping" — sleep_for has no Wait row, unlike await_event.
    done_in_order = [
        name
        for name in SEQUENCE_STEPS
        if Checkpoint.objects.filter(
            queue=queue, task_id=task_id, checkpoint_name=name
        ).exists()
    ]
    last_done = done_in_order[-1] if done_in_order else None
    sleeping_step = (
        last_done if last_done and not finished and "cooldown" in last_done else None
    )
    wake_at = None
    if sleeping_step:
        run = Run.objects.filter(queue=queue, task_id=task_id, state="sleeping").first()
        wake_at = run.available_at if run else None

    rows = ""
    for name in SEQUENCE_STEPS:
        if name == sleeping_step:
            wake = wake_at.strftime("%H:%M:%S") if wake_at else "?"
            state, label = "sleeping", f"asleep — wakes at {wake}"
        elif name in done_in_order:
            state, label = "done", "sent" if "cooldown" not in name else "woke up"
        else:
            state, label = "pending", "not yet"
        rows += (
            f'<li class="{state}"><span class="badge {state}">{name}</span>{label}</li>'
        )

    body = ""
    if result.status == TaskResultStatus.SUCCESSFUL:
        body = f"<p>Result: <strong>{result.return_value}</strong></p>"
    elif result.status == TaskResultStatus.FAILED:
        body = f"<p>Failed: {result.errors}</p>"

    admin_url = reverse("admin:django_absurd_task_change", args=[quote(result.id)])
    return shell(f"""
        {refresh}
        <h1>Sequence {result.id}</h1>
        <p>Status:
          <strong class="{result.status.name}">{result.status.name}</strong></p>
        <ul class="timeline">{rows}</ul>
        {body}
        <p>
          <a href="/">Start another</a> ·
          <a href="{admin_url}">View this task in the admin</a>
          — its Checkpoints and Runs inlines show the same rows this page reads.
        </p>
    """)


STYLE = """
<style>
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    max-width: 640px; margin: 3rem auto; padding: 0 1rem; line-height: 1.5;
    color: #1f2937; background: #ffffff;
  }
  h1 { font-size: 1.4rem; }
  code { font-family: ui-monospace, "SF Mono", Menlo, monospace; background: #f3f4f6;
         padding: 0.1rem 0.3rem; border-radius: 4px; }
  form p { margin: 0.75rem 0; }
  label { display: block; font-weight: 600; margin-bottom: 0.25rem; }
  input { width: 100%; padding: 0.4rem; box-sizing: border-box;
          border: 1px solid #d1d5db; border-radius: 4px; }
  button { padding: 0.5rem 1.1rem; border: none; border-radius: 6px;
           background: #2563eb; color: white; font-weight: 600; cursor: pointer; }
  button:hover { background: #1d4ed8; }
  a { color: #2563eb; }
  .timeline { list-style: none; padding: 0; margin: 1.5rem 0; }
  .timeline li { padding: 0.6rem 0.8rem; margin-bottom: 0.4rem; border-radius: 6px;
                 border: 1px solid #e5e7eb; }
  .timeline li.done { background: #ecfdf5; border-color: #a7f3d0; }
  .timeline li.sleeping { background: #eff6ff; border-color: #bfdbfe; }
  .timeline li.pending { color: #9ca3af; }
  .badge { display: inline-block; font-size: 0.72rem; font-weight: 700;
           padding: 0.1rem 0.5rem; border-radius: 999px; margin-right: 0.6rem; }
  .timeline li.done .badge { background: #10b981; color: white; }
  .timeline li.sleeping .badge { background: #3b82f6; color: white; }
  .timeline li.pending .badge { background: #e5e7eb; color: #6b7280; }
  .SUCCESSFUL { color: #059669; }
  .FAILED { color: #dc2626; }
</style>
"""


def shell(body: str) -> str:
    return f"<!doctype html><html><head>{STYLE}</head><body>{body}</body></html>"


if __name__ == "__main__":  # pragma: no cover
    app.run()
