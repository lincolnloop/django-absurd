import re
import typing as t

import pytest
import pytest_django.fixtures
from django.core.management import call_command
from django.core.management.base import CommandError

from django_absurd.backends import get_absurd_backends
from django_absurd.management.base import BEAT_DISABLED_UNDER_PG_CRON

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


def build_pg_cron_tasks(
    schedule: dict[str, dict[str, object]] | None = None,
) -> dict[str, dict[str, t.Any]]:
    return {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "QUEUES": {"default": {}},
                "SCHEDULE": schedule or {},
            },
        }
    }


def test_scheduler_is_pg_cron_when_app_installed(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = build_pg_cron_tasks()
    assert get_absurd_backends()["default"].scheduler == "pg_cron"


def test_beat_command_refuses_under_pg_cron(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = build_pg_cron_tasks()
    with pytest.raises(CommandError, match=re.escape(BEAT_DISABLED_UNDER_PG_CRON)):
        call_command("absurd_beat")


def test_worker_beat_flag_refuses_under_pg_cron(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = build_pg_cron_tasks()
    with pytest.raises(CommandError, match=re.escape(BEAT_DISABLED_UNDER_PG_CRON)):
        call_command("absurd_worker", queue="default", beat=True)
