import typing as t
from collections.abc import Sequence

from absurd_sdk import CreateQueueOptions, QueueDetachMode, QueueStorageMode
from django.apps import AppConfig
from django.conf import settings
from django.core.checks import CheckMessage, Error, Tags, register
from django.core.checks import Warning as DjangoWarning
from django.core.exceptions import ImproperlyConfigured
from django.db.utils import OperationalError, ProgrammingError
from django.utils.connection import ConnectionDoesNotExist

from django_absurd.backends import get_absurd_backends, get_declared_queues
from django_absurd.connection import BACKEND_ERROR_MESSAGE, validate_backend
from django_absurd.models import Queue
from django_absurd.queues import get_absurd_database
from django_absurd.routers import AbsurdRouter

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
