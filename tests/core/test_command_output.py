import io

import pytest
from django.core.management import call_command
from pytest_django.fixtures import SettingsWrapper

from django_absurd.test import AbsurdTestRuntime
from tests.utils import make_tasks_settings

pytestmark = pytest.mark.django_db(transaction=True)


def test_sync_queues_decorates_its_console_output(settings: SettingsWrapper) -> None:
    settings.TASKS = make_tasks_settings(queues={"freshemoji": {}})
    out = io.StringIO()
    call_command("absurd_sync_queues", stdout=out)

    assert out.getvalue() == "🗃️ Created: freshemoji\n"


def test_the_worker_banner_carries_the_elephant(dj_absurd: AbsurdTestRuntime) -> None:
    dj_absurd.sync_queues()
    out = io.StringIO()
    call_command("absurd_worker", queue="default", burst=True, stdout=out)

    assert out.getvalue() == (
        "🐘 Started worker on queue 'default'.\n🐘 Stopped worker on queue 'default'.\n"
    )
