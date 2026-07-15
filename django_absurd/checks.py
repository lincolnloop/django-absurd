import typing as t
from collections.abc import Mapping, Sequence

import croniter
from absurd_sdk import CreateQueueOptions, QueueDetachMode, QueueStorageMode
from django.apps import AppConfig, apps
from django.conf import settings
from django.contrib.admin.sites import AdminSite
from django.core.checks import CheckMessage, Error, Tags, register
from django.core.checks import Warning as DjangoWarning
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db.utils import OperationalError, ProgrammingError
from django.utils.connection import ConnectionDoesNotExist
from django.utils.module_loading import import_string

from django_absurd.backends import (
    get_absurd_backends,
    get_declared_queues,
    get_pg_cron_backends,
)
from django_absurd.connection import BACKEND_ERROR_MESSAGE, validate_backend
from django_absurd.models import Queue
from django_absurd.queues import get_absurd_backend, get_absurd_database
from django_absurd.routers import AbsurdRouter
from django_absurd.validators import (
    validate_args_is_list,
    validate_args_serializable,
    validate_kwargs_is_dict,
    validate_kwargs_serializable,
    validate_task_path,
)

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
E007_HINT_CRON = "Provide a valid cron expression (e.g. '0 2 * * *')."
E007_HINT_PG_CRON_CRON = (
    "Set cron to a non-empty schedule string (a 5-field cron or '<n> seconds');"
    " pg_cron validates the grammar at sync time."
)
E007_HINT_UNKNOWN_KEY = (
    "Remove unknown keys; valid keys are: task, cron, queue, args, kwargs."
)
E007_HINT_SERIALIZE = "Ensure args and kwargs contain only JSON-serializable values."
E007_HINT_SHAPE = (
    "args must be a JSON array (list); kwargs must be a JSON object (dict)."
)
E007_HINT_QUEUE = "Declare the queue under OPTIONS['QUEUES'] or correct the queue name."
E007_HINT_SCHEDULER = "Set SCHEDULER to 'beat' or 'pg_cron'."

E009_MSG = "django-absurd: OPTIONS['DEFAULT_MAX_ATTEMPTS'] must be an integer >= 1."
E009_HINT = "Set DEFAULT_MAX_ATTEMPTS to a positive integer (Absurd rejects < 1)."

E008_MSG = (
    "django-absurd: SCHEDULER is 'pg_cron' but 'django_absurd.pg_cron'"
    " is not in INSTALLED_APPS."
)
E008_HINT = "Add 'django_absurd.pg_cron' to INSTALLED_APPS, after 'django_absurd'."
W003_MSG = (
    "django-absurd: 'django_absurd.pg_cron' is ordered before 'django_absurd'"
    " in INSTALLED_APPS (its post_migrate cron reconcile runs before queue"
    " provisioning)."
)
W003_HINT = "Place 'django_absurd.pg_cron' after 'django_absurd' in INSTALLED_APPS."

VALID_SCHEDULE_KEYS = {"task", "cron", "queue", "args", "kwargs"}
VALID_SCHEDULERS = {"beat", "pg_cron"}
PG_CRON_APP_NAME = "django_absurd.pg_cron"


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


