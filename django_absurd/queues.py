import contextlib
import datetime as dt
import logging
import re
import typing as t
import zlib
from dataclasses import dataclass, field

import psycopg.errors
from absurd_sdk import Absurd, CreateQueueOptions, QueuePolicyOptions
from django.db import connections, transaction

from django_absurd import backends
from django_absurd.admin_views import rebuild_views
from django_absurd.connection import build_absurd_client, validate_backend
from django_absurd.exceptions import SchemaNotInstalledError

if t.TYPE_CHECKING:
    from django_absurd.models import Queue

logger = logging.getLogger(__name__)

# Per-queue table prefixes absurd.create_queue names for every queue: tasks, runs,
# checkpoints, events, waiters. Truncate, both missing-table probes and the
# UndefinedTable classifier all read this one tuple.
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

# Advisory-lock key for provision_backend. Derived from a name rather than written as a
# literal so it reads as ours; concurrent sessions only have to agree with each other,
# never across servers or versions.
PROVISION_LOCK_KEY = zlib.crc32(b"django_absurd.provision")


@dataclass
class SyncResult:
    created: list[str] = field(default_factory=list)
    reconciled: list[str] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)


@dataclass
class QueuePlan:
    """What one declared queue needs — carrying the arguments that would satisfy it, not
    just flags, so the write path applies a decision it never re-derives.
    """

    queue_name: str
    create_options: CreateQueueOptions | None = None
    repair: bool = False
    policy_options: QueuePolicyOptions | None = None


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
    db = backend.database
    validate_backend(db)
    plan = plan_queue(db, queue_name, backends.get_declared_queues(backend)[queue_name])
    apply_queue_plan(build_absurd_client(db), plan)
    return summarize_queue_plans([plan])


def plan_queue_sync(backend: backends.AbsurdBackend) -> SyncResult:
    """What ``provision_backend`` would do, without doing any of it.

    Shares ``plan_queue`` with the write path, so the two cannot classify a queue
    differently. Nothing here writes, so a role with no DDL rights can still ask, and
    the provisioning lock keeps covering the writes alone.
    """
    db = backend.database
    validate_backend(db)
    require_installed_schema(db)
    return summarize_queue_plans(
        plan_queue(db, queue_name, opts)
        for queue_name, opts in backends.get_declared_queues(backend).items()
    )


def plan_queue(using: str, queue_name: str, opts: CreateQueueOptions) -> QueuePlan:
    """What ``queue_name`` needs to match ``opts``, decided by reading only.

    The one decision point for both the dry run and the write path: a second copy of
    this branching would let ``--check`` report one thing and the write do another.
    """
    existing = get_queue_object(using, queue_name)
    if existing is None:
        return QueuePlan(queue_name, create_options=opts)
    plan = QueuePlan(queue_name)
    # Puts back the tables of a queue whose catalog row outlived them — a manual drop, a
    # partial restore — the state QueueNotProvisionedError sends an operator here to
    # repair. Gated on their actual absence rather than run unconditionally, which would
    # be needless DDL; when it does run against an out-of-band partitioned row,
    # absurd.create_queue names the mismatch itself.
    plan.repair = bool(find_missing_queue_tables(using, queue_name))
    # MUTABLE_OPTION_KEYS mirrors QueuePolicyOptions's fields exactly; the cast is safe
    # by construction.
    mutable_opts = t.cast(
        "QueuePolicyOptions",
        {k: v for k, v in opts.items() if k in MUTABLE_OPTION_KEYS},
    )
    if mutable_opts and check_mutable_options_drifted(using, mutable_opts, existing):
        plan.policy_options = mutable_opts
    return plan


def apply_queue_plan(client: Absurd, plan: QueuePlan) -> None:
    if plan.create_options is not None:
        client.create_queue(plan.queue_name, **plan.create_options)
    if plan.repair:
        client.create_queue(plan.queue_name)
    if plan.policy_options is not None:
        client.set_queue_policy(plan.queue_name, **plan.policy_options)


def summarize_queue_plans(plans: t.Iterable[QueuePlan]) -> SyncResult:
    result = SyncResult()
    for plan in plans:
        if plan.create_options is not None:
            result.created.append(plan.queue_name)
        if plan.repair:
            result.repaired.append(plan.queue_name)
        if plan.policy_options is not None:
            result.reconciled.append(plan.queue_name)
    return result


def get_queue_object(using: str, queue_name: str) -> "Queue | None":
    """``queue_name``'s ``Queue``, or None.

    Schema absence is classified by the probe below, ahead of the read; the read's own
    ``ProgrammingError`` surfaces as itself, since relabelling it would send the reader
    to the wrong door.
    """
    # The ONE import that would make this module settings-dependent at load time —
    # ``django_absurd.models`` reads INSTALLED_APPS — and our pytest plugin is an
    # entry point, so it loads in ANY venv's pytest run, Django project or not. See
    # tests/core/test_pytest_plugin.py's
    # test_a_pytest_run_with_no_django_settings_still_collects.
    from django_absurd.models import Queue  # noqa: PLC0415

    require_installed_schema(using)
    return Queue.objects.using(using).filter(queue_name=queue_name).first()


