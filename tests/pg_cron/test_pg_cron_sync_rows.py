import typing as t

import pytest
import pytest_django.fixtures

from django_absurd.backends import get_absurd_backends
from django_absurd.pg_cron.models import ScheduledTask
from django_absurd.pg_cron.reconcile import sync_crons

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


def build_tasks(
    schedule: dict[str, t.Any],
) -> dict[str, dict[str, t.Any]]:
    return {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "QUEUES": {"default": {}, "other": {}, "reports": {}},
                "SCHEDULER": "pg_cron",
                "SCHEDULE": schedule,
            },
        }
    }


def test_upsert_and_prune_settings_rows(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = build_tasks(
        {
            "a": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
            "b": {"task": "tests.tasks.add", "cron": "0 3 * * *"},
        }
    )
    be = get_absurd_backends()["default"]
    sync_crons(be)
    assert set(ScheduledTask.objects.values_list("name", flat=True)) == {"a", "b"}
    settings.TASKS = build_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    sync_crons(get_absurd_backends()["default"])
    assert set(ScheduledTask.objects.values_list("name", flat=True)) == {"a"}


def test_admin_rows_untouched(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    ScheduledTask.objects.create(
        name="a",
        source="a",
        task="tests.tasks.add",
        cron="0 2 * * *",
    )
    settings.TASKS = build_tasks({})
    sync_crons(get_absurd_backends()["default"])
    assert ScheduledTask.objects.filter(source="a", name="a").exists()


def test_sync_writes_named_option_columns(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = build_tasks(
        {
            "nightly": {
                "task": "tests.tasks.capped",  # decorated max_attempts=3
                "cron": "0 2 * * *",
                "args": [1, 2],
                "kwargs": {"k": "v"},
            },
        }
    )
    backend = get_absurd_backends()["default"]
    sync_crons(backend)
    row = ScheduledTask.objects.get(source="s", name="nightly")
    assert row.args == [1, 2]
    assert row.kwargs == {"k": "v"}
    assert row.max_attempts == 3


def test_reconcile_splits_cancellation_into_columns(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = build_tasks(
        {"c": {"task": "tests.tasks.cancellable", "cron": "0 2 * * *"}}
    )
    backend = get_absurd_backends()["default"]
    sync_crons(backend)
    row = ScheduledTask.objects.get(source="s", name="c")
    assert row.cancellation_max_duration == 30
    assert row.cancellation_max_delay is None


def test_reconcile_splits_retry_strategy_into_columns(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = build_tasks(
        {"r": {"task": "tests.tasks.retrying", "cron": "0 2 * * *"}}
    )
    backend = get_absurd_backends()["default"]
    sync_crons(backend)
    row = ScheduledTask.objects.get(source="s", name="r")
    assert row.retry_kind == "exponential"
    assert row.retry_base_seconds == 2.0


def test_sync_materializes_decorator_derived_columns(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    settings.TASKS = build_tasks(
        {"full": {"task": "tests.tasks.fully_specced", "cron": "0 2 * * *"}}
    )
    sync_crons(get_absurd_backends()["default"])
    row = ScheduledTask.objects.get(source="s", name="full")
    assert row.queue == "reports"
    assert row.max_attempts == 9
    assert row.retry_kind == "fixed"
    assert row.retry_base_seconds == 5
    assert row.cancellation_max_duration == 45
    assert row.cancellation_max_delay == 3
