import pytest
import pytest_django.fixtures

from django_absurd.backends import get_absurd_backends
from django_absurd.pg_cron.reconcile import resolve_spawn_options
from django_absurd.scheduler import Schedule
from tests.utils import make_tasks_settings

pytestmark = pytest.mark.django_db(transaction=True)


def test_max_attempts_from_decorator(
    settings: pytest_django.fixtures.Settings,
) -> None:
    settings.TASKS = make_tasks_settings()
    be = get_absurd_backends()["default"]
    s = Schedule(name="x", task="tests.tasks.capped", cron="0 2 * * *")
    # capped => @absurd_params(max_attempts=3)
    assert resolve_spawn_options(be, s.task)["max_attempts"] == 3


def test_max_attempts_falls_back_to_backend_default(
    settings: pytest_django.fixtures.Settings,
) -> None:
    settings.TASKS = make_tasks_settings(default_max_attempts=7)
    be = get_absurd_backends()["default"]
    s = Schedule(name="x", task="tests.tasks.add", cron="0 2 * * *")  # no decorator
    assert resolve_spawn_options(be, s.task)["max_attempts"] == 7  # NOT 5
