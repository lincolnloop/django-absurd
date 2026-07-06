"""pg_cron scheduler helpers — option resolution and effective-queue computation."""

import typing as t

from django.db import DatabaseError, InternalError, connections, transaction
from django.utils.module_loading import import_string

from django_absurd.backends import AbsurdBackend, build_merged_spawn_options
from django_absurd.pg_cron.models import ScheduledJob
from django_absurd.scheduler import Schedule, get_settings_schedules

# Stable advisory lock key that serializes concurrent sync_crons reconcilers.
SYNC_CRONS_ADVISORY_LOCK = 0x616273_75726421  # "absurd!" as hex


def resolve_spawn_options(
    backend: AbsurdBackend, schedule: Schedule
) -> dict[str, t.Any]:
    """Return the normalised spawn options dict for a scheduled task.

    Reproduces the enqueue path's option resolution exactly: task-decorator
    defaults win over the backend's configured DEFAULT_MAX_ATTEMPTS fallback.
    """
    # absurd_sdk._normalize_spawn_options is a module-level helper (bound:
    # absurd-sdk>=0.4.0,<0.5.0) that normalises spawn options into the jsonb dict
    # passed to absurd.spawn_task. We import it directly instead of routing through
    # client.spawn so we get the exact same serialisation without creating a client
    # or touching the DB — and lazily, so an SDK drift breaks pg_cron sync rather
    # than app startup.
    from absurd_sdk import _normalize_spawn_options  # noqa: PLC0415

    task = import_string(schedule.task)
    defaults = getattr(task.func, "absurd_default_params", None)
    merged = build_merged_spawn_options(defaults, None)
    merged["max_attempts"] = merged.pop("max_attempts", backend.default_max_attempts)
    return _normalize_spawn_options(**merged)


def effective_queue(schedule: Schedule) -> str:
    """Return the queue name a scheduled task will run on.

    Uses the schedule's explicit queue override when set; falls back to the
    task's own queue_name.
    """
    return schedule.queue or import_string(schedule.task).queue_name


def build_jobname(alias: str, name: str, source: str = "settings") -> str:
    """Return the pg_cron job name for a scheduled task."""
    return f"absurd:{source}:{alias}:{name}"


def jobname_prefix(alias: str, source: str = "settings") -> str:
    """Return the LIKE prefix used to prune pg_cron jobs for an alias."""
    return f"absurd:{source}:{alias}:"


def sync_crons(backend: AbsurdBackend) -> tuple[int, int]:
    """Reconcile ScheduledJob rows for this backend's declared SCHEDULE entries.

    Opens a transaction on backend.database and acquires an advisory lock to
    serialise concurrent reconcilers. Upserts one row per declared schedule
    (source="settings"), then prunes undeclared settings rows for this alias.
    The source="admin" scope is never touched.

    After the table phase, materializes one pg_cron job per declared entry
    (upsert + active re-arm) and prunes owned-but-undeclared pg_cron jobs.

    Returns (upserted, pruned): count of declared entries synced and count of
    ScheduledJob rows deleted.
    """
    schedules = get_settings_schedules(backend)
    declared_names = [s.name for s in schedules]

    with transaction.atomic(using=backend.database):
        conn = connections[backend.database]
        with conn.cursor() as cur:
            cur.execute("select pg_advisory_xact_lock(%s)", [SYNC_CRONS_ADVISORY_LOCK])

        for schedule in schedules:
            ScheduledJob.objects.using(backend.database).update_or_create(
                source="settings",
                alias=backend.alias,
                name=schedule.name,
                defaults={
                    "task": schedule.task,
                    "queue": effective_queue(schedule),
                    "params": {"args": schedule.args, "kwargs": schedule.kwargs},
                    "options": resolve_spawn_options(backend, schedule),
                    "cron": schedule.cron,
                    "enabled": True,
                },
            )

        pruned, _ = (
            ScheduledJob.objects.using(backend.database)
            .filter(source="settings", alias=backend.alias)
            .exclude(name__in=declared_names)
            .delete()
        )

        sync_pg_cron_jobs(backend, schedules)

    return len(schedules), pruned


