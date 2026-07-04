import json
import logging
import re
import sys
import typing as t
from collections.abc import Mapping, Sequence

import croniter
from absurd_sdk import CreateQueueOptions, QueueDetachMode, QueueStorageMode
from django.apps import AppConfig
from django.conf import settings
from django.contrib.admin.sites import AdminSite
from django.core.checks import CheckMessage, Error, Tags, register
from django.core.checks import Warning as DjangoWarning
from django.core.exceptions import ImproperlyConfigured
from django.db.utils import OperationalError, ProgrammingError
from django.tasks import Task
from django.utils.connection import ConnectionDoesNotExist
from django.utils.module_loading import import_string

from django_absurd.backends import get_absurd_backends, get_declared_queues
from django_absurd.connection import BACKEND_ERROR_MESSAGE, validate_backend
from django_absurd.models import Queue
from django_absurd.pgcron import build_jobname, effective_queue
from django_absurd.queues import get_absurd_backend, get_absurd_database
from django_absurd.routers import AbsurdRouter
from django_absurd.scheduler import Schedule

logger = logging.getLogger(__name__)

W002_MSG = (
    "django-absurd: a queue's declared storage_mode differs from the database"
    " (storage_mode is immutable)."
)
W002_HINT = "Recreate the queue, or revert the declared storage_mode."
E005_MSG = (
    "django-absurd: a non-default DATABASE is configured but AbsurdRouter is not in"
    " DATABASE_ROUTERS."
)
E005_HINT = "Add 'django_absurd.routers.AbsurdRouter' to settings.DATABASE_ROUTERS."
E001_MSG = BACKEND_ERROR_MESSAGE
E002_MSG = (
    "django-absurd: both top-level QUEUES and OPTIONS['QUEUES'] are set"
    " on the same backend."
)
E002_HINT = "Remove either the top-level QUEUES key or OPTIONS['QUEUES'] — not both."
E003_MSG = "django-absurd: invalid per-queue policy options."
E003_HINT = (
    "Remove unknown keys and ensure storage_mode/detach_mode values"
    " are valid SDK literals."
)
E004_MSG = (
    "django-absurd: multiple Absurd backends are configured with distinct"
    " DATABASE values."
)
E004_HINT = "Use a single DATABASE across all Absurd backends."

VALID_QUEUE_OPTION_KEYS = set(CreateQueueOptions.__annotations__)
VALID_STORAGE_MODES = set(t.get_args(QueueStorageMode))
VALID_DETACH_MODES = set(t.get_args(QueueDetachMode))

E006_ENABLE_ADMIN_MSG = "django-absurd: OPTIONS['ENABLE_ADMIN'] must be a bool."
E006_ENABLE_ADMIN_HINT = "Set ENABLE_ADMIN to True or False."
E006_ADMIN_SITE_TYPE_MSG = (
    "django-absurd: OPTIONS['ADMIN_SITE'] must be a tuple or list"
    " of dotted-path strings."
)
E006_ADMIN_SITE_HINT = (
    "Set ADMIN_SITE to a tuple of dotted paths to AdminSite instances."
)

E007_MSG = "django-absurd: invalid SCHEDULE entry."
E007_HINT_IMPORT = (
    "Ensure the task path is importable and points to a @task-decorated function."
)
E007_HINT_NOT_TASK = "The path must point to a Django @task-decorated callable."
E007_HINT_CRON = "Provide a valid 5-field cron expression (e.g. '0 2 * * *')."
E007_HINT_UNKNOWN_KEY = (
    "Remove unknown keys; valid keys are: task, cron, queue, args, kwargs."
)
E007_HINT_SERIALIZE = "Ensure args and kwargs contain only JSON-serializable values."
E007_HINT_QUEUE = "Declare the queue under OPTIONS['QUEUES'] or correct the queue name."
E007_HINT_SCHEDULER = "Set SCHEDULER to 'beat' or 'pg_cron'."
E007_HINT_PGCRON_SUBMINUTE = (
    "pg_cron is minute-granularity; use the beat scheduler for sub-minute schedules."
)
E007_HINT_PGCRON_NAME = (
    "Schedule names must match [A-Za-z0-9_-]+ when using the pg_cron scheduler."
)
E007_HINT_PGCRON_JOBNAME = (
    "Shorten the schedule name or backend alias so the composed job name"
    " (absurd:settings:<alias>:<name>) fits within 63 bytes."
)

