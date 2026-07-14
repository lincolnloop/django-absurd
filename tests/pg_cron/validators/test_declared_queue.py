import pytest
from django.core.exceptions import ValidationError

from django_absurd.pg_cron.models import ScheduledTask
from tests.pg_cron.validators.utils import (
    validate_from_model,
    validate_from_system_check,
)

ABSURD = "django_absurd.backends.AbsurdBackend"


def test_undeclared_queue_override_rejected_by_check(settings, capsys):
    # The core check validates explicit queue overrides via validate_schedule, which calls
    # validate_declared_queue with the override value and emits the custom message.
    result = validate_from_system_check(settings, capsys, queue="ghost")
    assert result
    assert "queue 'ghost' is not declared." in result


def test_undeclared_queue_override_rejected_by_model(settings):
    # Model full_clean: the queue field's callable choices enforce membership at the
    # field level (Django emits its own message) before clean() runs.
    result = validate_from_model(settings, queue="ghost")
    assert result
    assert "Value 'ghost' is not a valid choice." in result


def test_explicit_queue_declared_only_by_another_backend_rejected(settings):
    # The queue field's choices union queues across ALL pg_cron backends, so a queue
    # declared only by a different backend passes field-choice validation; clean() must
    # still reject it for the row's own backend (the queue doesn't exist there → every
    # fire would fail).
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {"QUEUES": {"default": {}}, "SCHEDULER": "pg_cron"},
        },
        "second": {
            "BACKEND": ABSURD,
            "OPTIONS": {"QUEUES": {"reports": {}}, "SCHEDULER": "pg_cron"},
        },
    }
    row = ScheduledTask(
        source="a",
        alias="default",
        name="ok",
        task="tests.tasks.add",
        queue="reports",
        cron="0 2 * * *",
    )
    with pytest.raises(ValidationError) as exc:
        row.full_clean()
    assert "queue 'reports' is not declared." in str(exc.value)


# The form (admin POST) subject is omitted for the explicit-queue override case:
# a dropdown can only submit values from the rendered choices list, so an undeclared
# value like "ghost" cannot reach the server via the normal admin form POST.


def test_bad_task_no_queue_reports_task_not_queue(validate):
    # no override + unimportable/not-a-task path: validate_declared_queue must SWALLOW
    # the task error (reported by validate_task_path) and not mislabel it as a queue
    # error. Exercises the try/except-return branch on both subjects.
    result = validate(task="os.getpid")
    assert result
    assert "is not a Django task." in result
    assert "is not declared" not in result
