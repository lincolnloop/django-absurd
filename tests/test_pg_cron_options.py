import pytest

from django_absurd.backends import get_absurd_backends
from django_absurd.pg_cron.reconcile import effective_queue, resolve_spawn_options
from django_absurd.scheduler import Schedule

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


BASE_QUEUES: dict[str, dict] = {"default": {}, "other": {}, "reports": {}}


def test_max_attempts_from_decorator(settings):
    settings.TASKS = {
        "default": {"BACKEND": ABSURD, "OPTIONS": {"QUEUES": BASE_QUEUES}}
    }
    be = get_absurd_backends()["default"]
    s = Schedule(name="x", task="tests.tasks.capped", cron="0 2 * * *")
    assert (
        resolve_spawn_options(be, s)["max_attempts"] == 3
    )  # capped => @absurd_default_params(max_attempts=3)


def test_max_attempts_falls_back_to_backend_default(settings):
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {"QUEUES": BASE_QUEUES, "DEFAULT_MAX_ATTEMPTS": 7},
        }
    }
    be = get_absurd_backends()["default"]
    s = Schedule(name="x", task="tests.tasks.add", cron="0 2 * * *")  # no decorator
    assert resolve_spawn_options(be, s)["max_attempts"] == 7  # NOT 5


def test_effective_queue_uses_task_queue_name_when_unset(settings):
    settings.TASKS = {
        "default": {"BACKEND": ABSURD, "OPTIONS": {"QUEUES": BASE_QUEUES}}
    }
    s = Schedule(
        name="x", task="tests.tasks.on_reports", cron="0 2 * * *"
    )  # @task(queue_name="reports")
    assert effective_queue(s) == "reports"
