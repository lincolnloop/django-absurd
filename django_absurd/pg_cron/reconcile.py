"""pg_cron reconcile engine: materialize declared SCHEDULE entries into
ScheduledTask rows (the rows' post_save signal emits the pg_cron jobs), prune
undeclared ones, and tear down on scheduler switch — plus the option/effective-queue
resolution they depend on. Per-row pg_cron job emission lives on the ScheduledTask
model."""

import typing as t

from django.utils.module_loading import import_string

from django_absurd.backends import AbsurdBackend, build_merged_spawn_options
from django_absurd.pg_cron.models import ScheduledTask, open_locked_cursor
from django_absurd.queues import resolve_absurd_database
from django_absurd.scheduler import Schedule, get_settings_schedules


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


def get_effective_queue(schedule: Schedule) -> str:
    """Return the queue name a scheduled task will run on.

    Uses the schedule's explicit queue override when set; falls back to the
    task's own queue_name.
    """
    return schedule.queue or import_string(schedule.task).queue_name


def sync_crons(backend: AbsurdBackend) -> tuple[int, int]:
    """Reconcile ScheduledTask rows for this backend's declared SCHEDULE entries.

    Opens a transaction on the absurd database and acquires an advisory lock to
    serialise concurrent reconcilers. Upserts one row per declared schedule
    (source="settings"), then prunes undeclared settings rows for this alias. The
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
    with open_locked_cursor(database):
        for schedule in schedules:
            opts = resolve_spawn_options(backend, schedule)
            _, was_created = ScheduledTask.objects.using(database).update_or_create(
                source="settings",
                alias=backend.alias,
                name=schedule.name,
                defaults={
                    "task": schedule.task,
                    "queue": get_effective_queue(schedule),
                    "args": schedule.args,
                    "kwargs": schedule.kwargs,
                    "max_attempts": opts.get("max_attempts"),
                    "retry_strategy": opts.get("retry_strategy"),
                    "headers": opts.get("headers"),
                    "cancellation": opts.get("cancellation"),
                    "idempotency_key": opts.get("idempotency_key") or "",
                    "cron": schedule.cron,
                    "enabled": True,
                },
            )
            created += was_created

        pruned, _ = (
            ScheduledTask.objects.using(database)
            .filter(source="settings", alias=backend.alias)
            .exclude(name__in=declared_names)
            .delete()
        )
        ScheduledTask.pg_cron.prune_jobs_without_rows(
            backend.alias, "settings", declared_names
        )

    return created, pruned


def sync_admin_crons(backend: AbsurdBackend) -> None:
    """Re-emit the pg_cron jobs for this backend's source="admin" rows (idempotent).

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
            source="admin", alias=backend.alias
        ):
            scheduled_task.schedule_pg_cron_job()
            names.append(scheduled_task.name)
        ScheduledTask.pg_cron.prune_jobs_without_rows(backend.alias, "admin", names)


def teardown_crons(backend: AbsurdBackend, include_admin: bool = False) -> int:
    """Remove pg_cron jobs and ScheduledTask rows owned by this backend alias.

    The migrate-time path (include_admin=False, a scheduler switch away from pg_cron)
    unschedules absurd:settings:<alias>:% jobs and deletes source="settings" rows,
    leaving admin schedules (user data) untouched. The guarded absurd_sync_crons
    --teardown command (include_admin=True) additionally clears absurd:admin:<alias>:%
    jobs AND deletes their rows — so the teardown is terminal, not undone by the next
    migrate's admin re-emit (that is why the command confirms first).

    Idempotent. Returns removed: count of ScheduledTask rows deleted.
    """
    database = resolve_absurd_database()
    sources = ["settings", "admin"] if include_admin else ["settings"]
    for source in sources:
        ScheduledTask.pg_cron.unschedule_matching(backend.alias, source)

    removed, _ = (
        ScheduledTask.objects.using(database)
        .filter(source__in=sources, alias=backend.alias)
        .delete()
    )
    return removed
