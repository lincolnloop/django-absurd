import dataclasses
import datetime
import logging
import threading
import typing as t

import croniter
from django.db import close_old_connections
from django.utils import timezone
from django.utils.module_loading import import_string

from django_absurd.backends import AbsurdBackend
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


def get_next_datetime(cron: str, after: datetime.datetime) -> datetime.datetime:
    local_after = timezone.localtime(after)
    return croniter.croniter(cron, local_after).get_next(datetime.datetime)


def get_settings_schedules(backend: AbsurdBackend) -> list[Schedule]:
    schedule_map: dict[str, t.Any] = backend.options.get("SCHEDULE", {})
    return [
        Schedule(
            name=name,
            task=spec["task"],
            cron=spec["cron"],
            queue=spec.get("queue", None),
            args=list(spec.get("args", [])),
            kwargs=dict(spec.get("kwargs", {})),
        )
        for name, spec in schedule_map.items()
    ]


def derive_idempotency_key(schedule: Schedule, slot: datetime.datetime) -> str:
    utc_slot = slot.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%MZ")
    return f"{schedule.name}:{utc_slot}"


def spawn_scheduled(schedule: Schedule, slot: datetime.datetime) -> None:
    close_old_connections()
    try:
        task = import_string(schedule.task)
        if schedule.queue is not None:
            task = task.using(queue_name=schedule.queue)
        task.enqueue(
            *schedule.args,
            **schedule.kwargs,
            absurd_spawn_params=AbsurdSpawnParams(
                idempotency_key=derive_idempotency_key(schedule, slot)
            ),
        )
    finally:
        close_old_connections()


def run_beat(
    backend: AbsurdBackend,
    *,
    now: t.Callable[[], datetime.datetime] = timezone.now,
    stop: threading.Event | None = None,
    wait: t.Callable[[float], bool] | None = None,
) -> None:
    validate_backend(backend.database)
    schedules = get_settings_schedules(backend)
    if not schedules:
        logger.info("no schedules declared")
        return

    logger.info("beat started: %d schedule(s)", len(schedules))
    stop = stop or threading.Event()
    wait = wait or stop.wait

    upcoming: dict[str, datetime.datetime] = {
        s.name: get_next_datetime(s.cron, now()) for s in schedules
    }
    by_name = {s.name: s for s in schedules}

    while not stop.is_set():
        earliest = min(upcoming.values())
        delay = (earliest - now()).total_seconds()
        if delay > 0 and wait(delay):
            break
        current = now()
        for name, due in list(upcoming.items()):
            if due <= current:
                fire_schedule(by_name[name], due)
                upcoming[name] = get_next_datetime(by_name[name].cron, current)


def fire_schedule(schedule: Schedule, slot: datetime.datetime) -> None:
    try:
        spawn_scheduled(schedule, slot)
    except Exception:
        logger.exception("failed to spawn schedule %r", schedule.name)
    else:
        logger.info(
            "enqueued scheduled task %r (slot %s)",
            schedule.name,
            slot.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%MZ"),
        )
