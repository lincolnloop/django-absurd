"""Subject adapters for the validator tests: run a case through a real enforcing
entrypoint and return the emitted error text (or None)."""

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import SystemCheckError

from django_absurd.pg_cron.models import ScheduledTask

BACKEND = "django_absurd.backends.AbsurdBackend"
QUEUES: dict = {"default": {}, "other": {}, "reports": {}}

# Valid baseline: every field passes, so a single override isolates one rule.
VALID = {
    "source": "admin",
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
