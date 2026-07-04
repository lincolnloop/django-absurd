"""pg_cron scheduler helpers — option resolution and effective-queue computation."""

import typing as t

# absurd_sdk._normalize_spawn_options is a module-level helper (pinned: absurd-sdk>=0.1)
# that normalises spawn options into the jsonb dict passed to absurd.spawn_task.
# We import it directly instead of routing through client.spawn so we get the
# exact same serialisation without creating a client or touching the DB.
from absurd_sdk import _normalize_spawn_options
from django.db import connections, transaction
from django.utils.module_loading import import_string

from django_absurd.backends import AbsurdBackend, build_merged_spawn_options
from django_absurd.models import ScheduledJob
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


def sync_crons(backend: AbsurdBackend) -> None:
    """Reconcile ScheduledJob rows for this backend's declared SCHEDULE entries.

    Opens a transaction on backend.database and acquires an advisory lock to
    serialise concurrent reconcilers. Upserts one row per declared schedule
    (source="settings"), then prunes undeclared settings rows for this alias.
    The source="admin" scope is never touched.

    The pg_cron-job phase (cron.schedule calls) is added in Task 9.
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

        ScheduledJob.objects.using(backend.database).filter(
            source="settings", alias=backend.alias
        ).exclude(name__in=declared_names).delete()
