import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from pytest_django.fixtures import Settings

from tests import utils

pytestmark = pytest.mark.django_db(transaction=True)

SCHEMA_ABSENT = "Absurd schema is not installed. Run: manage.py migrate"


@pytest.mark.parametrize("command", ["absurd_sync_queues", "absurd_worker"])
def test_a_command_names_the_missing_schema_without_a_traceback(
    command: str,
    settings: Settings,
) -> None:
    settings.TASKS = utils.make_tasks_settings(queues={"default": {}})
    with utils.hide_absurd_schema(), pytest.raises(CommandError) as excinfo:
        call_command(command)
    assert str(excinfo.value) == SCHEMA_ABSENT


def test_beat_reports_a_missing_backend_without_a_traceback(
    settings: Settings,
) -> None:
    settings.TASKS = {
        "default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}
    }
    with pytest.raises(CommandError) as excinfo:
        call_command("absurd_beat")
    assert str(excinfo.value) == (
        "No Absurd backend configured. Add a "
        "django_absurd.backends.AbsurdBackend entry to TASKS."
    )
