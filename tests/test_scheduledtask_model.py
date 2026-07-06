import pytest
from django.db import IntegrityError, transaction

from django_absurd.pg_cron.models import ScheduledTask


def test_scheduledtask_has_explicit_option_columns():
    task = ScheduledTask.objects.create(
        name="nightly",
        alias="default",
        task="demo.tasks.ping",
        cron="0 2 * * *",
        queue="default",
        args=[1, 2],
        kwargs={"k": "v"},
        max_attempts=3,
        retry_strategy={"kind": "fixed"},
        headers={"x": "y"},
        cancellation={"policy": "none"},
        idempotency_key="abc",
    )
    task.refresh_from_db()
    assert task.args == [1, 2]
    assert task.kwargs == {"k": "v"}
    assert task.max_attempts == 3
    assert task.retry_strategy == {"kind": "fixed"}
    assert task.headers == {"x": "y"}
    assert task.cancellation == {"policy": "none"}
    assert task.idempotency_key == "abc"
    assert str(task) == "settings:default:nightly"


def test_scheduledtask_option_columns_default_empty():
    task = ScheduledTask.objects.create(
        name="x", alias="default", task="demo.tasks.ping", cron="* * * * *"
    )
    task.refresh_from_db()
    assert task.args == []
    assert task.kwargs == {}
    assert task.max_attempts is None
    assert task.retry_strategy is None
    assert task.headers is None
    assert task.cancellation is None
    assert task.idempotency_key == ""


@pytest.mark.django_db(transaction=True)
def test_scheduledtask_unique_per_source_alias_name():
    ScheduledTask.objects.create(
        name="dup",
        source="settings",
        alias="default",
        task="demo.tasks.ping",
        cron="* * * * *",
    )
    with transaction.atomic(), pytest.raises(IntegrityError):
        ScheduledTask.objects.create(
            name="dup",
            source="settings",
            alias="default",
            task="demo.tasks.ping",
            cron="* * * * *",
        )
    # cross-source with the same alias/name is allowed
    ScheduledTask.objects.create(
        name="dup",
        source="admin",
        alias="default",
        task="demo.tasks.ping",
        cron="* * * * *",
    )
