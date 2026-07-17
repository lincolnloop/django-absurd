import pytest
from django.db import IntegrityError, transaction
from pytest_django.fixtures import SettingsWrapper

from django_absurd.pg_cron.models import ScheduledTask


def test_scheduledtask_has_explicit_option_columns() -> None:
    task = ScheduledTask.objects.create(
        name="nightly",
        alias="default",
        task="demo.tasks.ping",
        cron="0 2 * * *",
        queue="default",
        args=[1, 2],
        kwargs={"k": "v"},
        max_attempts=3,
        retry_kind="fixed",
        headers={"x": "y"},
        cancellation_max_duration=30,
        cancellation_max_delay=5,
        idempotency_key="abc",
    )
    task.refresh_from_db()
    assert task.args == [1, 2]
    assert task.kwargs == {"k": "v"}
    assert task.max_attempts == 3
    assert task.retry_kind == "fixed"
    assert task.headers == {"x": "y"}
    assert task.cancellation_max_duration == 30
    assert task.cancellation_max_delay == 5
    assert task.idempotency_key == "abc"
    assert str(task) == "s:default:nightly"


def test_scheduledtask_option_columns_default_empty() -> None:
    task = ScheduledTask.objects.create(
        name="x", alias="default", task="demo.tasks.ping", cron="* * * * *"
    )
    task.refresh_from_db()
    assert task.args == []
    assert task.kwargs == {}
    assert task.max_attempts == 5  # unset → Absurd's default retry ceiling
    assert task.retry_kind == ""
    assert task.headers is None
    assert task.cancellation_max_duration is None
    assert task.cancellation_max_delay is None
    assert task.idempotency_key == ""


def test_scheduledtask_max_attempts_default_bubbles_from_backend(
    settings: SettingsWrapper,
) -> None:
    # the field default is the backend's DEFAULT_MAX_ATTEMPTS, not a hardcoded 5
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {
                "QUEUES": {"default": {}},
                "SCHEDULER": "pg_cron",
                "DEFAULT_MAX_ATTEMPTS": 3,
            },
        }
    }
    task = ScheduledTask.objects.create(
        source="a",
        alias="default",
        name="bubble",
        task="demo.tasks.ping",
        cron="* * * * *",
    )
    assert task.max_attempts == 3


def test_scheduledtask_max_attempts_none_means_infinite() -> None:
    # explicit None is allowed and kept — Absurd treats NULL max_attempts as unbounded
    # retries (a deliberate opt-in, distinct from "unset", which defaults to 5).
    task = ScheduledTask.objects.create(
        name="forever",
        alias="default",
        task="demo.tasks.ping",
        cron="* * * * *",
        max_attempts=None,
    )
    task.refresh_from_db()
    assert task.max_attempts is None


@pytest.mark.django_db(transaction=True)
def test_scheduledtask_max_attempts_below_one_violates_db_constraint() -> None:
    # The DB CheckConstraint guards writes that skip full_clean (bulk_create, raw SQL):
    # Absurd's spawn_task rejects max_attempts < 1, so 0 must never reach the table.
    with transaction.atomic(), pytest.raises(IntegrityError):
        ScheduledTask.objects.bulk_create(
            [
                ScheduledTask(
                    name="zero",
                    alias="default",
                    task="demo.tasks.ping",
                    cron="* * * * *",
                    max_attempts=0,
                )
            ]
        )


@pytest.mark.django_db(transaction=True)
def test_scheduledtask_unique_per_source_alias_name() -> None:
    ScheduledTask.objects.create(
        name="dup",
        source="s",
        alias="default",
        task="demo.tasks.ping",
        cron="* * * * *",
    )
    with transaction.atomic(), pytest.raises(IntegrityError):
        ScheduledTask.objects.create(
            name="dup",
            source="s",
            alias="default",
            task="demo.tasks.ping",
            cron="* * * * *",
        )
    # but a different source with the same alias/name is allowed — settings and admin
    # schedules are distinct, source-namespaced jobs
    ScheduledTask.objects.create(
        name="dup",
        source="a",
        alias="default",
        task="demo.tasks.ping",
        cron="* * * * *",
    )
