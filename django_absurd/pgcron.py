"""pg_cron scheduler helpers — option resolution and effective-queue computation."""

import typing as t

# absurd_sdk._normalize_spawn_options is a module-level helper (pinned: absurd-sdk>=0.1)
# that normalises spawn options into the jsonb dict passed to absurd.spawn_task.
# We import it directly instead of routing through client.spawn so we get the
# exact same serialisation without creating a client or touching the DB.
from absurd_sdk import _normalize_spawn_options
from django.utils.module_loading import import_string

from django_absurd.backends import AbsurdBackend, build_merged_spawn_options
from django_absurd.scheduler import Schedule


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
