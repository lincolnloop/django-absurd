import dataclasses
import datetime as dt
import functools
import hashlib
import logging
import threading
import typing as t

import croniter
from django.db import close_old_connections
from django.utils import timezone
from django.utils.module_loading import import_string

from django_absurd.backends import AbsurdBackend
from django_absurd.cleanup import cleanup_queues
from django_absurd.connection import validate_backend
from django_absurd.params import absurd_params

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Schedule:
    name: str
    task: str
    cron: str
    queue: str | None = None
    args: list[t.Any] = dataclasses.field(default_factory=list)
    kwargs: dict[str, t.Any] = dataclasses.field(default_factory=dict)
    backend: str = "default"


def get_next_datetime(cron: str, after: dt.datetime) -> dt.datetime:
    # second_at_beginning=True: a 6-field cron carries a LEADING seconds column, so
    # "*/30 * * * * *" means every 30 seconds. Without it croniter reads seconds as the
    # trailing field and the expression silently degrades to every-second firing.
    local_after = timezone.localtime(after)
    return croniter.croniter(cron, local_after, second_at_beginning=True).get_next(
        dt.datetime
    )


def get_settings_schedules(backend: AbsurdBackend) -> list[Schedule]:
    schedule_map: dict[str, dict[str, t.Any]] = backend.options.get("SCHEDULE", {})
    return [
        Schedule(
            name=name,
            task=spec["task"],
            cron=spec["cron"],
            queue=spec.get("queue") or None,
            args=list(spec.get("args", [])),
            kwargs=dict(spec.get("kwargs", {})),
            backend=backend.alias,
        )
        for name, spec in schedule_map.items()
    ]


def derive_idempotency_key(schedule: Schedule, due: dt.datetime) -> str:
    # Dedup key, anchored on the schedule name (not task/cron) so args/queue-varying
    # entries don't collide. https://earendil-works.github.io/absurd/patterns/cron/
    utc_due = due.astimezone(dt.UTC).isoformat(timespec="seconds")
    raw = f"{schedule.backend}|{schedule.name}|{schedule.cron}|{utc_due}"
    return "cron:" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def spawn_scheduled(schedule: Schedule, due: dt.datetime) -> None:
    close_old_connections()
    try:
        task = import_string(schedule.task)
        overrides: dict[str, str] = {"backend": schedule.backend}
        if schedule.queue is not None:
            overrides["queue_name"] = schedule.queue
        task = task.using(**overrides)
        key = derive_idempotency_key(schedule, due)
        absurd_params(idempotency_key=key).bind(task).enqueue(
            *schedule.args, **schedule.kwargs
        )
    finally:
        close_old_connections()


def get_cleanup_schedule(backend: AbsurdBackend) -> str | None:
    cleanup = backend.options.get("CLEANUP") or {}
    return cleanup.get("schedule") or None


def run_beat(
    backend: AbsurdBackend,
    *,
    now: t.Callable[[], dt.datetime] = timezone.now,
    stop: threading.Event | None = None,
    wait: t.Callable[[float], bool] | None = None,
) -> None:
    validate_backend(backend.database)
    schedules = get_settings_schedules(backend)
    cleanup_cron = get_cleanup_schedule(backend)
    entries = build_beat_entries(backend, schedules, cleanup_cron, now())
    if not entries:
        logger.info("beat: no schedules declared")
        return

    logger.info(
        'beat started: schedules=%d cleanup="%s"',
        len(schedules),
        cleanup_cron or "off",
    )
    stop = stop or threading.Event()
    wait = wait or stop.wait

    while not stop.is_set():
        earliest = min(e.next_at for e in entries)
        delay = (earliest - now()).total_seconds()
        if delay > 0 and wait(delay):
            break
        current = now()
        for entry in entries:
            if entry.next_at <= current:
                entry.fire(entry.next_at)
                entry.next_at = get_next_datetime(entry.cron, current)


@dataclasses.dataclass
class BeatEntry:
    """One thing the beat loop fires on a cron cadence — a task schedule or cleanup.

    Both kinds share one loop: ``fire`` is the callback for a due time, ``next_at`` is
    advanced after each firing.
    """

    cron: str
    fire: t.Callable[[dt.datetime], None]
    next_at: dt.datetime


def build_beat_entries(
    backend: AbsurdBackend,
    schedules: list[Schedule],
    cleanup_cron: str | None,
    moment: dt.datetime,
) -> list[BeatEntry]:
    entries = [
        BeatEntry(
            s.cron,
            functools.partial(fire_schedule, s),
            get_next_datetime(s.cron, moment),
        )
        for s in schedules
    ]
    if cleanup_cron is not None:
        entries.append(
            BeatEntry(
                cleanup_cron,
                functools.partial(fire_cleanup, backend),
                get_next_datetime(cleanup_cron, moment),
            )
        )
    return entries


def fire_cleanup(backend: AbsurdBackend, due: dt.datetime) -> None:
    close_old_connections()
    try:
        cleanup_queues()
    except Exception:
        logger.exception("cleanup failed")
    else:
        logger.info(
            'cleanup ran: due="%s"',
            due.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    finally:
        close_old_connections()


def fire_schedule(schedule: Schedule, due: dt.datetime) -> None:
    due_utc = due.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        spawn_scheduled(schedule, due)
    except Exception:
        # The loop advances past this firing and never comes back, so this is an
        # operator's only notice of it. Our own errors name their fix in the message,
        # which lands on the traceback's last line.
        logger.exception(
            'schedule enqueue failed: name="%s" due="%s"',
            schedule.name,
            due_utc,
        )
    else:
        logger.info('schedule enqueued: name="%s" due="%s"', schedule.name, due_utc)
