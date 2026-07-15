"""Subject adapters for the validator tests: run a case through a real enforcing
entrypoint and return the emitted error text (or None)."""

import json

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.urls import reverse

from django_absurd.pg_cron.models import ScheduledTask


def get_change_url(pk):
    return reverse("admin:django_absurd_pg_cron_scheduledtask_change", args=[pk])


BACKEND = "django_absurd.backends.AbsurdBackend"
QUEUES: dict = {"default": {}, "other": {}, "reports": {}}

# Valid baseline: every field passes, so a single override isolates one rule.
VALID = {
    "source": "a",
    "alias": "default",
    "name": "ok",
    "task": "tests.tasks.add",
    "queue": "",
    "args": [],
    "kwargs": {},
    "cron": "0 2 * * *",
    "enabled": True,
}


def configure_pg_cron_backend(settings, schedule=None):
    """A pg_cron 'default' backend so model clean() resolves it (declared queues,
    alias-is-pg_cron-backend), and the check has a SCHEDULE to validate."""
    settings.TASKS = {
        "default": {
            "BACKEND": BACKEND,
            "OPTIONS": {
                "QUEUES": QUEUES,
                "SCHEDULER": "pg_cron",
                "SCHEDULE": schedule or {},
            },
        }
    }


def clean_scheduled_task(**kwargs):
    """Run ScheduledTask.full_clean() over the baseline + overrides. Return joined
    error text or None. Does NOT configure settings — callers needing a specific
    TASKS layout (e.g. a non-pg_cron backend) set it first."""
    try:
        ScheduledTask(**{**VALID, **kwargs}).full_clean()
    except ValidationError as exc:
        return " ".join(m for msgs in exc.message_dict.values() for m in msgs)
    return None


def validate_from_model(settings, **kwargs):
    """Subject: ScheduledTask.full_clean(). Return joined error text or None."""
    configure_pg_cron_backend(settings)
    return clean_scheduled_task(**kwargs)


def validate_from_system_check(settings, capsys, **kwargs):
    """Subject: the system check over a pg_cron SCHEDULE. Return captured text or None."""
    fields = {**VALID, **kwargs}
    entry = {"cron": fields["cron"], "task": fields["task"]}
    # omit empty optionals so the baseline is clean (a literal queue="" would read
    # as an undeclared queue; empty args/kwargs are the defaults)
    for key in ("args", "kwargs", "queue"):
        if fields[key]:
            entry[key] = fields[key]
    configure_pg_cron_backend(settings, {fields["name"]: entry})
    try:
        call_command("check", "django_absurd")
    except SystemCheckError as exc:
        cap = capsys.readouterr()
        return cap.out + cap.err + str(exc)
    cap = capsys.readouterr()
    out = cap.out + cap.err
    return out if "absurd.E007" in out else None


def validate_from_admin_post(client, admin_user, settings, **kwargs):
    """Subject: the admin change-form POST over a pre-seeded admin row. Return the
    joined form-error text, or None when the form validates (the POST redirects).

    The two-step create form collects only identity + cron and resolves every spawn
    column from the task, so it can't express rules about args/kwargs/queue/retry/
    cancellation. The change form exposes those fields, so drive validation there:
    seed a baseline admin row, then POST the overrides to its editable fields. alias
    and name are read-only on the change form; rules on those move to the
    check + model subjects."""
    configure_pg_cron_backend(settings)
    client.force_login(admin_user)
    fields = {**VALID, **kwargs}
    scheduled_task = ScheduledTask.objects.create(
        source="a",
        alias=VALID["alias"],
        name=VALID["name"],
        task=VALID["task"],
        queue="default",
        cron=VALID["cron"],
    )
    payload = {
        "task": fields["task"],
        "queue": fields["queue"],
        "cron": fields["cron"],
        "enabled": "on",
        "args": json.dumps(fields["args"]),
        "kwargs": json.dumps(fields["kwargs"]),
        "max_attempts": "",
        "retry_kind": "",
        "retry_base_seconds": "",
        "retry_factor": "",
        "retry_max_seconds": "",
        "headers": "",
        "cancellation_max_duration": "",
        "cancellation_max_delay": "",
        "idempotency_key": "",
    }
    response = client.post(get_change_url(scheduled_task.pk), payload)
    if response.status_code == 302:
        return None
    form = response.context["adminform"].form
    return " ".join(m for messages in form.errors.values() for m in messages)
