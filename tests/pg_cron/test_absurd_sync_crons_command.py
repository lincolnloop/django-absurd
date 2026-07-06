import typing as t

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from django_absurd.backends import get_absurd_backends
from django_absurd.pg_cron.models import ScheduledTask
from django_absurd.pg_cron.reconcile import sync_crons

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("ensure_pg_cron", "_clear_owned_pg_cron_jobs"),
]

ABSURD = "django_absurd.backends.AbsurdBackend"


def pg_cron_tasks(schedule: dict[str, t.Any]) -> dict[str, t.Any]:
    return {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "QUEUES": {"default": {}, "other": {}, "reports": {}},
                "SCHEDULER": "pg_cron",
                "SCHEDULE": schedule,
            },
        }
    }


def beat_tasks(schedule: dict[str, t.Any]) -> dict[str, t.Any]:
    return {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "QUEUES": {"default": {}, "other": {}, "reports": {}},
                "SCHEDULER": "beat",
                "SCHEDULE": schedule,
            },
        }
    }


def test_sync_crons_command_creates_cron_jobs(settings, capsys, owned_cron_jobs):
    settings.TASKS = pg_cron_tasks(
        {
            "a": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
            "b": {"task": "tests.tasks.add", "cron": "0 3 * * *"},
        }
    )
    call_command("absurd_sync_crons")

    jobs = owned_cron_jobs()
    assert "absurd:settings:default:a" in jobs
    assert "absurd:settings:default:b" in jobs
    assert len(jobs) == 2

    out = capsys.readouterr().out
    assert out.strip() == "Synced 2 cron(s); pruned 0 — backend 'default'."


def test_sync_crons_command_writes_summary_to_stdout(settings, capsys):
    settings.TASKS = pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    call_command("absurd_sync_crons")

    out = capsys.readouterr().out
    assert out.strip() == "Synced 1 cron(s); pruned 0 — backend 'default'."


def test_sync_crons_command_refuses_when_scheduler_is_beat(settings):
    settings.TASKS = beat_tasks({"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}})
    with pytest.raises(CommandError, match="pg_cron"):
        call_command("absurd_sync_crons")


def test_sync_crons_command_is_idempotent(settings, capsys, owned_cron_jobs):
    settings.TASKS = pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    call_command("absurd_sync_crons")
    call_command("absurd_sync_crons")

    assert len(owned_cron_jobs()) == 1


def test_teardown_removes_owned_cron_jobs(settings, capsys, owned_cron_jobs):
    settings.TASKS = pg_cron_tasks(
        {
            "a": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
            "b": {"task": "tests.tasks.add", "cron": "0 3 * * *"},
        }
    )
    be = get_absurd_backends()["default"]
    sync_crons(be)
    assert len(owned_cron_jobs()) == 2

    call_command("absurd_sync_crons", teardown=True)

    assert owned_cron_jobs() == []
    assert not ScheduledTask.objects.filter(source="settings", alias="default").exists()

    out = capsys.readouterr().out
    assert out.strip() == "Removed 2 cron(s) — backend 'default'."


def test_teardown_allowed_when_scheduler_is_beat(settings, capsys):
    settings.TASKS = beat_tasks({"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}})
    call_command("absurd_sync_crons", teardown=True)

    out = capsys.readouterr().out
    assert out.strip() == "Removed 0 cron(s) — backend 'default'."
