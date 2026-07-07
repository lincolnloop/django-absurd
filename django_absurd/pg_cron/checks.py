"""System checks for the pg_cron scheduler app (registered via PgCronConfig.ready)."""

import logging
import re
import typing as t
from collections.abc import Mapping, Sequence

from django.apps import AppConfig
from django.core.checks import CheckMessage, Error, register
from django.tasks import Task
from django.utils.module_loading import import_string

from django_absurd.backends import get_absurd_backends, get_declared_queues
from django_absurd.checks import E007_HINT_QUEUE, E007_MSG
from django_absurd.pg_cron.reconcile import build_jobname, get_effective_queue
from django_absurd.scheduler import Schedule

E007_HINT_PG_CRON_SUBMINUTE = (
    "pg_cron fires at minute granularity; use a 5-field cron expression"
    " (no leading seconds column)."
)
E007_HINT_PG_CRON_NAME = (
    "Schedule names must match [A-Za-z0-9_-]+ when using the pg_cron scheduler."
)
E007_HINT_PG_CRON_ALIAS = (
    "Backend aliases must match [A-Za-z0-9_-]+ when using the pg_cron scheduler."
)
E007_HINT_PG_CRON_JOBNAME = (
    "Shorten the schedule name or backend alias so the composed job name"
    " (absurd:settings:<alias>:<name>) fits within 63 bytes."
)

logger = logging.getLogger("django_absurd")

PG_CRON_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")


@register("absurd")
def check_pg_cron_schedules(
    *,
    app_configs: Sequence[AppConfig] | None,
    **kwargs: t.Any,
) -> list[CheckMessage]:
    errors: list[CheckMessage] = []
    for backend in get_absurd_backends().values():
        if backend.scheduler != "pg_cron":
            continue
        declared_queues = set(get_declared_queues(backend))
        raw_schedule = backend.options.get("SCHEDULE", {})
        if not isinstance(raw_schedule, Mapping):
            continue  # core's check_absurd_schedule_config reports this
        for name, spec in raw_schedule.items():
            errors.extend(
                validate_pg_cron_schedule(name, spec, backend.alias, declared_queues)
            )
    return errors


def validate_pg_cron_schedule(
    name: str,
    spec: t.Any,
    alias: str,
    declared_queues: set[str],
) -> list[CheckMessage]:
    if not isinstance(spec, Mapping):
        return []

    cron = spec.get("cron", "")
    task_path = spec.get("task", "")
    queue_override = spec.get("queue")
    errors: list[CheckMessage] = []
    errors.extend(check_pg_cron_cron_fields(name, cron))
    errors.extend(check_pg_cron_names(name, alias))
    errors.extend(
        check_pg_cron_effective_queue(
            name, task_path, cron, queue_override, declared_queues
        )
    )
    return errors


def check_pg_cron_cron_fields(name: str, cron: t.Any) -> list[CheckMessage]:
    if isinstance(cron, str) and len(cron.split()) == 6:
        return [
            Error(
                f"{E007_MSG} Schedule {name!r}: 6-field cron expressions are not"
                " supported by pg_cron.",
                hint=E007_HINT_PG_CRON_SUBMINUTE,
                id="absurd.E007",
            )
        ]
    return []


def check_pg_cron_names(name: t.Any, alias: str) -> list[CheckMessage]:
    errors: list[CheckMessage] = []
    if not isinstance(name, str) or not PG_CRON_NAME_RE.fullmatch(name):
        errors.append(
            Error(
                f"{E007_MSG} Schedule {name!r}: invalid schedule name"
                " for pg_cron (only [A-Za-z0-9_-] characters are allowed).",
                hint=E007_HINT_PG_CRON_NAME,
                id="absurd.E007",
            )
        )
    if not PG_CRON_NAME_RE.fullmatch(alias):
        errors.append(
            Error(
                f"{E007_MSG} Schedule {name!r}: backend alias {alias!r} contains"
                " characters not allowed in pg_cron job names ([A-Za-z0-9_-] only).",
                hint=E007_HINT_PG_CRON_ALIAS,
                id="absurd.E007",
            )
        )
    if not errors:
        jobname = build_jobname(alias, name)
        if len(jobname.encode()) > 63:
            errors.append(
                Error(
                    f"{E007_MSG} Schedule {name!r}: job name exceeds 63 bytes"
                    f" (composed name {jobname!r} is {len(jobname.encode())} bytes;"
                    " Postgres silently truncates longer names).",
                    hint=E007_HINT_PG_CRON_JOBNAME,
                    id="absurd.E007",
                )
            )
    return errors


def check_pg_cron_effective_queue(
    name: str,
    task_path: t.Any,
    cron: t.Any,
    queue_override: t.Any,
    declared_queues: set[str],
) -> list[CheckMessage]:
    if queue_override:
        return []  # explicit truthy overrides are validated generically by core
    if not isinstance(task_path, str) or not task_path:
        return []
    try:
        task_obj = import_string(task_path)
    except Exception:
        # Any import-time failure (not just ImportError — a module-level env read
        # can raise KeyError/ValueError) is already reported as E007 by core's
        # validate_schedule_task; log and return rather than crash the check.
        logger.exception("absurd.E007: task %r could not be imported", task_path)
        return []
    if not isinstance(task_obj, Task):
        return []
    schedule_obj = Schedule(
        name=name,
        task=task_path,
        cron=cron if isinstance(cron, str) else "",
    )
    eff_queue = get_effective_queue(schedule_obj)
    if eff_queue not in declared_queues:
        return [
            Error(
                f"{E007_MSG} Schedule {name!r}: queue {eff_queue!r} is not declared.",
                hint=E007_HINT_QUEUE,
                id="absurd.E007",
            )
        ]
    return []
