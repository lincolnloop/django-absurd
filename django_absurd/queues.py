import datetime as dt
import logging
import re
import typing as t
from dataclasses import dataclass, field

import psycopg.errors
from absurd_sdk import Absurd, QueuePolicyOptions
from django.db import connections
from django.db.utils import ProgrammingError

from django_absurd import backends
from django_absurd.admin_views import rebuild_views
from django_absurd.connection import build_absurd_client, validate_backend
from django_absurd.exceptions import SchemaNotInstalledError

if t.TYPE_CHECKING:
    from django_absurd.models import Queue

logger = logging.getLogger(__name__)

# Per-queue table prefixes, as absurd.create_queue names them: tasks, runs, checkpoints,
# events, waiters. (``i_<queue>`` exists for a partitioned queue too, but only spawn and
# cleanup touch it — nothing a drain runs can miss it.)
QUEUE_TABLE_PREFIXES = ("t", "r", "c", "e", "w")

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


def list_provisioned_queues(using: str | None = None) -> list[str]:
    client = get_absurd_client(using)
    try:
        return sorted(client.list_queues())
    except (
        psycopg.errors.InvalidSchemaName,
        psycopg.errors.UndefinedFunction,
        psycopg.errors.UndefinedTable,
    ) as exc:
        raise SchemaNotInstalledError from exc


def names_a_queue_table(exc: psycopg.errors.UndefinedTable, queue: str) -> bool:
    """Report whether ``exc`` is about one of ``queue``'s own Absurd tables.

    Read off ``diag.message_primary`` (``relation "absurd.r_default" does not exist``),
    not ``diag.table_name``: Postgres populates no table field for SQLSTATE 42P01 —
    verified, it is always ``None`` — since a relation that does not exist has no OID to
    name. The match is on the relation NAME, which Postgres never translates, so a
    server running a localised ``lc_messages`` classifies the same way an English one
    does.

    Word-bounded, so an unrelated ``audit_default`` is not read as this queue's
    ``t_default``. ``message_primary`` alone, never ``str(exc)``: the latter carries
    the failing statement as CONTEXT, and the claim statement names every queue table,
    so an unrelated failure inside it would look like a provisioning problem.
    """
    message = exc.diag.message_primary or ""
    return any(
        re.search(rf"\b{re.escape(prefix)}_{re.escape(queue)}\b", message)
        for prefix in QUEUE_TABLE_PREFIXES
    )


def reconcile_queue(backend: backends.AbsurdBackend, queue_name: str) -> SyncResult:
    # The ONE import that would make this module settings-dependent at load time:
    # ``django_absurd.models`` defines model classes (``build_admin_model``), so
    # importing it reads INSTALLED_APPS. Keeping it in here is what lets
    # ``django_absurd.pytest_plugin``/``.test``/``.flush`` import this module at their
    # own top level during pytest's bootstrap, in any venv, before Django is configured.
    # Move it to the top of this module and every pytest run in a non-Django project
    # dies with INTERNALERROR (see tests/core/test_pytest_plugin.py's
    # test_a_pytest_run_with_no_django_settings_still_collects).
    from django_absurd.models import Queue  # noqa: PLC0415

    db = backend.database
    validate_backend(db)
    opts = backends.get_declared_queues(backend)[queue_name]
    result = SyncResult()
    client = build_absurd_client(db)
    try:
        existing = Queue.objects.using(db).filter(queue_name=queue_name).first()
    except ProgrammingError as exc:
        raise SchemaNotInstalledError from exc
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
    log_sync_result(result)
    return result


def log_sync_result(result: SyncResult) -> None:
    if not result.created and not result.reconciled:
        logger.info("queues provisioned: no changes")
        return
    logger.info(
        "queues provisioned: created=%s reconciled=%s",
        ", ".join(result.created),
        ", ".join(result.reconciled),
    )


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
    using: str, opts: QueuePolicyOptions, existing: "Queue"
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
