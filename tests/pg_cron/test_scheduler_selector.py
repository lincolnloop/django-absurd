import re

import pytest
import pytest_django.fixtures
from absurd_sdk import CreateQueueOptions
from django.core.management import call_command
from django.core.management.base import CommandError

from django_absurd.backends import get_absurd_backends
from django_absurd.management.base import BEAT_DISABLED_UNDER_PG_CRON
from tests.utils import make_tasks_settings

pytestmark = pytest.mark.django_db(transaction=True)

# This suite never imports tests.tasks, so only "default" needs declaring.
SINGLE_QUEUE: dict[str, CreateQueueOptions] = {"default": {}}


def test_scheduler_is_pg_cron_when_app_installed(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = make_tasks_settings(queues=SINGLE_QUEUE)
    assert get_absurd_backends()["default"].scheduler == "pg_cron"


def test_beat_command_refuses_under_pg_cron(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = make_tasks_settings(queues=SINGLE_QUEUE)
    with pytest.raises(CommandError, match=re.escape(BEAT_DISABLED_UNDER_PG_CRON)):
        call_command("absurd_beat")


def test_worker_beat_flag_refuses_under_pg_cron(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = make_tasks_settings(queues=SINGLE_QUEUE)
    with pytest.raises(CommandError, match=re.escape(BEAT_DISABLED_UNDER_PG_CRON)):
        call_command("absurd_worker", queue="default", beat=True)
