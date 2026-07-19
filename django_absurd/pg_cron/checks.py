"""System checks for the pg_cron scheduler app (registered via PgCronConfig.ready)."""

import typing as t
from collections.abc import Mapping, Sequence

from django.apps import AppConfig
from django.core.checks import CheckMessage, Error, register
from django.core.exceptions import ValidationError

from django_absurd.backends import get_absurd_backends, get_declared_queues
from django_absurd.checks import E007_HINT_QUEUE, E007_MSG
from django_absurd.pg_cron.choices import Source
from django_absurd.pg_cron.validators import (
    validate_declared_queue,
    validate_jobname_length,
    validate_name_charset,
)

E007_HINT_PG_CRON_NAME = (
    "Schedule names must match [A-Za-z0-9_-]+ when using the pg_cron scheduler."
)
E007_HINT_PG_CRON_JOBNAME = (
    "Shorten the schedule name so the composed job name"
    " (_dj:s:<name>) fits within 63 bytes."
)


@register("absurd")
def check_pg_cron_schedules(
    *,
    app_configs: Sequence[AppConfig] | None,
    **kwargs: t.Any,
) -> list[CheckMessage]:
    errors: list[CheckMessage] = []
    for backend in get_absurd_backends().values():
        declared_queues = set(get_declared_queues(backend))
        raw_schedule = backend.options.get("SCHEDULE", {})
        if not isinstance(raw_schedule, Mapping):
            continue  # core's check_absurd_schedule_config reports this
        for name, spec in raw_schedule.items():
            errors.extend(validate_pg_cron_schedule(name, spec, declared_queues))
    return errors


def validate_pg_cron_schedule(
    name: str,
    spec: t.Any,
    declared_queues: set[str],
) -> list[CheckMessage]:
    if not isinstance(spec, Mapping):
        return []

    task_path = spec.get("task", "")
    queue_override = spec.get("queue")
    errors: list[CheckMessage] = []
    errors.extend(check_pg_cron_name(name))
    errors.extend(
        check_pg_cron_effective_queue(name, task_path, queue_override, declared_queues)
    )
    return errors


def check_pg_cron_name(name: t.Any) -> list[CheckMessage]:
    errors: list[CheckMessage] = []
    try:
        validate_name_charset(name)
    except ValidationError as exc:
        errors.append(
            Error(
                f"{E007_MSG} Schedule {name!r}: {exc.message}",
                hint=E007_HINT_PG_CRON_NAME,
                id="absurd.E007",
            )
        )
    # jobname length is composite (source:name); only meaningful once the name
    # charset is clean, so skip it when the name is already flagged.
    if not errors:
        try:
            validate_jobname_length(Source.SETTINGS, name)
        except ValidationError as exc:
            errors.append(
                Error(
                    f"{E007_MSG} Schedule {name!r}: {exc.message}",
                    hint=E007_HINT_PG_CRON_JOBNAME,
                    id="absurd.E007",
                )
            )
    return errors


def check_pg_cron_effective_queue(
    name: str,
    task_path: t.Any,
    queue_override: t.Any,
    declared_queues: set[str],
) -> list[CheckMessage]:
    if queue_override:
        return []  # explicit truthy overrides are validated generically by core
    try:
        validate_declared_queue("", task_path, declared_queues)
    except ValidationError as exc:
        return [
            Error(
                f"{E007_MSG} Schedule {name!r}: {exc.message}",
                hint=E007_HINT_QUEUE,
                id="absurd.E007",
            )
        ]
    return []
