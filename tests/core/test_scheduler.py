import datetime as dt
import os
import signal
import threading
import time
import zoneinfo

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from freezegun import freeze_time

from django_absurd.backends import get_absurd_backends
from django_absurd.scheduler import (
    Schedule,
    derive_idempotency_key,
    get_next_datetime,
    get_settings_schedules,
    run_beat,
    spawn_scheduled,
)
from tests.models import Payload
from tests.tasks import make_group as make_group_task

pytestmark = pytest.mark.django_db(transaction=True)


def make_tasks_setting(schedule):
    return {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {
                "QUEUES": {"default": {}, "other": {}, "reports": {}},
                "SCHEDULE": schedule,
            },
        }
    }


def test_settings_provider_reads_entries(settings):
    settings.TASKS = make_tasks_setting(
        {
            "nightly": {
                "task": "tests.tasks.add",
                "cron": "0 2 * * *",
                "args": [1, 2],
                "queue": "reports",
            }
        }
    )
    backend = get_absurd_backends()["default"]
    schedules = get_settings_schedules(backend)
    assert schedules == [
        Schedule(
            name="nightly",
            task="tests.tasks.add",
            cron="0 2 * * *",
            queue="reports",
            args=[1, 2],
            kwargs={},
        )
    ]


def test_settings_provider_defaults_and_empty(settings):
    settings.TASKS = make_tasks_setting(
        {"ping": {"task": "tests.tasks.add", "cron": "*/5 * * * *"}}
    )
    backend = get_absurd_backends()["default"]
    (s,) = get_settings_schedules(backend)
    assert s.queue is None
    assert s.args == []
    assert s.kwargs == {}


def test_settings_provider_no_schedule_key(settings):
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "QUEUES": ["default"],
        }
    }
    backend = get_absurd_backends()["default"]
    assert get_settings_schedules(backend) == []


@freeze_time("2026-01-01 01:59:00")
def test_get_next_datetime_same_day():
    expected = dt.datetime(2026, 1, 1, 2, 0, tzinfo=dt.UTC)
    assert get_next_datetime("0 2 * * *", timezone.now()) == expected


@freeze_time("2026-01-01 02:00:00")
def test_get_next_datetime_rolls_forward():
    expected = dt.datetime(2026, 1, 2, 2, 0, tzinfo=dt.UTC)
    assert get_next_datetime("0 2 * * *", timezone.now()) == expected


@freeze_time("2026-01-01 12:00:00")  # 06:00 in Chicago (UTC-6)
def test_get_next_datetime_uses_django_timezone(settings):
    # cron interpreted in Django TIME_ZONE: 02:00 Chicago already passed -> tomorrow
    settings.TIME_ZONE = "America/Chicago"
    chicago = zoneinfo.ZoneInfo("America/Chicago")
    expected = dt.datetime(2026, 1, 2, 2, 0, tzinfo=chicago)
    result = get_next_datetime("0 2 * * *", timezone.now())
    assert result == expected


@freeze_time("2026-01-01 12:00:00")
def test_get_next_datetime_six_field_leading_seconds():
    # A 6-field cron uses a LEADING seconds column, so "*/30 * * * * *" means every
    # 30 seconds -> next fire at :30, not every second (which is what a trailing-seconds
    # reading of the same string produces).
    expected = dt.datetime(2026, 1, 1, 12, 0, 30, tzinfo=dt.UTC)
    assert get_next_datetime("*/30 * * * * *", timezone.now()) == expected


@freeze_time("2026-01-01 12:00:00")
def test_get_next_datetime_six_field_non_divisor_seconds():
    # Leading seconds holds for any step: "*/7 * * * * *" fires at :07, not :01.
    expected = dt.datetime(2026, 1, 1, 12, 0, 7, tzinfo=dt.UTC)
    assert get_next_datetime("*/7 * * * * *", timezone.now()) == expected


@freeze_time("2026-01-01 12:00:00")
def test_get_next_datetime_six_field_zero_seconds():
    # Leading seconds=0 with a minute step fires on the minute boundary, not every
    # second: "0 */5 * * * *" -> next at 12:05:00.
    expected = dt.datetime(2026, 1, 1, 12, 5, 0, tzinfo=dt.UTC)
    assert get_next_datetime("0 */5 * * * *", timezone.now()) == expected


