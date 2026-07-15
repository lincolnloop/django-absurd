import pytest

from django_absurd.backends import get_absurd_backends
from django_absurd.pg_cron.models import ScheduledTask
from django_absurd.pg_cron.reconcile import sync_crons, teardown_crons

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


def build_tasks(schedule):
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


def test_teardown_removes_all_owned_cron_jobs_and_settings_rows(settings):
    settings.TASKS = build_tasks(
        {
            "a": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
            "b": {"task": "tests.tasks.add", "cron": "0 3 * * *"},
        }
    )
    be = get_absurd_backends()["default"]
    sync_crons(be)

    assert len(ScheduledTask.pg_cron.get_managed_jobs()) == 2
    assert ScheduledTask.objects.filter(source="s", alias="default").count() == 2

    teardown_crons(be)

    assert ScheduledTask.pg_cron.get_managed_jobs() == []
    assert not ScheduledTask.objects.filter(source="s", alias="default").exists()


def test_teardown_leaves_admin_rows_intact(settings):
    ScheduledTask.objects.create(
        name="admin-job",
        source="a",
        alias="default",
        task="tests.tasks.add",
        cron="0 4 * * *",
    )
    settings.TASKS = build_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    be = get_absurd_backends()["default"]
    sync_crons(be)
    teardown_crons(be)

    assert not ScheduledTask.objects.filter(source="s", alias="default").exists()
    assert ScheduledTask.objects.filter(source="a", name="admin-job").exists()


def test_teardown_is_idempotent(settings):
    settings.TASKS = build_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    be = get_absurd_backends()["default"]
    sync_crons(be)
    teardown_crons(be)
    teardown_crons(be)  # must not raise

    assert ScheduledTask.pg_cron.get_managed_jobs() == []
    assert not ScheduledTask.objects.filter(source="s", alias="default").exists()