# cron.schedule / cron.unschedule / cron.alter_job are pg_cron catalog functions,
# not part of the Absurd SDK (which covers spawn/queues/claim only). Raw SQL here
# is inherent to this DB-side scheduler backend.
#
# psycopg scans the whole query for %, so SQL format()'s %L placeholders must be
# doubled to %%L; the bound params carry an explicit ::text cast. Without both,
# psycopg raises "only '%s','%b','%t' are allowed". Building the command
# server-side means runtime arg values can never inject into the scheduled SQL.
SCHEDULE_JOB_SQL = (
    "select cron.schedule(%s, %s, "
    "format('select public.django_absurd_run_scheduled(%%L, %%L, %%L)', "
    "%s::text, %s::text, %s::text))"
)


def sync_pg_cron_jobs(backend: AbsurdBackend, schedules: list[Schedule]) -> None:
    """Upsert one pg_cron job per declared schedule and prune stale ones.

    Runs inside sync_crons' transaction on backend.database. Each declared entry
    is (re)scheduled with a constant wrapper command and re-armed to active;
    pg_cron jobs owned by this alias but no longer declared are unscheduled.
    """
    conn = connections[backend.database]
    prefix = jobname_prefix(backend.alias)
    declared_jobnames = [build_jobname(backend.alias, s.name) for s in schedules]

    with conn.cursor() as cur:
        for schedule in schedules:
            jobname = build_jobname(backend.alias, schedule.name)
            cur.execute(
                SCHEDULE_JOB_SQL,
                [jobname, schedule.cron, "settings", backend.alias, schedule.name],
            )
            jobid = cur.fetchone()[0]
            cur.execute("select cron.alter_job(%s, active := true)", [jobid])

        stale_jobids = find_stale_pg_cron_jobids(cur, prefix, declared_jobnames)
        prune_pg_cron_jobs(cur, stale_jobids)


def teardown_crons(backend: AbsurdBackend) -> int:
    """Remove every pg_cron job and ScheduledJob row owned by this backend alias.

    Opens a transaction on backend.database and acquires the same advisory lock
    used by sync_crons to serialise concurrent reconcilers. All jobs matching the
    absurd:settings:<alias>:% prefix are unscheduled via the savepoint-swallow
    helper (tolerating already-gone rows). All source="settings" ScheduledJob rows
    for this alias are deleted; source="admin" rows are left untouched.

    Idempotent: a second call with no owned jobs or rows is a clean no-op.

    Returns removed: count of ScheduledJob rows deleted.
    """
    with transaction.atomic(using=backend.database):
        conn = connections[backend.database]
        with conn.cursor() as cur:
            cur.execute("select pg_advisory_xact_lock(%s)", [SYNC_CRONS_ADVISORY_LOCK])

            prefix = jobname_prefix(backend.alias)
            owned_jobids = find_owned_pg_cron_jobids(cur, prefix)
            prune_pg_cron_jobs(cur, owned_jobids)

        removed, _ = (
            ScheduledJob.objects.using(backend.database)
            .filter(source="settings", alias=backend.alias)
            .delete()
        )

    return removed


def find_stale_pg_cron_jobids(
    cur: t.Any, prefix: str, declared_jobnames: list[str]
) -> list[int]:
    """Return jobids of pg_cron jobs matching prefix that are no longer declared."""
    cur.execute(
        "select jobid from cron.job where jobname like %s and not (jobname = any(%s))",
        [prefix + "%", declared_jobnames],
    )
    return [row[0] for row in cur.fetchall()]


def find_owned_pg_cron_jobids(cur: t.Any, prefix: str) -> list[int]:
    """Return all jobids of pg_cron jobs matching the given prefix."""
    cur.execute(
        "select jobid from cron.job where jobname like %s",
        [prefix + "%"],
    )
    return [row[0] for row in cur.fetchall()]


def prune_pg_cron_jobs(cur: t.Any, stale_jobids: list[int]) -> None:
    """Unschedule each stale pg_cron jobid, tolerating already-removed rows.

    Each unschedule runs inside its own savepoint: if the job's cron.job row was
    removed out-of-band between the stale-id scan and this call, cron.unschedule
    raises InternalError (SQLSTATE XX000, "could not find valid entry"); we roll
    back to the savepoint and continue rather than abort the whole reconcile.
    """
    for jobid in stale_jobids:
        cur.execute("savepoint prune_sp")
        try:
            cur.execute("select cron.unschedule(%s)", [jobid])
        except (InternalError, DatabaseError) as exc:
            if "could not find" not in str(exc):
                raise
            cur.execute("rollback to savepoint prune_sp")
        else:
            cur.execute("release savepoint prune_sp")