# run_beat_until and tests below it use run_beat's injected wait/now seam because a real
# threading.Event.wait can't be fast-forwarded by freezegun; the command path is not
# deterministic enough for exact multi-slot counts.
def run_beat_until(backend, cutoff: dt.datetime) -> None:
    with freeze_time("2026-01-01 00:00:00") as frozen:

        def fake_wait(timeout: float) -> bool:
            frozen.tick(dt.timedelta(seconds=timeout))
            return timezone.now() >= cutoff

        run_beat(backend, wait=fake_wait)


def test_beat_fires_each_due_slot(settings):
    settings.TASKS = make_tasks_setting(
        {
            "p": {
                "task": "tests.tasks.create_payload",
                "cron": "*/1 * * * *",
                "args": ["tick"],
            }
        },
    )
    backend = get_absurd_backends()["default"]
    call_command("absurd_sync_queues")
    run_beat_until(backend, dt.datetime(2026, 1, 1, 0, 2, 30, tzinfo=dt.UTC))
    call_command("absurd_worker", queue="default", burst=True)
    expected_fires = 2  # slots 00:01 and 00:02
    assert Payload.objects.count() == expected_fires


def test_beat_no_schedules_returns(settings):
    settings.TASKS = make_tasks_setting({})
    backend = get_absurd_backends()["default"]
    run_beat(backend, stop=threading.Event())  # returns immediately, no hang


def test_beat_isolates_failing_schedule(settings):
    settings.TASKS = make_tasks_setting(
        {
            "bad": {"task": "tests.tasks.does_not_exist", "cron": "*/1 * * * *"},
            "good": {
                "task": "tests.tasks.create_payload",
                "cron": "*/1 * * * *",
                "args": ["ok"],
            },
        },
    )
    backend = get_absurd_backends()["default"]
    call_command("absurd_sync_queues")
    run_beat_until(backend, dt.datetime(2026, 1, 1, 0, 1, 30, tzinfo=dt.UTC))
    call_command("absurd_worker", queue="default", burst=True)
    expected_good = 1  # "bad" raised in spawn (unimportable, logged); "good" still ran
    assert Payload.objects.count() == expected_good


def test_beat_spawns_task_with_args(settings):
    settings.TASKS = make_tasks_setting(
        {
            "g": {
                "task": "tests.tasks.make_group",
                "cron": "*/1 * * * *",
                "args": ["beat-args"],
            }
        }
    )
    backend = get_absurd_backends()["default"]
    call_command("absurd_sync_queues")
    run_beat_until(backend, dt.datetime(2026, 1, 1, 0, 1, 30, tzinfo=dt.UTC))
    call_command("absurd_worker", queue="default", burst=True)
    assert Group.objects.filter(name="beat-args").exists()


def test_beat_spawns_task_with_kwargs(settings):
    settings.TASKS = make_tasks_setting(
        {
            "g": {
                "task": "tests.tasks.make_group",
                "cron": "*/1 * * * *",
                "kwargs": {"name": "beat-kw"},
            }
        }
    )
    backend = get_absurd_backends()["default"]
    call_command("absurd_sync_queues")
    run_beat_until(backend, dt.datetime(2026, 1, 1, 0, 1, 30, tzinfo=dt.UTC))
    call_command("absurd_worker", queue="default", burst=True)
    assert Group.objects.filter(name="beat-kw").exists()


def test_beat_empty_queue_string_falls_back_to_task_queue(settings):
    """queue: "" normalises to the task's own queue (parity with pg_cron's
    fallback), not a literal "" queue that enqueue would reject."""
    settings.TASKS = make_tasks_setting(
        {
            "g": {
                "task": "tests.tasks.make_group",
                "cron": "*/1 * * * *",
                "kwargs": {"name": "beat-empty-q"},
                "queue": "",
            }
        }
    )
    backend = get_absurd_backends()["default"]
    call_command("absurd_sync_queues")
    run_beat_until(backend, dt.datetime(2026, 1, 1, 0, 1, 30, tzinfo=dt.UTC))
    call_command("absurd_worker", queue="default", burst=True)
    assert Group.objects.filter(name="beat-empty-q").exists()


