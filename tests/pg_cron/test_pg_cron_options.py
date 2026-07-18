import typing as t

import pytest
import pytest_django.fixtures

from django_absurd.backends import get_absurd_backends
from django_absurd.pg_cron.reconcile import resolve_spawn_options
from django_absurd.scheduler import Schedule

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


BASE_QUEUES: dict[str, dict[str, t.Any]] = {
    "default": {},
    "other": {},
    "reports": {},
}


def test_max_attempts_from_decorator(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = {
        "default": {"BACKEND": ABSURD, "OPTIONS": {"QUEUES": BASE_QUEUES}}
    }
    be = get_absurd_backends()["default"]
    s = Schedule(name="x", task="tests.tasks.capped", cron="0 2 * * *")
    assert (
        resolve_spawn_options(be, s.task)["max_attempts"] == 3
    )  # capped => @absurd_default_params(max_attempts=3)


def test_max_attempts_falls_back_to_backend_default(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {"QUEUES": BASE_QUEUES, "DEFAULT_MAX_ATTEMPTS": 7},
        }
    }
    be = get_absurd_backends()["default"]
    s = Schedule(name="x", task="tests.tasks.add", cron="0 2 * * *")  # no decorator
    assert resolve_spawn_options(be, s.task)["max_attempts"] == 7  # NOT 5
