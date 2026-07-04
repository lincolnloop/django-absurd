import pytest

from django_absurd.backends import get_absurd_backends
from django_absurd.models import ScheduledJob
from django_absurd.pgcron import sync_crons

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.pgcron]

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


def test_upsert_and_prune_settings_rows(settings):
    settings.TASKS = tasks(
        {
            "a": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
            "b": {"task": "tests.tasks.add", "cron": "0 3 * * *"},
        }
    )
    be = get_absurd_backends()["default"]
    sync_crons(be)
    assert set(ScheduledJob.objects.values_list("name", flat=True)) == {"a", "b"}
    settings.TASKS = tasks({"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}})
    sync_crons(get_absurd_backends()["default"])
    assert set(ScheduledJob.objects.values_list("name", flat=True)) == {"a"}


def test_admin_rows_untouched(settings):
    ScheduledJob.objects.create(
        name="a",
        source="admin",
        alias="default",
        task="tests.tasks.add",
        params={"args": [], "kwargs": {}},
        options={},
        cron="0 2 * * *",
    )
    settings.TASKS = tasks({})
    sync_crons(get_absurd_backends()["default"])
    assert ScheduledJob.objects.filter(source="admin", name="a").exists()
