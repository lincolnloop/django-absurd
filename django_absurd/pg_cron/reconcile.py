"""pg_cron reconcile engine: materialize declared SCHEDULE entries into
ScheduledTask rows (the rows' post_save signal emits the pg_cron jobs), prune
undeclared ones, and tear down via the explicit ``--teardown`` command — plus the
spawn-option resolution they depend on. Per-row pg_cron job emission lives on the
ScheduledTask model."""

import typing as t

from absurd_sdk import CancellationPolicy, JsonObject, RetryStrategy
from django.utils.module_loading import import_string

from django_absurd.backends import AbsurdBackend
from django_absurd.pg_cron import catalog
from django_absurd.pg_cron.choices import Source
from django_absurd.pg_cron.models import ScheduledTask
from django_absurd.queues import resolve_absurd_database
from django_absurd.scheduler import get_cleanup_schedule, get_settings_schedules
from django_absurd.tasks import build_merged_spawn_options

# The OPTIONS["CLEANUP"] job. It rides the catalog seam like every other
# schedule, on its own db-namespaced lane (source="c" → jobname
# _dj:<app db>:c:cleanup_all), so it's scoped to the app database and swept by
# the same per-database flush.
#
# Deliberately NOT absurd.enable_cron / `absurdctl cron --enable <queue>`, which
# schedule per-queue jobs (absurd_cleanup_<suffix>, absurd_partitions_<suffix>,
# absurd_detach_plan_<suffix>) and need cron.schedule in the app database — which
# the central cron.database_name topology never gives it. Nothing here can see or
# unschedule those names, so drive cleanup ONE way — OPTIONS["CLEANUP"] OR
# `absurdctl cron`, not both (deferred: multi-manager arbitration is out of scope).
CLEANUP_SOURCE = "c"
CLEANUP_NAME = "cleanup_all"
CLEANUP_COMMAND = "select * from absurd.cleanup_all_queues(null::text);"


def resolve_spawn_options(backend: AbsurdBackend, task_path: str) -> JsonObject:
    """Return the normalised spawn options dict for a scheduled task.

    Reproduces the enqueue path's option resolution exactly: task-decorator
    defaults win over the backend's configured DEFAULT_MAX_ATTEMPTS fallback.
    """
    # absurd_sdk._normalize_spawn_options is a module-level helper (bound:
    # absurd-sdk>=0.5.0,<0.6.0) that normalises spawn options into the jsonb dict
    # passed to absurd.spawn_task. We import it directly instead of routing through
    # client.spawn so we get the exact same serialisation without creating a client
    # or touching the DB — and lazily, so an SDK drift breaks pg_cron sync rather
    # than app startup.
    from absurd_sdk import _normalize_spawn_options  # noqa: PLC0415

    task = import_string(task_path)
    defaults = getattr(task.func, "absurd_params", None)
    merged = build_merged_spawn_options(defaults, None)
    merged.setdefault("max_attempts", backend.default_max_attempts)
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
    retry_strategy = t.cast("RetryStrategy | None", opts.get("retry_strategy")) or {}
    cancellation = t.cast("CancellationPolicy | None", opts.get("cancellation")) or {}
    return {
        "queue": queue,
        "max_attempts": opts.get("max_attempts"),
        "retry_kind": retry_strategy.get("kind") or "",
        "retry_base_seconds": retry_strategy.get("base_seconds"),
        "retry_factor": retry_strategy.get("factor"),
        "retry_max_seconds": retry_strategy.get("max_seconds"),
        "cancellation_max_duration": cancellation.get("max_duration"),
        "cancellation_max_delay": cancellation.get("max_delay"),
        "headers": opts.get("headers"),
        "idempotency_key": opts.get("idempotency_key") or "",
    }


