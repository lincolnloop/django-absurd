import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from django_absurd.backends import get_absurd_backends

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


def pgcron_tasks(schedule=None):
    return {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "QUEUES": {"default": {}},
                "SCHEDULER": "pg_cron",
                "SCHEDULE": schedule or {},
            },
        }
    }


def test_scheduler_defaults_to_beat(settings):
    settings.TASKS = {"default": {"BACKEND": ABSURD, "QUEUES": ["default"]}}
    assert get_absurd_backends()["default"].scheduler == "beat"


def test_beat_command_refuses_under_pgcron(settings):
    settings.TASKS = pgcron_tasks()
    with pytest.raises(CommandError, match="SCHEDULER is pg_cron"):
        call_command("absurd_beat")


def test_worker_beat_flag_refuses_under_pgcron(settings):
    settings.TASKS = pgcron_tasks()
    with pytest.raises(CommandError, match="SCHEDULER is pg_cron"):
        call_command("absurd_worker", queue="default", beat=True)
