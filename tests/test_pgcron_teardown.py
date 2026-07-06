import pytest

from django_absurd.backends import get_absurd_backends
from django_absurd.models import ScheduledJob
from django_absurd.pgcron import sync_crons, teardown_crons

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.pgcron,
    pytest.mark.usefixtures("ensure_pgcron", "_clear_owned_cron_jobs"),
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
    assert ScheduledJob.objects.filter(source="settings", alias="default").count() == 2

    teardown_crons(be)

    assert owned_cron_jobs() == []
    assert not ScheduledJob.objects.filter(source="settings", alias="default").exists()


def test_teardown_leaves_admin_rows_intact(settings):
    ScheduledJob.objects.create(
        name="admin-job",
        source="admin",
        alias="default",
        task="tests.tasks.add",
        params={"args": [], "kwargs": {}},
        options={},
        cron="0 4 * * *",
    )
    settings.TASKS = tasks({"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}})
    be = get_absurd_backends()["default"]
    sync_crons(be)
    teardown_crons(be)

    assert not ScheduledJob.objects.filter(source="settings", alias="default").exists()
    assert ScheduledJob.objects.filter(source="admin", name="admin-job").exists()


def test_teardown_is_idempotent(settings, owned_cron_jobs):
    settings.TASKS = tasks({"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}})
    be = get_absurd_backends()["default"]
    sync_crons(be)
    teardown_crons(be)
    teardown_crons(be)  # must not raise

    assert owned_cron_jobs() == []
    assert not ScheduledJob.objects.filter(source="settings", alias="default").exists()
