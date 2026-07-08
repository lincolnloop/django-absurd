"""Shared schedule validators — the single source of rule truth.

Each raises `django.core.exceptions.ValidationError`. `ScheduledTask.clean()` +
field `validators=[...]` enforce them model-first; the system checks call the
same callables and wrap failures into `absurd.E007`. The field-level validators
(name/alias charset, jobname length) are pure; the contextual ones
(`validate_alias_is_pg_cron_backend`, `validate_no_cross_source_clash`) read
settings / the database.
"""

import typing as t

from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.utils.module_loading import import_string

from django_absurd.backends import get_absurd_backends
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


def build_jobname(alias: str, name: str, source: str = "settings") -> str:
    """Return the pg_cron job name for a scheduled task."""
    return f"absurd:{source}:{alias}:{name}"


def build_jobname_prefix(alias: str, source: str = "settings") -> str:
    """Return the LIKE prefix used to prune pg_cron jobs for an alias."""
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


def validate_no_cross_source_clash(source: str, alias: str, name: str) -> None:
    """Reject a schedule whose (alias, name) already exists under the OTHER source.

    One name = one schedule per backend: a settings and an admin row sharing
    (alias, name) would produce two pg_cron jobs that both fire.

    No self/pk exclusion: source is immutable in Phase A, so a row can never
    clash with itself. Phase A's writable-admin follow-up must add pk exclusion
    before it allows editing an existing row.
    """
    scheduled_task = apps.get_model("django_absurd_pg_cron", "ScheduledTask")
    clash = (
        scheduled_task.objects.filter(alias=alias, name=name)
        .exclude(source=source)
        .first()
    )
    if clash is not None:
        msg = f"a {clash.source} schedule {name!r} already exists on backend {alias!r}."
        raise ValidationError(msg)
