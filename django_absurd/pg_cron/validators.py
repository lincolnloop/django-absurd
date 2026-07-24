"""Shared schedule validators — the single source of rule truth.

Each raises `django.core.exceptions.ValidationError`. `ScheduledTask.clean()` +
field `validators=[...]` enforce them model-first; the system checks call the
same callables and wrap failures into `absurd.E007`. The field-level validator
(name charset) is pure; the contextual one (`validate_pg_cron_cron`) reads the
database.
"""

import typing as t

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.utils.module_loading import import_string

from django_absurd.pg_cron import catalog
from django_absurd.validators import validate_task_path

NAME_CHARSET_MESSAGE = "Schedule name contains characters other than [A-Za-z0-9_-]."

# name can arrive as a non-str SCHEDULE key (e.g. an int); RegexValidator coerces
# via str(), so guard first to reject it rather than silently pass.
NAME_CHARSET_VALIDATOR = RegexValidator(
    r"^[A-Za-z0-9_-]+\Z", message=NAME_CHARSET_MESSAGE
)


def validate_name_charset(value: t.Any) -> None:
    if not isinstance(value, str):
        raise ValidationError(NAME_CHARSET_MESSAGE)
    NAME_CHARSET_VALIDATOR(value)


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


def validate_pg_cron_cron(cron: str, database: str) -> None:
    """Validate a pg_cron schedule expression by asking pg_cron itself, via the catalog
    seam's ``probe_cron_grammar`` (schedule a throwaway job on the central connection,
    then unschedule it). Delegates so grammar probing shares the single cross-database
    write path; it re-raises pg_cron's own error as ``ValidationError`` on the cron
    field."""
    catalog.probe_cron_grammar(database, cron=cron)