def sync_crons(backend: AbsurdBackend) -> tuple[int, int]:
    """Reconcile ScheduledTask rows for this backend's declared SCHEDULE entries.

    Runs the ordered central-connection body: upsert one row per declared schedule
    (source="settings") — whose post_save commit hook schedules the job — then prune
    undeclared settings rows (each pruned row's commit hook unschedules its job), then
    prune (via the catalog seam) any owned settings job whose row was removed
    out-of-band (signal-less delete), then reconcile the cleanup job — so cron.job
    reconverges to the declared state. The source="admin" scope is never touched. No
    lock — emission is idempotent (upserts) and self-healing (the prune), so concurrent
    reconcilers converge without one. Lost row↔job atomicity is acceptable: the
    run-wrapper re-reads the row on each fire, so a dropped emission is only a missed
    fire / stale cadence, never a wrong or orphan spawn.

    Returns (created, pruned): count of ScheduledTask rows newly created and count
    deleted. A no-op reconcile returns (0, 0) so callers can stay quiet.
    """
    schedules = get_settings_schedules(backend)
    declared_names = [s.name for s in schedules]
    database = resolve_absurd_database()

    created = 0
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
    catalog.prune_jobs(database, source=Source.SETTINGS, keep_names=declared_names)
    reconcile_cleanup_job(backend)

    return created, pruned


def reconcile_cleanup_job(backend: AbsurdBackend) -> None:
    """Schedule or unschedule Absurd's global cleanup job from OPTIONS["CLEANUP"].

    Stateless (no ScheduledTask row) — it rides the catalog seam on its own cleanup lane
    (source="c" → jobname ``_dj:<app db>:c:cleanup_all``), running ``select * from
    absurd.cleanup_all_queues(null::text)`` bound to the app database. A declared
    CLEANUP schedule → schedule that job on the declared cadence (an idempotent upsert);
    an absent one → unschedule it (tolerating an already-gone job). The present-or-not
    decision
    lives here; the cross-database write goes through the generic catalog verbs, not a
    dedicated cleanup verb."""
    cleanup_cron = get_cleanup_schedule(backend)
    alias = resolve_absurd_database()
    if cleanup_cron is not None:
        catalog.schedule_job(
            alias,
            name=CLEANUP_NAME,
            source=CLEANUP_SOURCE,
            cron=cleanup_cron,
            command=CLEANUP_COMMAND,
            active=True,
        )
    else:
        catalog.unschedule_job(alias, name=CLEANUP_NAME, source=CLEANUP_SOURCE)


def sync_admin_crons() -> None:
    """Re-emit the pg_cron jobs for the source="admin" rows (idempotent).

    Admin schedules are authored through the ORM/admin, whose post_save signal emits
    the job. But a row created by a data migration goes through the historical model
    and never fires that signal — so its job is missing. This reconciles every admin
    row at migrate, restoring the row⇔job invariant regardless of how the row arrived.
    cron.schedule is an upsert, so re-emitting an already-scheduled job is harmless.
    It then prunes any admin job whose row is gone (a signal-less row delete — bulk
    operations, direct SQL), symmetric with the settings lane.
    """
    database = resolve_absurd_database()
    names = []
    for scheduled_task in ScheduledTask.objects.using(database).filter(
        source=Source.ADMIN
    ):
        scheduled_task.schedule_pg_cron_job()
        names.append(scheduled_task.name)
    catalog.prune_jobs(database, source=Source.ADMIN, keep_names=names)


def teardown_crons(include_admin: bool = False) -> int:
    """Remove pg_cron jobs and ScheduledTask rows.

    Without include_admin, unschedules the settings lane (_dj:<app db>:s:%) and deletes
    settings rows only, leaving admin schedules (user data) untouched — a narrower,
    general-purpose form. The guarded absurd_sync_crons --teardown command always passes
    include_admin=True, additionally clearing the admin lane (_dj:<app db>:a:%) AND
    deleting their rows — so that teardown is terminal, not undone by the next migrate's
    admin re-emit (that is why the command confirms first). Either way the cleanup
    lane's job is removed.

    Idempotent. Returns removed: count of ScheduledTask rows deleted.
    """
    database = resolve_absurd_database()
    sources = [Source.SETTINGS, Source.ADMIN] if include_admin else [Source.SETTINGS]
    for source in sources:
        catalog.unschedule_jobs_for_database(database, source=source)
    catalog.unschedule_job(database, name=CLEANUP_NAME, source=CLEANUP_SOURCE)

    removed, _ = (
        ScheduledTask.objects.using(database).filter(source__in=sources).delete()
    )
    return removed
