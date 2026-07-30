"""(Un)schedule a pg_cron job whenever a ScheduledTask row is saved or deleted.

The single emission path for `.save()`/`.delete()`: settings reconcile upserts, admin
authoring, direct ORM save/delete, AND loaddata all flow through here, so pg_cron
matches the rows — the row is the source of truth, so a loaded/restored schedule is a
live schedule (cron.schedule is an idempotent upsert). NOTE: `QuerySet.update()` /
`bulk_create()` / `bulk_update()` send no post_save and so DON'T (re)schedule — don't
use them to change a schedule on this model.

TWO CONNECTIONS. The pg_cron catalog (cron.job) lives on a SEPARATE central database,
not the row's own connection, so emission is deferred to ``transaction.on_commit``:
the job is (un)scheduled on the central connection only AFTER the row's transaction
commits. A central-connection failure that lands AFTER the row committed is
swallowed-and-logged, never a 500 on an already-saved row — the next reconcile
self-heals it.

NO LOCK. Concurrent writers converge without one: each emission is an idempotent
upsert (``cron.schedule_in_database``) / ``update_or_create``, and reconcile's prune
self-heals divergence. The row↔job pair is no longer atomic (the central write can be
lost while the row survives), which is acceptable because the run-wrapper RE-READS the
row on each fire — so divergence is only a missed fire / stale cadence, never a wrong
or orphan spawn.

A ScheduledTask only works on the single absurd database (the run-wrapper reads the row
from the DB it runs in), so a pre_save receiver rejects a write forced onto another
database BEFORE the row is inserted; the delete receiver instead SKIPS such a row (a
stray row created out-of-band on another DB must stay deletable).
"""

import logging
import typing as t

import psycopg
from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError, transaction

from django_absurd.queues import resolve_absurd_database

if t.TYPE_CHECKING:
    from django_absurd.pg_cron.models import ScheduledTask

logger = logging.getLogger("django_absurd")


def reject_cross_database_save(
    sender: type, instance: "ScheduledTask", using: str | None = None, **kwargs: t.Any
) -> None:
    """pre_save: reject a write forced onto a non-absurd database before the INSERT, so
    no misplaced row is created. Cross-database schedules belong to the multi-Absurd-
    database feature, which isn't built yet."""
    if is_foreign_database(using):
        msg = (
            f"ScheduledTask was written to database {using!r}, but Absurd schedules "
            f"live only on {resolve_absurd_database()!r} "
            "(the run-wrapper reads there). "
            "Cross-database schedule writes are not supported."
        )
        raise NotImplementedError(msg)


def schedule_job_on_save(
    sender: type, instance: "ScheduledTask", using: str | None = None, **kwargs: t.Any
) -> None:
    """post_save: register a commit hook that (re)schedules the row's pg_cron job on the
    central connection AFTER the row's transaction commits. Fires only for a write that
    reached the absurd DB — pre_save rejects a cross-database write before this."""
    transaction.on_commit(
        lambda: emit_schedule_change(instance.schedule_pg_cron_job), using=using
    )


def unschedule_job_on_delete(
    sender: type, instance: "ScheduledTask", using: str | None = None, **kwargs: t.Any
) -> None:
    """post_delete: register a commit hook that removes the row's pg_cron job after the
    delete commits. Skips a row deleted from a foreign database — nothing of ours to
    unschedule there — so a stray row created out-of-band on another DB stays deletable
    rather than being trapped by a raising guard."""
    if is_foreign_database(using):
        return
    transaction.on_commit(
        lambda: emit_schedule_change(instance.unschedule_pg_cron_job), using=using
    )


def emit_schedule_change(catalog_op: t.Callable[[], None]) -> None:
    """Run a post-commit catalog (un)schedule on the central connection,
    swallowing-and-logging a failure. The row is already committed, so a central-
    connection error must NOT surface as a 500 — the next reconcile self-heals the
    missing/stale job. Catches the central-connection failure surface (a raw
    ``psycopg.Error`` when the connect itself fails, a translated ``DatabaseError`` when
    a statement does), broadly because this is best-effort heal, not the row's write
    path."""
    try:
        catalog_op()
    # ImproperlyConfigured covers post-migrate config drift: pg_cron present at
    # migrate but later dropped from shared_preload_libraries, so resolve_cron_database
    # finds cron.database_name NULL — in autocommit the on_commit callback runs inside
    # .save(), so this must be swallowed too, never a 500 on an already-committed row.
    except (DatabaseError, ImproperlyConfigured, psycopg.Error):
        logger.warning(
            "django-absurd: pg_cron schedule emission failed after commit",
            exc_info=True,
        )


def is_foreign_database(using: str | None) -> bool:
    """True when a save/delete targeted a database other than the single absurd one."""
    return using is not None and using != resolve_absurd_database()
