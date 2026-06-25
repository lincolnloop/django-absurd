import typing as t
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta

from absurd_sdk import Absurd, CreateQueueOptions
from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.db.utils import ProgrammingError

from django_absurd.backends import (
    AbsurdBackend,
    get_absurd_backends,
    get_declared_queues,
)
from django_absurd.connection import build_absurd_client, validate_backend
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

INTERVAL_OPTION_KEYS = frozenset(
    ("partition_lookahead", "partition_lookback", "cleanup_ttl", "detach_min_age")
)


@dataclass
class SyncResult:
    created: list[str] = field(default_factory=list)
    reconciled: list[str] = field(default_factory=list)
    storage_warnings: list[str] = field(default_factory=list)


def get_absurd_database(backend: AbsurdBackend) -> str:
    return backend.database


def resolve_absurd_database() -> str:
    databases = {be.database for be in get_absurd_backends().values()}
    if len(databases) == 1:
        return next(iter(databases))
    return "default"


def get_absurd_client(using: str | None = None) -> Absurd:
    return build_absurd_client(using or resolve_absurd_database())


def reconcile_queue(backend: AbsurdBackend, queue_name: str) -> SyncResult:
    db = backend.database
    validate_backend(db)
    declared = get_declared_queues(backend)
    if queue_name not in declared:
        msg = f"Queue {queue_name!r} is not declared in TASKS QUEUES."
        raise ImproperlyConfigured(msg)
    opts = declared[queue_name]
    result = SyncResult()
    client = build_absurd_client(db)
    try:
        row = Queue.objects.using(db).filter(queue_name=queue_name).first()
    except ProgrammingError as exc:
        msg = "Absurd schema is not installed. Run: manage.py migrate"
        raise ImproperlyConfigured(msg) from exc
    if row is None:
        client.create_queue(queue_name, **opts)
        result.created.append(queue_name)
    else:
        mutable_opts = {k: v for k, v in opts.items() if k in MUTABLE_OPTION_KEYS}
        if mutable_opts and check_mutable_options_drifted(db, mutable_opts, row):
            client.set_queue_policy(queue_name, **mutable_opts)
            result.reconciled.append(queue_name)
        if "storage_mode" in opts and opts["storage_mode"] != row.storage_mode:
            result.storage_warnings.append(
                f"Queue '{queue_name}': storage_mode cannot be changed "
                f"(existing: {row.storage_mode!r}, "
                f"declared: {opts['storage_mode']!r}); skipping."
            )
    return result


def sync_queues(backend: AbsurdBackend) -> SyncResult:
    result = SyncResult()
    for name in get_declared_queues(backend):
        r = reconcile_queue(backend, name)
        result.created.extend(r.created)
        result.reconciled.extend(r.reconciled)
        result.storage_warnings.extend(r.storage_warnings)
    return result


def parse_interval(using: str, interval_str: str) -> timedelta:
    with connections[using].cursor() as cur:
        cur.execute("SELECT %s::interval", [interval_str])
        return cur.fetchone()[0]


def check_mutable_options_drifted(
    using: str, opts: dict[str, t.Any], row: Queue
) -> bool:
    for key, declared_value in opts.items():
        db_value = getattr(row, key)
        if key in INTERVAL_OPTION_KEYS:
            if parse_interval(using, declared_value) != db_value:
                return True
        elif declared_value != db_value:
            return True
    return False
