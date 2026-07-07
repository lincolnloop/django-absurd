import re

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from django_absurd.backends import get_absurd_backends
from django_absurd.management.base import BEAT_DISABLED_UNDER_PG_CRON

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


def build_pg_cron_tasks(schedule=None):
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


def test_beat_command_refuses_under_pg_cron(settings):
    settings.TASKS = build_pg_cron_tasks()
    with pytest.raises(CommandError, match=re.escape(BEAT_DISABLED_UNDER_PG_CRON)):
        call_command("absurd_beat")


def test_worker_beat_flag_refuses_under_pg_cron(settings):
    settings.TASKS = build_pg_cron_tasks()
    with pytest.raises(CommandError, match=re.escape(BEAT_DISABLED_UNDER_PG_CRON)):
        call_command("absurd_worker", queue="default", beat=True)