def test_beat_routes_task_to_queue(settings):
    settings.TASKS = make_tasks_setting(
        {
            "g": {
                "task": "tests.tasks.make_group",
                "cron": "*/1 * * * *",
                "queue": "other",
                "args": ["beat-routed"],
            }
        }
    )
    backend = get_absurd_backends()["default"]
    call_command("absurd_sync_queues")
    run_beat_until(backend, dt.datetime(2026, 1, 1, 0, 1, 30, tzinfo=dt.UTC))
    call_command("absurd_worker", queue="default", burst=True)
    assert not Group.objects.filter(name="beat-routed").exists()
    call_command("absurd_worker", queue="other", burst=True)
    assert Group.objects.filter(name="beat-routed").exists()


def test_derive_idempotency_key_stable_same_inputs():
    schedule = Schedule(name="nightly", task="tests.tasks.add", cron="0 2 * * *")
    slot = dt.datetime(2026, 1, 1, 2, 0, tzinfo=dt.UTC)
    assert derive_idempotency_key(schedule, slot) == derive_idempotency_key(
        schedule, slot
    )


def test_derive_idempotency_key_differs_across_slots():
    schedule = Schedule(name="nightly", task="tests.tasks.add", cron="0 2 * * *")
    slot_a = dt.datetime(2026, 1, 1, 2, 0, tzinfo=dt.UTC)
    slot_b = dt.datetime(2026, 1, 2, 2, 0, tzinfo=dt.UTC)
    assert derive_idempotency_key(schedule, slot_a) != derive_idempotency_key(
        schedule, slot_b
    )


def test_derive_idempotency_key_differs_across_names():
    slot = dt.datetime(2026, 1, 1, 2, 0, tzinfo=dt.UTC)
    s_a = Schedule(name="alpha", task="tests.tasks.add", cron="0 2 * * *")
    s_b = Schedule(name="beta", task="tests.tasks.add", cron="0 2 * * *")
    assert derive_idempotency_key(s_a, slot) != derive_idempotency_key(s_b, slot)


def test_derive_idempotency_key_distinguishes_sub_minute_slots():
    # 6-field crons (croniter accepts them) yield sub-minute slots; the key must
    # distinguish slots in the same minute, or two fires would collide and the
    # second would be wrongly deduped.
    schedule = Schedule(name="n", task="tests.tasks.add", cron="*/30 * * * * *")
    slot_a = dt.datetime(2026, 1, 1, 2, 0, 0, tzinfo=dt.UTC)
    slot_b = dt.datetime(2026, 1, 1, 2, 0, 30, tzinfo=dt.UTC)
    assert derive_idempotency_key(schedule, slot_a) != derive_idempotency_key(
        schedule, slot_b
    )


def test_derive_idempotency_key_differs_across_backends():
    slot = dt.datetime(2026, 1, 1, 2, 0, tzinfo=dt.UTC)
    s_a = Schedule(
        name="alpha", task="tests.tasks.add", cron="0 2 * * *", backend="default"
    )
    s_b = Schedule(
        name="alpha", task="tests.tasks.add", cron="0 2 * * *", backend="second"
    )
    assert derive_idempotency_key(s_a, slot) != derive_idempotency_key(s_b, slot)


def test_settings_provider_sets_backend_alias(settings):
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {
                "QUEUES": {"default": {}},
            },
        },
        "second": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {
                "DATABASE": "default",
                "QUEUES": {"beat": {}},
                "SCHEDULE": {
                    "g": {
                        "task": "tests.tasks.make_group",
                        "cron": "*/1 * * * *",
                        "queue": "beat",
                        "args": ["cross-backend"],
                    }
                },
            },
        },
    }
    backend = get_absurd_backends()["second"]
    (schedule,) = get_settings_schedules(backend)
    assert schedule.backend == "second"


def test_beat_routes_task_to_nondefault_backend(settings):
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {
                "QUEUES": {"default": {}},
            },
        },
        "second": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {
                "DATABASE": "default",
                "QUEUES": {"beat": {}},
                "SCHEDULE": {
                    "g": {
                        "task": "tests.tasks.make_group",
                        "cron": "*/1 * * * *",
                        "queue": "beat",
                        "args": ["cross-backend"],
                    }
                },
            },
        },
    }
    call_command("absurd_sync_queues")
    backend = get_absurd_backends()["second"]
    run_beat_until(backend, dt.datetime(2026, 1, 1, 0, 1, 30, tzinfo=dt.UTC))
    call_command("absurd_worker", alias="second", queue="beat", burst=True)
    assert Group.objects.filter(name="cross-backend").exists()


