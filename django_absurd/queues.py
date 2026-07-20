import datetime as dt
import typing as t
from dataclasses import dataclass, field

from absurd_sdk import Absurd, QueuePolicyOptions
from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.db.utils import ProgrammingError

from django_absurd import backends
from django_absurd.admin_views import rebuild_views
from django_absurd.connection import build_absurd_client, validate_backend
from django_absurd.models import Queue

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


def get_absurd_database(backend: backends.AbsurdBackend) -> str:
    return backend.database


def resolve_absurd_database() -> str:
    databases = {be.database for be in backends.get_absurd_backends().values()}
    if len(databases) == 1:
        return next(iter(databases))
    return "default"


def get_absurd_backend() -> backends.AbsurdBackend | None:
    target = resolve_absurd_database()
    for be in backends.get_absurd_backends().values():
        if be.database == target:
            return be
    return None


def get_absurd_client(using: str | None = None) -> Absurd:
    return build_absurd_client(using or resolve_absurd_database())


def reconcile_queue(backend: backends.AbsurdBackend, queue_name: str) -> SyncResult:
    db = backend.database
    validate_backend(db)
    opts = backends.get_declared_queues(backend)[queue_name]
    result = SyncResult()
    client = build_absurd_client(db)
    try:
        existing = Queue.objects.using(db).filter(queue_name=queue_name).first()
    except ProgrammingError as exc:
        msg = "Absurd schema is not installed. Run: manage.py migrate"
        raise ImproperlyConfigured(msg) from exc
    if existing is None:
        client.create_queue(queue_name, **opts)
        result.created.append(queue_name)
    else:
        # MUTABLE_OPTION_KEYS mirrors QueuePolicyOptions's fields exactly; the cast is
        # safe by construction.
        mutable_opts = t.cast(
            "QueuePolicyOptions",
            {k: v for k, v in opts.items() if k in MUTABLE_OPTION_KEYS},
        )
        if mutable_opts and check_mutable_options_drifted(db, mutable_opts, existing):
            client.set_queue_policy(queue_name, **mutable_opts)
            result.reconciled.append(queue_name)
        if "storage_mode" in opts and opts["storage_mode"] != existing.storage_mode:
            result.storage_warnings.append(
                f"Queue '{queue_name}': storage_mode cannot be changed "
                f"(existing: {existing.storage_mode!r}, "
                f"declared: {opts['storage_mode']!r}); skipping."
            )
    return result


def sync_queues(backend: backends.AbsurdBackend) -> SyncResult:
    result = SyncResult()
    for name in backends.get_declared_queues(backend):
        r = reconcile_queue(backend, name)
        result.created.extend(r.created)
        result.reconciled.extend(r.reconciled)
        result.storage_warnings.extend(r.storage_warnings)
    return result


def provision_backend(backend: backends.AbsurdBackend) -> SyncResult:
    # The single integral provisioning step (used by post_migrate, the sync command,
    # and worker start): reconcile every declared queue, then rebuild all admin views
    # so they reflect the full catalog — not just the queue a worker happens to serve.
    result = sync_queues(backend)
    rebuild_views(backend.database)
    return result


def parse_interval(using: str, interval_str: str) -> dt.timedelta:
    with connections[using].cursor() as cur:
        cur.execute("SELECT %s::interval", [interval_str])
        record: tuple[dt.timedelta, ...] = cur.fetchone()
        return record[0]


def check_mutable_options_drifted(
    using: str, opts: QueuePolicyOptions, existing: Queue
) -> bool:
    for key, declared_value in opts.items():
        db_value = getattr(existing, key)
        if key in INTERVAL_OPTION_KEYS:
            # Every INTERVAL_OPTION_KEYS member is a str field on QueuePolicyOptions.
            interval_str = t.cast("str", declared_value)
            if parse_interval(using, interval_str) != db_value:
                return True
        elif declared_value != db_value:
            return True
    return False
