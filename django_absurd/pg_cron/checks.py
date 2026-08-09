"""System checks for the pg_cron scheduler app (registered via PgCronConfig.ready)."""

import typing as t
from collections.abc import Mapping, Sequence

import psycopg
from django.apps import AppConfig
from django.core.checks import CheckMessage, Error, Tags, register
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db.utils import OperationalError

from django_absurd.backends import get_absurd_backends, get_declared_queues
from django_absurd.checks import E007_HINT_QUEUE, E007_MSG, E010_MSG
from django_absurd.connection import open_central_connection
from django_absurd.pg_cron import detection
from django_absurd.pg_cron.validators import (
    validate_declared_queue,
    validate_name_charset,
    validate_pg_cron_schedule,
)

E007_HINT_PG_CRON_NAME = (
    "Schedule names must match [A-Za-z0-9_-]+ when using the pg_cron scheduler."
)

E007_HINT_PG_CRON_CRON = (
    "Use a 5-field cron expression, an interval such as '30 seconds' (1-59), or one of"
    " @hourly/@daily/@weekly/@monthly/@yearly/@annually/@midnight (lowercase). The beat"
    " scheduler's 6-field leading-seconds form is not pg_cron syntax."
)

E011_MSG = (
    "django-absurd: OPTIONS['SYNC_SCHEDULES_ON_TEST_DB'] is True without"
    " OPTIONS['PG_CRON_ON_TEST_DB']."
)
E011_HINT = (
    "Set OPTIONS['PG_CRON_ON_TEST_DB'] = True as well, or turn off"
    " SYNC_SCHEDULES_ON_TEST_DB."
)

E012_MSG = (
    "django-absurd: the central pg_cron catalog is unreachable or missing"
    " the pg_cron extension."
)
E012_HINT = (
    "Install pg_cron on the database named by cron.database_name"
    " (CREATE EXTENSION pg_cron) and ensure it's reachable from this server."
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
            errors.extend(check_pg_cron_schedule(name, spec, declared_queues))
    return errors


def check_pg_cron_schedule(
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
    errors.extend(check_pg_cron_grammar(name, spec.get("cron", "")))
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
    return errors


def check_pg_cron_grammar(name: str, cron: t.Any) -> list[CheckMessage]:
    if not isinstance(cron, str) or not cron.strip():
        return []  # core reports a missing/non-string cron; don't report it twice
    try:
        validate_pg_cron_schedule(cron)
    except ValidationError as exc:
        return [
            Error(
                f"{E007_MSG} Schedule {name!r}: {exc.message}",
                hint=E007_HINT_PG_CRON_CRON,
                id="absurd.E007",
            )
        ]
    return []


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


@register("absurd")
def check_pg_cron_cleanup_schedule(
    *,
    app_configs: Sequence[AppConfig] | None,
    **kwargs: t.Any,
) -> list[CheckMessage]:
    """CLEANUP's cron is pg_cron's grammar too — core validates its SHAPE and, under
    beat, its croniter grammar; the pg_cron grammar belongs to this app."""
    errors: list[CheckMessage] = []
    for backend in get_absurd_backends().values():
        cleanup = backend.options.get("CLEANUP")
        if not isinstance(cleanup, Mapping):
            continue  # core's check_absurd_cleanup_config reports the shape
        schedule = cleanup.get("schedule")
        if not isinstance(schedule, str) or not schedule.strip():
            continue  # core reports a missing/non-string schedule; not twice
        try:
            validate_pg_cron_schedule(schedule)
        except ValidationError as exc:
            errors.append(
                Error(
                    f"{E010_MSG} {exc.message}",
                    hint=E007_HINT_PG_CRON_CRON,
                    id="absurd.E010",
                )
            )
    return errors


@register("absurd")
def check_pg_cron_test_db_composition(
    *,
    app_configs: Sequence[AppConfig] | None,
    **kwargs: t.Any,
) -> list[CheckMessage]:
    errors: list[CheckMessage] = []
    for backend in get_absurd_backends().values():
        sync_on_test_db = bool(backend.options.get("SYNC_SCHEDULES_ON_TEST_DB", False))
        pg_cron_on_test_db = bool(backend.options.get("PG_CRON_ON_TEST_DB", False))
        if sync_on_test_db and not pg_cron_on_test_db:
            errors.append(Error(E011_MSG, hint=E011_HINT, id="absurd.E011"))
    return errors


@register(Tags.database, "absurd")
def check_pg_cron_central_extension(
    *,
    app_configs: Sequence[AppConfig] | None,
    databases: Sequence[str] | None,
    **kwargs: t.Any,
) -> list[CheckMessage]:
    if not databases:
        return []  # plain `check` (no migrate / --database) stays DB-free

    errors: list[CheckMessage] = []
    for backend in get_absurd_backends().values():
        if backend.database not in databases:
            continue
        if backend.scheduler != "pg_cron":
            continue
        if detection.test_environment_active() or detection.is_test_database(
            backend.database
        ):
            continue  # this fail-safe must not fire during the suite
        if not probe_central_extension(backend.database):  # pragma: no cover
            # Non-test fail-fast; unreachable under pytest — the test env is
            # always active (approved: genuinely can't be reached without
            # tampering with Django test-state internals).
            errors.append(Error(E012_MSG, hint=E012_HINT, id="absurd.E012"))
    return errors


def probe_central_extension(alias: str) -> bool:
    """Whether the central pg_cron catalog DB (auto-discovered via
    ``cron.database_name``) is reachable and has the extension — a function-existence
    gate (``to_regproc``), never a version parse."""
    try:
        with open_central_connection(alias) as cur:
            cur.execute("select to_regproc('cron.schedule_in_database') is not null")
            (present,) = t.cast("tuple[bool]", cur.fetchone())
    except (ImproperlyConfigured, OperationalError, psycopg.OperationalError):
        return False
    return bool(present)
