import typing as t

import pytest

if t.TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

from django_absurd.backends import get_absurd_backends
from django_absurd.pg_cron.models import ScheduledTask
from django_absurd.pg_cron.reconcile import sync_crons, teardown_crons
from tests.utils import make_tasks_settings

pytestmark = pytest.mark.django_db(transaction=True)


def test_teardown_removes_all_owned_cron_jobs_and_settings_rows(
    settings: "SettingsWrapper",
) -> None:
    settings.TASKS = make_tasks_settings(
        schedule={
            "a": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
            "b": {"task": "tests.tasks.add", "cron": "0 3 * * *"},
        }
    )
    be = get_absurd_backends()["default"]
    sync_crons(be)

    assert len(ScheduledTask.pg_cron.get_managed_jobs()) == 2
    assert ScheduledTask.objects.filter(source="s").count() == 2

    teardown_crons()

    assert ScheduledTask.pg_cron.get_managed_jobs() == []
    assert not ScheduledTask.objects.filter(source="s").exists()


def test_teardown_leaves_admin_rows_intact(
    settings: "SettingsWrapper",
) -> None:
    ScheduledTask.objects.create(
        name="admin-job",
        source="a",
        task="tests.tasks.add",
        cron="0 4 * * *",
    )
    settings.TASKS = make_tasks_settings(
        schedule={"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    be = get_absurd_backends()["default"]
    sync_crons(be)
    teardown_crons()

    assert not ScheduledTask.objects.filter(source="s").exists()
    assert ScheduledTask.objects.filter(source="a", name="admin-job").exists()


def test_teardown_is_idempotent(settings: "SettingsWrapper") -> None:
    settings.TASKS = make_tasks_settings(
        schedule={"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    be = get_absurd_backends()["default"]
    sync_crons(be)
    teardown_crons()
    teardown_crons()  # must not raise

    assert ScheduledTask.pg_cron.get_managed_jobs() == []
    assert not ScheduledTask.objects.filter(source="s").exists()