VALID_SCHEDULE_KEYS = {"task", "cron", "queue", "args", "kwargs"}
VALID_SCHEDULERS = {"beat", "pg_cron"}
PGCRON_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@register("absurd")
def check_absurd_admin_config(
    *,
    app_configs: Sequence[AppConfig] | None,
    **kwargs: t.Any,
) -> list[CheckMessage]:
    backend = get_absurd_backend()
    if backend is None:
        return []

    errors: list[CheckMessage] = []
    options = backend.options

    if "ENABLE_ADMIN" in options and not isinstance(options["ENABLE_ADMIN"], bool):
        errors.append(
            Error(
                E006_ENABLE_ADMIN_MSG,
                hint=E006_ENABLE_ADMIN_HINT,
                id="absurd.E006",
            )
        )

    if "ADMIN_SITE" in options:
        errors.extend(validate_admin_site_option(options["ADMIN_SITE"]))

    return errors


def validate_admin_site_option(value: t.Any) -> list[CheckMessage]:
    if not isinstance(value, (tuple, list)) or not all(
        isinstance(entry, str) for entry in value
    ):
        return [
            Error(
                E006_ADMIN_SITE_TYPE_MSG,
                hint=E006_ADMIN_SITE_HINT,
                id="absurd.E006",
            )
        ]

    errors: list[CheckMessage] = []
    for path in value:
        try:
            obj = import_string(path)
        except ImportError:
            errors.append(
                Error(
                    f"django-absurd: OPTIONS['ADMIN_SITE'] entry {path!r}"
                    " could not be imported.",
                    hint=E006_ADMIN_SITE_HINT,
                    id="absurd.E006",
                )
            )
            continue
        if not isinstance(obj, AdminSite):
            errors.append(
                Error(
                    f"django-absurd: OPTIONS['ADMIN_SITE'] entry {path!r}"
                    " is not an AdminSite instance.",
                    hint=E006_ADMIN_SITE_HINT,
                    id="absurd.E006",
                )
            )
    return errors


@register("absurd")
def check_absurd_schedule_config(
    *,
    app_configs: Sequence[AppConfig] | None,
    **kwargs: t.Any,
) -> list[CheckMessage]:
    errors: list[CheckMessage] = []
    for backend in get_absurd_backends().values():
        scheduler = backend.scheduler
        if scheduler not in VALID_SCHEDULERS:
            errors.append(
                Error(
                    f"{E007_MSG} unknown SCHEDULER {scheduler!r}.",
                    hint=E007_HINT_SCHEDULER,
                    id="absurd.E007",
                )
            )
            continue

        declared_queues = set(get_declared_queues(backend))
        raw_schedule = backend.options.get("SCHEDULE", {})
        if not isinstance(raw_schedule, Mapping):
            errors.append(
                Error(
                    f'{E007_MSG} OPTIONS["SCHEDULE"] must be a mapping'
                    " of name -> spec.",
                    hint="Set SCHEDULE to a dict mapping schedule names to spec dicts.",
                    id="absurd.E007",
                )
            )
            continue
        for name, spec in raw_schedule.items():
            errors.extend(validate_schedule(name, spec, declared_queues))
            if scheduler == "pg_cron":
                errors.extend(
                    validate_pgcron_schedule(name, spec, backend.alias, declared_queues)
                )
    return errors


