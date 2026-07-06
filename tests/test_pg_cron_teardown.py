import pytest

from django_absurd.backends import get_absurd_backends
from django_absurd.pg_cron.models import ScheduledTask
from django_absurd.pg_cron.reconcile import sync_crons, teardown_crons

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.pg_cron,
    pytest.mark.usefixtures("ensure_pg_cron", "_clear_owned_pg_cron_jobs"),
]

ABSURD = "django_absurd.backends.AbsurdBackend"


def tasks(schedule):
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


def test_teardown_removes_all_owned_cron_jobs_and_settings_rows(
    settings, owned_cron_jobs
):
    settings.TASKS = tasks(
        {
            "a": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
            "b": {"task": "tests.tasks.add", "cron": "0 3 * * *"},
        }
    )
    be = get_absurd_backends()["default"]
    sync_crons(be)

    assert len(owned_cron_jobs()) == 2
    assert ScheduledTask.objects.filter(source="settings", alias="default").count() == 2

    teardown_crons(be)

    assert owned_cron_jobs() == []
    assert not ScheduledTask.objects.filter(source="settings", alias="default").exists()


def test_teardown_leaves_admin_rows_intact(settings):
    ScheduledTask.objects.create(
        name="admin-job",
        source="admin",
        alias="default",
        task="tests.tasks.add",
        cron="0 4 * * *",
    )
    settings.TASKS = tasks({"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}})
    be = get_absurd_backends()["default"]
    sync_crons(be)
    teardown_crons(be)

    assert not ScheduledTask.objects.filter(source="settings", alias="default").exists()
    assert ScheduledTask.objects.filter(source="admin", name="admin-job").exists()


def test_teardown_is_idempotent(settings, owned_cron_jobs):
    settings.TASKS = tasks({"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}})
    be = get_absurd_backends()["default"]
    sync_crons(be)
    teardown_crons(be)
    teardown_crons(be)  # must not raise

    assert owned_cron_jobs() == []
    assert not ScheduledTask.objects.filter(source="settings", alias="default").exists()