def require_installed_schema(using: str) -> None:
    """Raise ``SchemaNotInstalledError`` when ``using`` has no Absurd schema.

    Asked of the whole operation up front, never read off a failed statement. An absent
    schema and a dropped ``absurd.queues`` raise the identical ``UndefinedTable`` while
    only the first is something ``migrate`` can fix, and the statement that hit it has
    already aborted its transaction, so nothing can be asked after the fact. A backend
    declaring no queues reads no queue at all and still rebuilds views off that table.
    """
    with connections[using].cursor() as cursor:
        cursor.execute("select to_regnamespace('absurd') is null")
        if cursor.fetchone()[0]:
            raise SchemaNotInstalledError


def find_missing_queue_tables(using: str, queue_name: str) -> list[str]:
    """Which of ``queue_name``'s own Absurd tables are absent, catalog row aside.

    ``to_regclass`` rather than a ``pg_class`` join: it takes the qualified name and
    answers NULL instead of raising, which is the whole question being asked.
    """
    with connections[using].cursor() as cursor:
        cursor.execute(
            "select name from unnest(%s::text[]) as name "
            "where to_regclass('absurd.' || quote_ident(name)) is null",
            [[f"{prefix}_{queue_name}" for prefix in QUEUE_TABLE_PREFIXES]],
        )
        return [str(row[0]) for row in cursor.fetchall()]


async def afind_missing_queue_tables(
    conn: "psycopg.AsyncConnection[t.Any]", queue_name: str
) -> list[str]:
    """``find_missing_queue_tables``, asked on a worker's own async connection.

    A twin body, not a shared one: a worker holds a raw async connection, and a Django
    cursor inside its loop raises ``SynchronousOnlyOperation``. The two must answer the
    same question, so the ``to_regclass`` test and the prefix set are kept identical —
    change one and change the other.
    """
    async with conn.cursor() as cursor:
        await cursor.execute(
            "select name from unnest(%s::text[]) as name "
            "where to_regclass('absurd.' || quote_ident(name)) is null",
            [[f"{prefix}_{queue_name}" for prefix in QUEUE_TABLE_PREFIXES]],
        )
        return [str(row[0]) for row in await cursor.fetchall()]


def sync_queues(backend: backends.AbsurdBackend) -> SyncResult:
    result = SyncResult()
    for name in backends.get_declared_queues(backend):
        r = reconcile_queue(backend, name)
        result.created.extend(r.created)
        result.reconciled.extend(r.reconciled)
        result.repaired.extend(r.repaired)
    log_sync_result(result)
    return result


def log_sync_result(result: SyncResult) -> None:
    if not result.created and not result.reconciled and not result.repaired:
        logger.info("queues provisioned: no changes")
        return
    logger.info(
        'queues provisioned: created="%s" reconciled="%s" repaired="%s"',
        ", ".join(result.created),
        ", ".join(result.reconciled),
        ", ".join(result.repaired),
    )


def provision_backend(backend: backends.AbsurdBackend) -> SyncResult:
    validate_backend(backend.database)  # the lock below is Postgres-only SQL
    require_installed_schema(backend.database)
    with lock_provisioning(backend.database):
        result = sync_queues(backend)
        rebuild_views(backend.database)
    return result


@contextlib.contextmanager
def lock_provisioning(using: str) -> t.Iterator[None]:
    """Serialize concurrent provisioners, which a deploy runs by the handful.

    Both halves of provisioning create objects by name with no lock held while the name
    is absent — ``CREATE TABLE IF NOT EXISTS`` inside ``absurd.create_queue``, and
    ``CREATE VIEW`` after a ``DROP VIEW IF EXISTS`` that matched nothing — so racing a
    database's first boot collides on a catalog unique index.

    The transaction belongs to the lock, not to the caller: ``pg_advisory_xact_lock``
    lives exactly as long as its transaction, so taken under autocommit it would release
    before the work it guards. Scoping it here also means a crashed provisioner releases
    it, with no stuck-lock cleanup path to own.

    Called inside a caller's own atomic this degrades to a savepoint, so the lock is
    held until THEIR commit — still correct, just longer. No caller here provisions
    inside a transaction: ``absurd_sync_queues`` and the post_migrate receiver run
    outside one, and ``AbsurdTestRuntime.sync_queues`` refuses if one is open.
    """
    with transaction.atomic(using=using):
        with connections[using].cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", [PROVISION_LOCK_KEY])
        yield


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