def test_idempotency_key_dedups_same_slot(settings):
    # The command/loop never re-fires a single slot; this tests spawn_scheduled directly
    # to prove our key + Absurd's real dedup together collapse repeated fires to one task.
    settings.TASKS = make_tasks_setting(
        {
            "p": {
                "task": "tests.tasks.create_payload",
                "cron": "*/1 * * * *",
                "args": ["x"],
            }
        }
    )
    call_command("absurd_sync_queues")
    backend = get_absurd_backends()["default"]
    schedules = get_settings_schedules(backend)
    (schedule,) = schedules
    slot = dt.datetime(2026, 1, 1, 0, 1, tzinfo=dt.UTC)
    spawn_scheduled(schedule, slot)
    spawn_scheduled(schedule, slot)
    call_command("absurd_worker", queue="default", burst=True)
    assert Payload.objects.count() == 1


def test_absurd_beat_empty_schedule_runs_handle(settings):
    # Covers absurd_beat handle body (single-backend path via base.py:16-17).
    # Empty SCHEDULE → run_beat returns immediately (no blocking), no threads needed.
    settings.TASKS = make_tasks_setting({})
    call_command("absurd_sync_queues")
    prev_sigint = signal.getsignal(signal.SIGINT)
    prev_sigterm = signal.getsignal(signal.SIGTERM)
    try:
        call_command("absurd_beat")
    finally:
        signal.signal(signal.SIGINT, prev_sigint)
        signal.signal(signal.SIGTERM, prev_sigterm)


def test_absurd_beat_valid_alias_and_signal_handler(settings):
    # Covers base.py:15 (valid alias → backend = backends[alias]) and
    # absurd_beat.py:27 (handle_signal body: stop.set()).
    settings.TASKS = make_tasks_setting(
        {
            "nightly": {
                "task": "tests.tasks.create_payload",
                "cron": "0 2 * * *",
                "args": ["x"],
            }
        }
    )
    call_command("absurd_sync_queues")

    prev_sigint = signal.getsignal(signal.SIGINT)
    prev_sigterm = signal.getsignal(signal.SIGTERM)

    def fire_sigint() -> None:
        # Wait until absurd_beat has installed its SIGINT handler, then signal.
        # Gating on the handler (vs a fixed sleep) removes the race where the
        # signal could arrive before the command replaces the default handler.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:  # pragma: no branch
            if signal.getsignal(signal.SIGINT) is not prev_sigint:
                break
            time.sleep(0.005)
        os.kill(os.getpid(), signal.SIGINT)

    killer = threading.Thread(target=fire_sigint, daemon=True)
    try:
        killer.start()
        call_command("absurd_beat", alias="default")
    finally:
        killer.join(timeout=2)
        signal.signal(signal.SIGINT, prev_sigint)
        signal.signal(signal.SIGTERM, prev_sigterm)


def test_absurd_beat_unknown_alias_errors(settings):
    settings.TASKS = make_tasks_setting(
        {"m": {"task": "tests.tasks.add", "cron": "*/5 * * * *"}}
    )
    with pytest.raises(CommandError, match="not an Absurd backend alias"):
        call_command("absurd_beat", alias="nope")


def test_absurd_beat_multiple_backends_requires_alias(settings):
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "QUEUES": ["default"],
        },
        "second": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"DATABASE": "default", "QUEUES": {"default": {}}},
        },
    }
    with pytest.raises(CommandError, match="Use --alias"):
        call_command("absurd_beat")


def test_worker_beat_rejects_burst(settings):
    settings.TASKS = make_tasks_setting(
        {"g": {"task": "tests.tasks.make_group", "cron": "*/1 * * * *", "args": ["x"]}}
    )
    with pytest.raises(CommandError, match="--beat"):
        call_command("absurd_worker", queue="default", burst=True, beat=True)


