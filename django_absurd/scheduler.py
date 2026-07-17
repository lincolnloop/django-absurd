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
from django_absurd.params import AbsurdSpawnParams

logger = logging.getLogger("django_absurd")


@dataclasses.dataclass(frozen=True)
class Schedule:
    name: str
    task: str
    cron: str
    queue: str | None = None
    args: list = dataclasses.field(default_factory=list)
    kwargs: dict = dataclasses.field(default_factory=dict)
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
    schedule_map: dict[str, t.Any] = backend.options.get("SCHEDULE", {})
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


def derive_idempotency_key(schedule: Schedule, slot: dt.datetime) -> str:
    # Dedup key, anchored on the schedule name (not task/cron) so args/queue-varying
    # entries don't collide. https://earendil-works.github.io/absurd/patterns/cron/
    utc_slot = slot.astimezone(dt.UTC).isoformat(timespec="seconds")
    raw = f"{schedule.backend}|{schedule.name}|{schedule.cron}|{utc_slot}"
    return "cron:" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def spawn_scheduled(schedule: Schedule, slot: dt.datetime) -> None:
    close_old_connections()
    try:
        task = import_string(schedule.task)
        overrides: dict[str, t.Any] = {"backend": schedule.backend}
        if schedule.queue is not None:
            overrides["queue_name"] = schedule.queue
        task = task.using(**overrides)
        task.enqueue(
            *schedule.args,
            **schedule.kwargs,
            absurd_spawn_params=AbsurdSpawnParams(
                idempotency_key=derive_idempotency_key(schedule, slot)
            ),
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
        logger.info("django-absurd beat: no schedules declared")
        return

    logger.info(
        "django-absurd beat started: schedules=%d cleanup=%s",
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

    Both kinds share one loop: ``fire`` is the callback for a due slot, ``next_at`` is
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


def fire_cleanup(backend: AbsurdBackend, slot: dt.datetime) -> None:
    close_old_connections()
    try:
        counts = cleanup_queues()
    except Exception:
        logger.exception("django-absurd cleanup failed")
    else:
        logger.info(
            "django-absurd cleanup ran: slot=%s counts=%s",
            slot.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            counts,
        )
    finally:
        close_old_connections()


def fire_schedule(schedule: Schedule, slot: dt.datetime) -> None:
    try:
        spawn_scheduled(schedule, slot)
    except Exception:
        logger.exception("django-absurd schedule failed: name=%s", schedule.name)
    else:
        logger.info(
            "django-absurd schedule enqueued: name=%s slot=%s",
            schedule.name,
            slot.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
