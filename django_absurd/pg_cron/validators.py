"""Shared schedule validators — the single source of rule truth.

Each raises `django.core.exceptions.ValidationError`. `ScheduledTask.clean()` +
field `validators=[...]` enforce them model-first; the system checks call the
same callables and wrap failures into `absurd.E007`. The field-level validators
(name/alias charset, jobname length) are pure; the contextual ones
(`validate_alias_is_pg_cron_backend`, `validate_pg_cron_cron`) read settings /
the database.
"""

import typing as t
import uuid

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import DatabaseError, connections, transaction
from django.utils.module_loading import import_string

from django_absurd.backends import get_absurd_backends
from django_absurd.pg_cron.choices import Source
from django_absurd.validators import validate_task_path

NAME_CHARSET_MESSAGE = "Schedule name contains characters other than [A-Za-z0-9_-]."

# alias is always a string (a TextField, or the TASKS backend key), so the plain
# built-in RegexValidator suffices.
validate_alias_charset = RegexValidator(
    r"^[A-Za-z0-9_-]+\Z",
    message="Backend alias contains characters other than [A-Za-z0-9_-].",
)

# name can arrive as a non-str SCHEDULE key (e.g. an int); RegexValidator coerces
# via str(), so guard first to reject it rather than silently pass.
NAME_CHARSET_VALIDATOR = RegexValidator(
    r"^[A-Za-z0-9_-]+\Z", message=NAME_CHARSET_MESSAGE
)


def validate_name_charset(value: t.Any) -> None:
    if not isinstance(value, str):
        raise ValidationError(NAME_CHARSET_MESSAGE)
    NAME_CHARSET_VALIDATOR(value)


def build_jobname(alias: str, name: str, source: str = Source.SETTINGS) -> str:
    """Return the pg_cron job name for a scheduled task."""
    return f"absurd:{source}:{alias}:{name}"


def build_jobname_prefix(alias: str, source: str = Source.SETTINGS) -> str:
    """Return the LIKE prefix used to prune pg_cron jobs for an alias + source."""
    return f"absurd:{source}:{alias}:"


def validate_jobname_length(source: str, alias: str, name: str) -> None:
    jobname = build_jobname(alias, name, source)
    size = len(jobname.encode())
    if size > 63:
        msg = (
            f"job name exceeds 63 bytes (composed name {jobname!r} is {size} bytes;"
            " Postgres silently truncates longer names)."
        )
        raise ValidationError(msg)


def validate_declared_queue(
    queue: str, task_path: str, declared_queues: set[str]
) -> None:
    """Reject a schedule whose effective queue isn't declared.

    Effective queue = the explicit override, else the task's own queue_name. A
    bad/unimportable task is reported by validate_task_path, so skip silently here.
    """
    if queue:
        effective = queue
    else:
        try:
            validate_task_path(task_path)
        except ValidationError:
            return  # bad task path is reported by validate_task_path itself
        effective = import_string(task_path).queue_name
    if effective not in declared_queues:
        msg = f"queue {effective!r} is not declared."
        raise ValidationError(msg)


def validate_alias_is_pg_cron_backend(alias: str) -> None:
    backend = get_absurd_backends().get(alias)
    if backend is None or backend.scheduler != "pg_cron":
        msg = f"backend {alias!r} is not a configured pg_cron backend."
        raise ValidationError(msg)


def validate_pg_cron_cron(cron: str, database: str) -> None:
    """Validate a pg_cron schedule expression by asking pg_cron itself.

    pg_cron owns its grammar (a 5-field cron or the interval form ``<n> seconds``),
    so rather than a hand-rolled matcher we schedule a throwaway job inside an atomic
    block that is always rolled back — nothing persists. If cron.schedule rejects the
    expression, surface pg_cron's own error message on the cron field. The atomic is a
    savepoint when an enclosing transaction exists (the admin wraps the whole request;
    a model's save-time full_clean may run inside one) and a real transaction otherwise;
    set_rollback rolls it back either way, so the probe never leaves a row.
    """
    # Unique per call so concurrent probes never collide on the pg_cron job name
    # (each is rolled back, but a shared name would still contend on the same row).
    probe_jobname = f"absurd:__probe__:{uuid.uuid4()}"
    try:
        with transaction.atomic(using=database):
            with connections[database].cursor() as cur:
                cur.execute(
                    "select cron.schedule(%s, %s, %s)",
                    [probe_jobname, cron, "select 1"],
                )
            transaction.set_rollback(True, using=database)
    except DatabaseError as exc:
        raise ValidationError(str(exc).strip()) from exc