def test_beat_stop_interrupts_long_sleep(settings):
    # Real threading.Event.wait — stop.set() must wake the beat promptly.
    settings.TASKS = make_tasks_setting(
        {
            "nightly": {
                "task": "tests.tasks.create_payload",
                "cron": "0 2 * * *",  # next slot is ~1-23h away
                "args": ["should-not-fire"],
            }
        },
    )
    backend = get_absurd_backends()["default"]
    call_command("absurd_sync_queues")

    stop = threading.Event()
    beat_thread = threading.Thread(
        target=run_beat, kwargs={"backend": backend, "stop": stop}, daemon=True
    )
    beat_thread.start()
    time.sleep(0.05)  # let beat enter stop.wait
    stop.set()
    beat_thread.join(timeout=2)

    assert not beat_thread.is_alive()
    assert Payload.objects.count() == 0


def test_worker_with_beat_runs_scheduled_task(settings):
    settings.TASKS = make_tasks_setting(
        {
            "g": {
                "task": "tests.tasks.make_group",
                "cron": "*/1 * * * *",
                "args": ["beat-ran"],
            }
        }
    )
    call_command("absurd_sync_queues")

    def watch():
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:  # pragma: no branch
            if Group.objects.filter(name="beat-ran").exists():
                break
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)  # stop worker + beat (main-thread handler)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    # tick=True near a boundary: next "*/1" slot is ~1s away in real time, so beat fires
    # almost immediately; the live worker (fast poll) drains it.
    with freeze_time("2026-01-01 00:00:59", tick=True):
        call_command("absurd_worker", queue="default", beat=True, poll_interval=0.05)
    watcher.join(timeout=5)

    assert Group.objects.filter(name="beat-ran").exists()


def test_beat_already_stopped_on_entry_skips_loop(settings):
    # Covers scheduler.py 92->exit: while-False branch when stop is pre-set.
    # Beat has at least one schedule so it passes the early-return guard, reaches
    # the while loop with stop already set → exits without enqueueing anything.
    settings.TASKS = make_tasks_setting(
        {
            "p": {
                "task": "tests.tasks.create_payload",
                "cron": "*/1 * * * *",
                "args": ["should-not-fire"],
            }
        }
    )
    call_command("absurd_sync_queues")
    backend = get_absurd_backends()["default"]

    stop = threading.Event()
    stop.set()
    run_beat(backend, stop=stop)

    assert Payload.objects.count() == 0


def test_beat_skips_not_yet_due_schedule(settings):
    # Covers scheduler.py 99->98: if due <= current False branch.
    # Two schedules: one every minute (due), one at 02:00 (far future, not due).
    # After one slot cutoff only the due schedule fires.
    settings.TASKS = make_tasks_setting(
        {
            "due": {
                "task": "tests.tasks.create_payload",
                "cron": "*/1 * * * *",
                "args": ["due"],
            },
            "later": {
                "task": "tests.tasks.create_payload",
                "cron": "0 2 * * *",
                "args": ["later"],
            },
        }
    )
    backend = get_absurd_backends()["default"]
    call_command("absurd_sync_queues")
    # Cutoff at 00:01:30 → "due" slot 00:01 fires, "later" slot 02:00 does not.
    run_beat_until(backend, dt.datetime(2026, 1, 1, 0, 1, 30, tzinfo=dt.UTC))
    call_command("absurd_worker", queue="default", burst=True)
    assert Payload.objects.count() == 1
    assert Payload.objects.filter(data="due").exists()
    assert not Payload.objects.filter(data="later").exists()


def test_plain_worker_runs_blocking_worker(settings):
    # Covers worker.py line 114: else branch of arun_worker (no burst, no beat).
    settings.TASKS = make_tasks_setting(
        {
            "g": {
                "task": "tests.tasks.make_group",
                "cron": "*/1 * * * *",
                "args": ["plain-worker"],
            }
        }
    )
    call_command("absurd_sync_queues")
    make_group_task.enqueue("plain-worker")

    def watch():
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:  # pragma: no branch
            if Group.objects.filter(name="plain-worker").exists():
                break
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    call_command("absurd_worker", queue="default", poll_interval=0.05)
    watcher.join(timeout=5)

    assert Group.objects.filter(name="plain-worker").exists()
