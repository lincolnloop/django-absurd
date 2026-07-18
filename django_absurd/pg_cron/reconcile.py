"""pg_cron reconcile engine: materialize declared SCHEDULE entries into
ScheduledTask rows (the rows' post_save signal emits the pg_cron jobs), prune
undeclared ones, and tear down on scheduler switch — plus the spawn-option
resolution they depend on. Per-row pg_cron job emission lives on the ScheduledTask
model."""

import typing as t

from django.utils.module_loading import import_string

from django_absurd.backends import AbsurdBackend, build_merged_spawn_options
from django_absurd.pg_cron.choices import Source
from django_absurd.pg_cron.models import (
    ScheduledTask,
    open_locked_cursor,
    prune_pg_cron_jobs,
)
from django_absurd.queues import resolve_absurd_database
from django_absurd.scheduler import get_cleanup_schedule, get_settings_schedules

# Absurd's OWN global cleanup job: reconcile schedules/unschedules exactly this
# identity — the same jobname and command that absurd.enable_cron / `absurdctl cron
# --enable` use — so django-absurd and absurdctl reference one shared job rather than
# forking a parallel one. It lives outside our managed ``_dj:`` (colon) namespace,
# so get_managed_jobs() never sweeps it. When ``django_absurd.pg_cron`` is installed,
# django-absurd is AUTHORITATIVE over this job: it schedules it from OPTIONS["CLEANUP"]
# and removes it otherwise — including at migrate teardown / scheduler-flip even when
# CLEANUP was never set — so a job created via ``absurdctl cron`` is reclaimed and
# removed. Drive cleanup ONE way — OPTIONS["CLEANUP"] OR `absurdctl cron`, not both
# (deferred: multi-manager cleanup-job arbitration is out of scope here).
ABSURD_CLEANUP_JOB = "absurd_cleanup_all"
CLEANUP_COMMAND = "select * from absurd.cleanup_all_queues(null::text);"


def resolve_spawn_options(backend: AbsurdBackend, task_path: str) -> dict[str, t.Any]:
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

    task = import_string(task_path)
    defaults = getattr(task.func, "absurd_default_params", None)
    merged = build_merged_spawn_options(defaults, None)
    merged["max_attempts"] = merged.pop("max_attempts", backend.default_max_attempts)
    return _normalize_spawn_options(**merged)


def build_scheduled_fields(
    backend: AbsurdBackend,
    task_path: str,
    *,
    queue_override: str | None = None,
) -> dict[str, t.Any]:
    """Return the ten spawn-option columns for a scheduled task row.

    Resolves decorator defaults against the backend's fallback, then flattens
    nested retry_strategy / cancellation dicts into their typed sub-columns.
    Does not include schedule-owned keys (task, args, kwargs, cron, enabled).
    """
    task = import_string(task_path)
    queue = queue_override or task.queue_name
    opts = resolve_spawn_options(backend, task_path)
    return {
        "queue": queue,
        "max_attempts": opts.get("max_attempts"),
        "retry_kind": (opts.get("retry_strategy") or {}).get("kind") or "",
        "retry_base_seconds": (opts.get("retry_strategy") or {}).get("base_seconds"),
        "retry_factor": (opts.get("retry_strategy") or {}).get("factor"),
        "retry_max_seconds": (opts.get("retry_strategy") or {}).get("max_seconds"),
        "cancellation_max_duration": (opts.get("cancellation") or {}).get(
            "max_duration"
        ),
        "cancellation_max_delay": (opts.get("cancellation") or {}).get("max_delay"),
        "headers": opts.get("headers"),
        "idempotency_key": opts.get("idempotency_key") or "",
    }


def sync_crons(backend: AbsurdBackend) -> tuple[int, int]:
    """Reconcile ScheduledTask rows for this backend's declared SCHEDULE entries.

    Opens a transaction on the absurd database and acquires an advisory lock to
    serialise concurrent reconcilers. Upserts one row per declared schedule
    (source="settings"), then prunes undeclared settings rows. The
    source="admin" scope is never touched. The pg_cron jobs follow: each row upsert
    fires post_save → schedule; each pruned row fires post_delete → unschedule. Finally
    it prunes any owned settings job whose row was removed out-of-band (signal-less
    delete), so cron.job reconverges to the declared state.

    Returns (created, pruned): count of ScheduledTask rows newly created and count
    deleted. A no-op reconcile returns (0, 0) so callers can stay quiet.
    """
    schedules = get_settings_schedules(backend)
    declared_names = [s.name for s in schedules]
    database = resolve_absurd_database()

    created = 0
    with open_locked_cursor(database) as cur:
        for schedule in schedules:
            spawn_fields = build_scheduled_fields(
                backend, schedule.task, queue_override=schedule.queue
            )
            _, was_created = ScheduledTask.objects.using(database).update_or_create(
                source=Source.SETTINGS,
                name=schedule.name,
                defaults={
                    "task": schedule.task,
                    "args": schedule.args,
                    "kwargs": schedule.kwargs,
                    "cron": schedule.cron,
                    "enabled": True,
                    **spawn_fields,
                },
            )
            created += was_created

        pruned, _ = (
            ScheduledTask.objects.using(database)
            .filter(source=Source.SETTINGS)
            .exclude(name__in=declared_names)
            .delete()
        )
        ScheduledTask.pg_cron.prune_jobs_without_rows(Source.SETTINGS, declared_names)
        reconcile_cleanup_job(cur, backend)

    return created, pruned


