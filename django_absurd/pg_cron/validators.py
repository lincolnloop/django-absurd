"""Shared schedule validators — the single source of rule truth.

Each raises `django.core.exceptions.ValidationError`. `ScheduledTask.clean()` +
field `validators=[...]` enforce them model-first; the system checks call the
same callables and wrap failures into `absurd.E007`. The field-level validator
(name charset) is pure; the contextual one (`validate_pg_cron_cron`) reads the
database.
"""

import re
import typing as t

from croniter import croniter
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.utils.module_loading import import_string

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


CRON_FIELD_COUNT = 5
MIN_INTERVAL_SECONDS = 1
MAX_INTERVAL_SECONDS = 59

# Ports pg_cron's TryParseInterval: sscanf(" %u secon%c%c %c") over a lowercased copy,
# so singular and plural both parse, case is irrelevant, surrounding space is skipped
# and trailing text is refused.
INTERVAL = re.compile(r"\s*(\d+)\s+seconds?\s*", re.IGNORECASE)
INTERVAL_RANGE_MESSAGE = (
    f"An interval schedule must be between {MIN_INTERVAL_SECONDS}"
    f" and {MAX_INTERVAL_SECONDS} seconds."
)

# entry.c implements all nine; the two that name no cadence are refused, since a
# schedule that fires on server restart is not a recurring task.
RECURRING_ALIASES = frozenset(
    {"@yearly", "@annually", "@monthly", "@weekly", "@daily", "@midnight", "@hourly"}
)
NON_RECURRING_ALIASES = frozenset({"@reboot", "@restart"})
NON_RECURRING_ALIAS_MESSAGE = (
    "@reboot and @restart do not describe a recurring schedule."
)
INVALID_CRON_MESSAGE = "Not a valid cron expression."

# Vixie's parser implements none of these. They are rejected rather than deferred to
# pg_cron because it does not reject them uniformly: '#' it swallows as command text
# (so '0 2 * * 5#2' silently means every Friday), while 'L' and '?' it does refuse.
# Matching a bare modifier only — L/W/R/H also occur INSIDE legal names (JUL, WED,
# MAR, THU), so a plain character test would reject valid expressions.
UNSUPPORTED_SYNTAX = re.compile(r"[#?]|(?<![A-Za-z])[LWRHlwrh](?![A-Za-z])")
UNSUPPORTED_SYNTAX_MESSAGE = "pg_cron cannot parse '#', 'L', 'W', '?', 'R' or 'H'."


def validate_pg_cron_cron(cron: str) -> None:
    """Validate a pg_cron schedule expression in Python, without asking the database.

    pg_cron's OWN grammar, which is not the beat scheduler's: five fields or the
    interval form, never beat's 6-field leading-seconds shape. Two rejections are
    deliberately stricter than pg_cron itself, because its parser is Vixie's — five
    fields and then THE COMMAND — so a sixth field or a ``#`` is swallowed as command
    text and the job silently runs a schedule nobody wrote.

    croniter decides the contents of the five fields (ranges, steps, lists, month and
    day names); everything above it here decides which grammar the expression is in."""
    expression = cron.strip()
    if expression.startswith("@"):
        validate_alias(expression)
        return
    if interval := INTERVAL.fullmatch(expression):
        validate_interval_seconds(int(interval.group(1)))
        return
    reject_unsupported_syntax(expression)
    reject_wrong_field_count(expression)
    if not croniter.is_valid(expression):
        raise ValidationError(INVALID_CRON_MESSAGE)


def validate_alias(expression: str) -> None:
    if expression.lower() in NON_RECURRING_ALIASES:
        raise ValidationError(NON_RECURRING_ALIAS_MESSAGE)
    if expression.lower() not in RECURRING_ALIASES:
        raise ValidationError(INVALID_CRON_MESSAGE)


def validate_interval_seconds(seconds: int) -> None:
    if not MIN_INTERVAL_SECONDS <= seconds <= MAX_INTERVAL_SECONDS:
        raise ValidationError(INTERVAL_RANGE_MESSAGE)


def reject_wrong_field_count(expression: str) -> None:
    fields = expression.split()
    if len(fields) != CRON_FIELD_COUNT:
        msg = (
            f"Expected a {CRON_FIELD_COUNT}-field cron expression;"
            f" got {len(fields)} fields."
        )
        raise ValidationError(msg)


def reject_unsupported_syntax(cron: str) -> None:
    if UNSUPPORTED_SYNTAX.search(cron):
        raise ValidationError(UNSUPPORTED_SYNTAX_MESSAGE)
