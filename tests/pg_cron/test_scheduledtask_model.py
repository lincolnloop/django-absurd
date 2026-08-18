import logging

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from pytest_django import Settings

from django_absurd.pg_cron.choices import Source
from django_absurd.pg_cron.models import ScheduledTask
from tests.pg_cron import utils


@pytest.mark.django_db(transaction=True)
def test_long_schedule_name_passes_full_clean(settings: Settings) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)
    task = ScheduledTask(
        name="n" * 300,
        source=Source.ADMIN,
        task="tests.pg_cron.tasks.add",
        queue="default",
        cron="5 seconds",
    )
    task.full_clean()  # no ValidationError — length is unbounded


def test_scheduledtask_has_explicit_option_columns() -> None:
    task = ScheduledTask.objects.create(
        name="nightly",
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
    assert str(task) == "s:nightly"


def test_scheduledtask_option_columns_default_empty() -> None:
    task = ScheduledTask.objects.create(
        name="x", task="demo.tasks.ping", cron="* * * * *"
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
    settings: Settings,
) -> None:
    # the field default is the backend's DEFAULT_MAX_ATTEMPTS, not a hardcoded 5
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {
                "QUEUES": {"default": {}},
                "DEFAULT_MAX_ATTEMPTS": 3,
            },
        }
    }
    task = ScheduledTask.objects.create(
        source="a",
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
                    task="demo.tasks.ping",
                    cron="* * * * *",
                    max_attempts=0,
                )
            ]
        )


@pytest.mark.django_db(transaction=True)
def test_scheduledtask_unique_per_source_name() -> None:
    ScheduledTask.objects.create(
        name="dup",
        source="s",
        task="demo.tasks.ping",
        cron="* * * * *",
    )
    with transaction.atomic(), pytest.raises(IntegrityError):
        ScheduledTask.objects.create(
            name="dup",
            source="s",
            task="demo.tasks.ping",
            cron="* * * * *",
        )
    # but a different source with the same name is allowed — settings and admin
    # schedules are distinct, source-namespaced jobs
    ScheduledTask.objects.create(
        name="dup",
        source="a",
        task="demo.tasks.ping",
        cron="* * * * *",
    )


@pytest.mark.django_db(transaction=True)
def test_cron_is_validated_even_with_no_backend_configured(
    settings: Settings,
) -> None:
    # The queue rule needs a backend to know what is declared; the grammar rule needs
    # nothing, so a misconfigured TASKS must not buy a row a free pass on its cron.
    settings.TASKS = {
        "default": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}
    }
    row = ScheduledTask(
        source=Source.ADMIN,
        name="s",
        task="tests.pg_cron.tasks.add",
        cron="*/30 * * * * *",
        queue="default",
    )
    with pytest.raises(ValidationError) as exc:
        row.full_clean()
    assert exc.value.message_dict["cron"] == [
        "Expected a 5-field cron expression; got 6 fields."
    ]


@pytest.mark.django_db(transaction=True)
def test_saving_with_no_backend_says_why_no_job_was_scheduled(
    caplog: pytest.LogCaptureFixture,
    settings: Settings,
) -> None:
    # The emission path is best-effort by design, so it returns early rather than
    # raising — but a row that saves and never gets a job must not be silent.
    settings.TASKS = {
        "default": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}
    }
    row = ScheduledTask(
        source=Source.ADMIN,
        name="orphan",
        task="tests.pg_cron.tasks.add",
        queue="default",
        cron="0 2 * * *",
    )
    with caplog.at_level(logging.WARNING, logger="django_absurd.pg_cron.models"):
        row.save()
    assert [record.getMessage() for record in caplog.records] == [
        "pg_cron job not scheduled for 'a:orphan': no AbsurdBackend is configured"
    ]


@pytest.mark.django_db(transaction=True)
def test_deleting_with_no_backend_says_the_job_keeps_firing(
    caplog: pytest.LogCaptureFixture,
    settings: Settings,
) -> None:
    # The worse half of the silence: the row goes, its job does not, and it fires on
    # against a row that no longer exists.
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)
    row = ScheduledTask.objects.create(
        source=Source.ADMIN,
        name="doomed",
        task="tests.pg_cron.tasks.add",
        queue="default",
        cron="0 2 * * *",
    )
    settings.TASKS = {
        "default": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}
    }
    with caplog.at_level(logging.WARNING, logger="django_absurd.pg_cron.models"):
        row.delete()
    expected = (
        "pg_cron job not unscheduled for 'a:doomed': no AbsurdBackend is configured;"
        " it keeps firing until the next reconcile"
    )
    assert [record.getMessage() for record in caplog.records] == [expected]