def validate_schedule(
    name: str,
    spec: t.Any,
    declared_queues: set[str],
) -> list[CheckMessage]:
    if not isinstance(spec, Mapping):
        return [
            Error(
                f"{E007_MSG} Schedule {name!r} must be a mapping.",
                hint=(
                    "Set the schedule entry to a dict"
                    " with task, cron, and optional queue/args/kwargs."
                ),
                id="absurd.E007",
            )
        ]

    errors: list[CheckMessage] = [
        Error(
            f"{E007_MSG} Schedule {name!r}: unknown key {key!r}.",
            hint=E007_HINT_UNKNOWN_KEY,
            id="absurd.E007",
        )
        for key in spec
        if key not in VALID_SCHEDULE_KEYS
    ]

    errors.extend(validate_schedule_task(name, spec.get("task", "")))

    cron = spec.get("cron", "")
    if not isinstance(cron, str) or not croniter.croniter.is_valid(cron):
        errors.append(
            Error(
                f"{E007_MSG} Schedule {name!r}: invalid cron expression {cron!r}.",
                hint=E007_HINT_CRON,
                id="absurd.E007",
            )
        )

    for field in ("args", "kwargs"):
        value = spec.get(field)
        if value is not None:
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                errors.append(
                    Error(
                        f"{E007_MSG} Schedule {name!r}:"
                        f" {field} is not JSON-serializable.",
                        hint=E007_HINT_SERIALIZE,
                        id="absurd.E007",
                    )
                )

    queue = spec.get("queue")
    if queue is not None and (
        not isinstance(queue, str) or queue not in declared_queues
    ):
        errors.append(
            Error(
                f"{E007_MSG} Schedule {name!r}: queue {queue!r} is not declared.",
                hint=E007_HINT_QUEUE,
                id="absurd.E007",
            )
        )

    return errors