@register("absurd")
def check_absurd_default_max_attempts(
    *,
    app_configs: Sequence[AppConfig] | None,
    **kwargs: t.Any,
) -> list[CheckMessage]:
    # DEFAULT_MAX_ATTEMPTS becomes the retry ceiling for every schedule reconcile
    # writes; a value < 1 would fail the pg_cron max_attempts CheckConstraint and crash
    # migrate. Validate it for every backend that sets it (bool is rejected — it is an
    # int subclass but not a meaningful attempt count).
    errors: list[CheckMessage] = []
    for backend in get_absurd_backends().values():
        if "DEFAULT_MAX_ATTEMPTS" not in backend.options:
            continue
        value = backend.options["DEFAULT_MAX_ATTEMPTS"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            errors.append(Error(E009_MSG, hint=E009_HINT, id="absurd.E009"))
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
                    f"django-absurd: unknown SCHEDULER {scheduler!r}.",
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
            errors.extend(validate_schedule(name, spec, declared_queues, scheduler))
    return errors


def validate_schedule(
    name: str,
    spec: t.Any,
    declared_queues: set[str],
    scheduler: str,
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

    try:
        validate_task_path(spec.get("task", ""))
    except ValidationError as exc:
        hint = E007_HINT_NOT_TASK if exc.code == "not_a_task" else E007_HINT_IMPORT
        errors.append(
            Error(
                f"{E007_MSG} Schedule {name!r}: {exc.message}",
                hint=hint,
                id="absurd.E007",
            )
        )

    # croniter validates the beat grammar only. pg_cron has its own grammar
    # (5-field cron or "[1-59] seconds"), validated by the DB at sync — so for
    # pg_cron the check only enforces structural presence (a non-empty string),
    # leaving the grammar to cron.schedule.
    cron = spec.get("cron", "")
    if scheduler == "beat" and (
        not isinstance(cron, str) or not croniter.croniter.is_valid(cron)
    ):
        errors.append(
            Error(
                f"{E007_MSG} Schedule {name!r}: invalid cron expression {cron!r}.",
                hint=E007_HINT_CRON,
                id="absurd.E007",
            )
        )
    elif scheduler == "pg_cron" and (not isinstance(cron, str) or not cron.strip()):
        errors.append(
            Error(
                f"{E007_MSG} Schedule {name!r}: cron must be a non-empty string.",
                hint=E007_HINT_PG_CRON_CRON,
                id="absurd.E007",
            )
        )

    for field, validate, hint in (
        ("args", validate_args_serializable, E007_HINT_SERIALIZE),
        ("args", validate_args_is_list, E007_HINT_SHAPE),
        ("kwargs", validate_kwargs_serializable, E007_HINT_SERIALIZE),
        ("kwargs", validate_kwargs_is_dict, E007_HINT_SHAPE),
    ):
        value = spec.get(field)
        if value is not None:
            try:
                validate(value)
            except ValidationError as exc:
                errors.append(
                    Error(
                        f"{E007_MSG} Schedule {name!r}: {exc.message}",
                        hint=hint,
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


@register("absurd")
def check_scheduler_app_installed(
    *,
    app_configs: Sequence[AppConfig] | None,
    **kwargs: t.Any,
) -> list[CheckMessage]:
    app_installed = apps.is_installed(PG_CRON_APP_NAME)

    if not app_installed:
        # E008 only fires when a backend actually needs the app.
        if get_pg_cron_backends():
            return [Error(E008_MSG, hint=E008_HINT, id="absurd.E008")]
        return []

    # W003 tracks INSTALLED_APPS ordering regardless of the active scheduler: a
    # mis-ordered app runs its post_migrate reconcile before queue provisioning
    # the moment any backend switches to pg_cron.
    app_names = resolve_installed_app_names()
    if (
        PG_CRON_APP_NAME in app_names
        and "django_absurd" in app_names
        and app_names.index(PG_CRON_APP_NAME) < app_names.index("django_absurd")
    ):
        return [DjangoWarning(W003_MSG, hint=W003_HINT, id="absurd.W003")]
    return []


def resolve_installed_app_names() -> list[str]:
    """Return INSTALLED_APPS entries as canonical app names.

    Plain module strings pass through unchanged; dotted AppConfig paths
    (e.g. 'django_absurd.pg_cron.apps.PgCronConfig') are resolved via the
    registry to their app's .name so ordering comparisons work regardless of
    how the consumer specifies each app.
    """
    name_by_config_class: dict[str, str] = {
        f"{type(cfg).__module__}.{type(cfg).__qualname__}": cfg.name
        for cfg in apps.get_app_configs()
    }
    return [name_by_config_class.get(entry, entry) for entry in settings.INSTALLED_APPS]


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
