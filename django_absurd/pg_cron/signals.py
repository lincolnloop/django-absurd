"""(Un)schedule a pg_cron job whenever a ScheduledTask row is saved or deleted.

The single emission path for `.save()`/`.delete()`: settings reconcile upserts, admin
authoring, direct ORM save/delete, AND loaddata all flow through here, so pg_cron
matches the rows — the row is the source of truth, so a loaded/restored schedule is a
live schedule (cron.schedule is an idempotent upsert). NOTE: `QuerySet.update()` /
`bulk_create()` / `bulk_update()` send no post_save and so DON'T (re)schedule — don't
use them to change a schedule on this model. Receivers do not swallow exceptions: a
failing pg_cron op propagates, so when the write is inside a transaction (admin,
loaddata, an explicit atomic) the row write rolls back with it. A bare autocommit
``create()``/``save()`` commits the row first, so a then-failing pg_cron op leaves a
committed row without its job — wrap such writes in ``transaction.atomic`` for
both-or-neither.

A ScheduledTask only works on the single absurd database (the run-wrapper reads the row
from the DB it runs in), so a pre_save receiver rejects a write forced onto another
database BEFORE the row is inserted; the delete receiver instead SKIPS such a row (a
stray row created out-of-band on another DB must stay deletable).
"""

import typing as t

if t.TYPE_CHECKING:
    from django_absurd.pg_cron.models import ScheduledTask


def reject_cross_database_save(
    sender: type, instance: "ScheduledTask", using: str | None = None, **kwargs: t.Any
) -> None:
    """pre_save: reject a write forced onto a non-absurd database before the INSERT, so
    no misplaced row is created. Cross-database schedules belong to the multi-Absurd-
    database feature, which isn't built yet."""
    if is_foreign_database(using):
        msg = (
            f"ScheduledTask was written to database {using!r}, but Absurd schedules "
            f"live only on {resolve_absurd_database_lazily()!r} "
            "(the run-wrapper reads there). "
            "Cross-database schedule writes are not supported."
        )
        raise NotImplementedError(msg)


def schedule_job_on_save(
    sender: type, instance: "ScheduledTask", **kwargs: t.Any
) -> None:
    """post_save: (re)schedule the row's pg_cron job. Fires only for a write that
    reached the absurd DB — pre_save rejects a cross-database write before this."""
    instance.schedule_pg_cron_job()


def unschedule_job_on_delete(
    sender: type, instance: "ScheduledTask", using: str | None = None, **kwargs: t.Any
) -> None:
    """post_delete: remove the row's pg_cron job. Skips a row deleted from a foreign
    database — nothing of ours to unschedule there — so a stray row created out-of-band
    on another DB stays deletable rather than being trapped by a raising guard."""
    if is_foreign_database(using):
        return
    instance.unschedule_pg_cron_job()


def is_foreign_database(using: str | None) -> bool:
    """True when a save/delete targeted a database other than the single absurd one."""
    return using is not None and using != resolve_absurd_database_lazily()


def resolve_absurd_database_lazily() -> str:
    from django_absurd.queues import resolve_absurd_database  # noqa: PLC0415

    return resolve_absurd_database()