def validate_pgcron_schedule(
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
    errors.extend(check_pgcron_cron_fields(name, cron))
    errors.extend(check_pgcron_names(name, alias))
    errors.extend(
        check_pgcron_effective_queue(
            name, task_path, cron, queue_override, declared_queues
        )
    )
    return errors


def check_pgcron_cron_fields(name: str, cron: t.Any) -> list[CheckMessage]:
    if isinstance(cron, str) and len(cron.split()) == 6:
        return [
            Error(
                f"{E007_MSG} Schedule {name!r}: 6-field cron expressions are not"
                " supported by pg_cron (pg_cron is minute-granularity; use the beat"
                " scheduler for sub-minute schedules).",
                hint=E007_HINT_PGCRON_SUBMINUTE,
                id="absurd.E007",
            )
        ]
    return []


def check_pgcron_names(name: str, alias: str) -> list[CheckMessage]:
    errors: list[CheckMessage] = []
    if not PGCRON_NAME_RE.match(name):
        errors.append(
            Error(
                f"{E007_MSG} Schedule {name!r}: invalid schedule name"
                " for pg_cron (only [A-Za-z0-9_-] characters are allowed).",
                hint=E007_HINT_PGCRON_NAME,
                id="absurd.E007",
            )
        )
    if not PGCRON_NAME_RE.match(alias):
        errors.append(
            Error(
                f"{E007_MSG} Schedule {name!r}: backend alias {alias!r} contains"
                " characters not allowed in pg_cron job names ([A-Za-z0-9_-] only).",
                hint=E007_HINT_PGCRON_NAME,
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
                    hint=E007_HINT_PGCRON_JOBNAME,
                    id="absurd.E007",
                )
            )
    return errors


def check_pgcron_effective_queue(
    name: str,
    task_path: t.Any,
    cron: t.Any,
    queue_override: t.Any,
    declared_queues: set[str],
) -> list[CheckMessage]:
    if not isinstance(task_path, str) or not task_path:
        return []
    try:
        task_obj = import_string(task_path)
    except ImportError:
        return []
    if not isinstance(task_obj, Task):
        return []
    schedule_obj = Schedule(
        name=name,
        task=task_path,
        cron=cron if isinstance(cron, str) else "",
        queue=queue_override,
    )
    eff_queue = effective_queue(schedule_obj)
    if eff_queue not in declared_queues:
        return [
            Error(
                f"{E007_MSG} Schedule {name!r}: queue {eff_queue!r} is not declared.",
                hint=E007_HINT_QUEUE,
                id="absurd.E007",
            )
        ]
    return []


def validate_schedule_task(name: str, task_path: str) -> list[CheckMessage]:
    try:
        task_obj = import_string(task_path)
    except Exception:
        logger.exception("absurd.E007: task %r could not be imported", task_path)
        exc = sys.exc_info()[1]
        return [
            Error(
                f"{E007_MSG} Schedule {name!r}: task {task_path!r}"
                f" could not be imported: {exc!r}",
                hint=E007_HINT_IMPORT,
                id="absurd.E007",
            )
        ]
    if not isinstance(task_obj, Task):
        return [
            Error(
                f"{E007_MSG} Schedule {name!r}: {task_path!r} is not a Django task.",
                hint=E007_HINT_NOT_TASK,
                id="absurd.E007",
            )
        ]
    return []


@register("absurd")
def check_absurd_config(
    *,
    app_configs: Sequence[AppConfig] | None,
    **kwargs: t.Any,
) -> list[CheckMessage]:
    backends = get_absurd_backends()
    if not backends:
        return []

    errors: list[CheckMessage] = []
    databases: set[str] = set()
    e005_emitted = False

    for backend in backends.values():
        db = get_absurd_database(backend)
        databases.add(db)

        if backend.has_top_level_queues and "QUEUES" in backend.options:
            errors.append(Error(E002_MSG, hint=E002_HINT, id="absurd.E002"))

        declared = get_declared_queues(backend)
        for queue_name, policy in declared.items():
            errors.extend(validate_queue_policy(queue_name, policy))

        if db != "default" and not router_installed() and not e005_emitted:
            errors.append(Error(E005_MSG, hint=E005_HINT, id="absurd.E005"))
            e005_emitted = True

        try:
            validate_backend(db)
        except (OperationalError, ConnectionDoesNotExist):
            pass
        except ImproperlyConfigured:
            errors.append(Error(E001_MSG, id="absurd.E001"))

    if len(databases) > 1:
        errors.append(Error(E004_MSG, hint=E004_HINT, id="absurd.E004"))

    return errors


@register(Tags.database, "absurd")
def check_absurd_queue_state(
    *,
    app_configs: Sequence[AppConfig] | None,
    databases: Sequence[str] | None,
    **kwargs: t.Any,
) -> list[CheckMessage]:
    if not databases:
        return []

    backends = get_absurd_backends()
    if not backends:
        return []

    errors: list[CheckMessage] = []
    for backend in backends.values():
        db = get_absurd_database(backend)
        if db not in databases:
            continue
        declared = get_declared_queues(backend)
        if not declared:
            continue
        errors.extend(query_queue_state(db, declared))

    return errors


def validate_queue_policy(
    queue_name: str, policy: dict[str, t.Any]
) -> list[CheckMessage]:
    errors: list[CheckMessage] = [
        Error(
            f"{E003_MSG} Queue '{queue_name}': unknown key '{key}'.",
            hint=E003_HINT,
            id="absurd.E003",
        )
        for key in policy
        if key not in VALID_QUEUE_OPTION_KEYS
    ]
    if "storage_mode" in policy and policy["storage_mode"] not in VALID_STORAGE_MODES:
        mode = policy["storage_mode"]
        errors.append(
            Error(
                f"{E003_MSG} Queue '{queue_name}': invalid storage_mode '{mode}'.",
                hint=E003_HINT,
                id="absurd.E003",
            )
        )
    if "detach_mode" in policy and policy["detach_mode"] not in VALID_DETACH_MODES:
        mode = policy["detach_mode"]
        errors.append(
            Error(
                f"{E003_MSG} Queue '{queue_name}': invalid detach_mode '{mode}'.",
                hint=E003_HINT,
                id="absurd.E003",
            )
        )
    return errors


def router_installed() -> bool:
    for router in settings.DATABASE_ROUTERS:
        if router == "django_absurd.routers.AbsurdRouter":
            return True
        if isinstance(router, AbsurdRouter):
            return True
    return False


def query_queue_state(alias: str, declared: dict[str, dict]) -> list[CheckMessage]:
    try:
        actual = {
            q.queue_name: q
            for q in Queue.objects.using(alias).filter(queue_name__in=declared)
        }
    except (OperationalError, ProgrammingError):
        return []

    drift = [
        name
        for name in declared
        if name in actual
        and declared[name].get("storage_mode")
        and declared[name]["storage_mode"] != actual[name].storage_mode
    ]
    if drift:
        return [
            DjangoWarning(
                W002_MSG,
                hint=f"{W002_HINT} Affected: {', '.join(drift)}",
                id="absurd.W002",
            )
        ]
    return []
