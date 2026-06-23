from collections.abc import Mapping
from dataclasses import dataclass, field

from absurd_sdk import Absurd, CreateQueueOptions
from django.tasks import task_backends

from django_absurd.backends import AbsurdBackend
from django_absurd.connection import build_absurd_client
from django_absurd.models import Queue

AbsurdQueues = Mapping[str, CreateQueueOptions]

MUTABLE_OPTION_KEYS = (
    "partition_lookahead",
    "partition_lookback",
    "cleanup_ttl",
    "cleanup_limit",
    "detach_mode",
    "detach_min_age",
)


@dataclass
class SyncResult:
    created: list[str] = field(default_factory=list)
    reconciled: list[str] = field(default_factory=list)
    storage_warnings: list[str] = field(default_factory=list)


def get_declared_queues(backend: AbsurdBackend) -> dict[str, dict]:
    if "QUEUES" in backend.options:
        return dict(backend.options["QUEUES"])
    return {name: {} for name in backend.queues}


def get_absurd_database(backend: AbsurdBackend) -> str:
    return backend.database


def resolve_absurd_database() -> str:
    databases = {be.database for be in get_absurd_backends().values()}
    if len(databases) == 1:
        return next(iter(databases))
    return "default"


def get_absurd_client(using: str | None = None) -> Absurd:
    return build_absurd_client(using or resolve_absurd_database())


def sync_queues(backend: AbsurdBackend) -> SyncResult:
    using = backend.database
    result = SyncResult()
    client = get_absurd_client(using)
    existing = {q.queue_name: q for q in Queue.objects.using(using)}
    for name, opts in get_declared_queues(backend).items():
        if name not in existing:
            client.create_queue(name, **opts)
            result.created.append(name)
        else:
            mutable_opts = {k: v for k, v in opts.items() if k in MUTABLE_OPTION_KEYS}
            if mutable_opts:
                client.set_queue_policy(name, **mutable_opts)
            result.reconciled.append(name)
            existing_mode = existing[name].storage_mode
            if "storage_mode" in opts and opts["storage_mode"] != existing_mode:
                result.storage_warnings.append(
                    f"Queue '{name}': storage_mode cannot be changed "
                    f"(existing: {existing[name].storage_mode!r}, "
                    f"declared: {opts['storage_mode']!r}); skipping."
                )
    return result


def get_absurd_backends() -> dict[str, AbsurdBackend]:
    return {
        alias: be
        for alias in task_backends
        if isinstance((be := task_backends[alias]), AbsurdBackend)
    }