def reconcile_cleanup_job(cur: t.Any, backend: AbsurdBackend) -> None:
    """Schedule or unschedule Absurd's global cleanup job from OPTIONS["CLEANUP"].

    Stateless (no ScheduledTask row). This targets Absurd's OWN global cleanup job —
    jobname ``absurd_cleanup_all`` running ``select * from
    absurd.cleanup_all_queues(null::text)`` — deliberately the same identity
    ``absurd.enable_cron`` / ``absurdctl cron --enable`` uses, so the two never fork a
    parallel job. A declared CLEANUP schedule → upsert that job on the declared cadence
    (cron.schedule is an idempotent upsert); an absent one → unschedule it (tolerating
    an already-gone job). Because ``django_absurd.pg_cron`` is installed, django-absurd
    is AUTHORITATIVE over this job: it schedules it from OPTIONS["CLEANUP"] and removes
    it otherwise — including at migrate teardown / scheduler-flip even when CLEANUP was
    never set — so a job created via ``absurdctl cron`` is reclaimed and removed. Drive
    cleanup ONE way — OPTIONS["CLEANUP"] OR ``absurdctl cron``, not both
    (deferred: multi-manager cleanup-job arbitration is out of scope here).
    Runs on the caller's already-locked cursor so it shares the reconcile's advisory
    lock and transaction.
    """
    cleanup_cron = get_cleanup_schedule(backend)
    if cleanup_cron is not None:
        cur.execute(
            "select cron.schedule(%s, %s, %s)",
            [ABSURD_CLEANUP_JOB, cleanup_cron, CLEANUP_COMMAND],
        )
    else:
        unschedule_cleanup_job(cur)


def unschedule_cleanup_job(cur: t.Any) -> None:
    """Remove Absurd's global cleanup job, tolerating an already-gone job."""
    cur.execute("select jobid from cron.job where jobname = %s", [ABSURD_CLEANUP_JOB])
    prune_pg_cron_jobs(cur, [jobid for (jobid,) in cur.fetchall()])


def sync_admin_crons() -> None:
    """Re-emit the pg_cron jobs for the source="admin" rows (idempotent).

    Admin schedules are authored through the ORM/admin, whose post_save signal emits
    the job. But a row created by a data migration goes through the historical model
    and never fires that signal — so its job is missing. This reconciles every admin
    row at migrate, restoring the row⇔job invariant regardless of how the row arrived.
    cron.schedule is an upsert, so re-emitting an already-scheduled job is harmless.
    It then prunes any admin job whose row is gone (a backend flipped off pg_cron then
    the row deleted, or a signal-less row delete), symmetric with the settings lane.
    """
    database = resolve_absurd_database()
    with open_locked_cursor(database):
        names = []
        for scheduled_task in ScheduledTask.objects.using(database).filter(
            source=Source.ADMIN
        ):
            scheduled_task.schedule_pg_cron_job()
            names.append(scheduled_task.name)
        ScheduledTask.pg_cron.prune_jobs_without_rows(Source.ADMIN, names)


def teardown_crons(include_admin: bool = False) -> int:
    """Remove pg_cron jobs and ScheduledTask rows.

    The migrate-time path (include_admin=False, a scheduler switch away from pg_cron)
    unschedules _dj:s:% jobs and deletes settings rows, leaving admin schedules (user
    data) untouched. The guarded absurd_sync_crons --teardown command
    (include_admin=True) additionally clears _dj:a:% jobs AND deletes their rows — so
    the teardown is terminal, not undone by the next migrate's admin re-emit (that is
    why the command confirms first).

    Idempotent. Returns removed: count of ScheduledTask rows deleted.
    """
    database = resolve_absurd_database()
    sources = [Source.SETTINGS, Source.ADMIN] if include_admin else [Source.SETTINGS]
    for source in sources:
        ScheduledTask.pg_cron.unschedule_matching(source)

    with open_locked_cursor(database) as cur:
        unschedule_cleanup_job(cur)

    removed, _ = (
        ScheduledTask.objects.using(database).filter(source__in=sources).delete()
    )
    return removed
