import typing as t

import pytest
from django.db import connection

from django_absurd.apps import reconcile_crons_after_migrate
from django_absurd.models import ScheduledJob

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.pgcron,
    pytest.mark.usefixtures("ensure_pgcron"),
]

ABSURD = "django_absurd.backends.AbsurdBackend"


def pgcron_tasks(schedule: dict[str, t.Any]) -> dict[str, t.Any]:
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


def beat_tasks(schedule: dict[str, t.Any]) -> dict[str, t.Any]:
    return {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "QUEUES": {"default": {}, "other": {}, "reports": {}},
                "SCHEDULER": "beat",
                "SCHEDULE": schedule,
            },
        }
    }


def owned_cron_jobs(alias: str = "default") -> list[str]:
    with connection.cursor() as cur:
        cur.execute(
            "select jobname from cron.job where jobname like %s order by jobname",
            [f"absurd:settings:{alias}:%"],
        )
        return [row[0] for row in cur.fetchall()]


def run_scheduled(source: str, alias: str, name: str) -> None:
    with connection.cursor() as cur:
        cur.execute(
            "select public.django_absurd_run_scheduled(%s, %s, %s)",
            [source, alias, name],
        )


@pytest.fixture(autouse=True)
def _clear_owned_jobs():
    yield
    with connection.cursor() as cur:
        cur.execute("select jobid from cron.job where jobname like 'absurd:%'")
        for (jobid,) in cur.fetchall():
            cur.execute("select cron.unschedule(%s)", [jobid])


def test_reconcile_creates_owned_cron_jobs_under_pg_cron(settings):
    settings.TASKS = pgcron_tasks(
        {
            "a": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
            "b": {"task": "tests.tasks.add", "cron": "0 3 * * *"},
        }
    )
    reconcile_crons_after_migrate(sender=None)

    assert owned_cron_jobs() == [
        "absurd:settings:default:a",
        "absurd:settings:default:b",
    ]
    assert ScheduledJob.objects.filter(source="settings", alias="default").count() == 2


def test_reconcile_tears_down_when_scheduler_switches_to_beat(settings):
    settings.TASKS = pgcron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    reconcile_crons_after_migrate(sender=None)
    assert owned_cron_jobs() == ["absurd:settings:default:a"]

    settings.TASKS = beat_tasks({"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}})
    reconcile_crons_after_migrate(sender=None)

    assert owned_cron_jobs() == []
    assert not ScheduledJob.objects.filter(source="settings", alias="default").exists()


def test_reconcile_missing_row_fires_clean_noop(settings):
    settings.TASKS = pgcron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    reconcile_crons_after_migrate(sender=None)

    # Drop the backing row out-of-band; the committed cron.job wrapper must fire
    # as a clean no-op (the reconcile does not leave a firing job that errors).
    ScheduledJob.objects.filter(source="settings", alias="default", name="a").delete()

    run_scheduled("settings", "default", "a")  # no exception

    with connection.cursor() as cur:
        cur.execute(
            "select status from cron.job_run_details d join cron.job j "
            "using (jobid) where j.jobname = %s and d.status = 'failed'",
            ["absurd:settings:default:a"],
        )
        assert cur.fetchall() == []


def test_reconcile_skips_when_extension_absent(settings):
    settings.TASKS = pgcron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    with connection.cursor() as cur:
        cur.execute("drop extension pg_cron cascade")
    try:
        reconcile_crons_after_migrate(sender=None)  # must NOT raise
    finally:
        with connection.cursor() as cur:
            cur.execute("create extension if not exists pg_cron")


def test_reconcile_skips_on_bad_dotted_path(settings):
    settings.TASKS = pgcron_tasks(
        {"a": {"task": "tests.tasks.does_not_exist", "cron": "0 2 * * *"}}
    )

    reconcile_crons_after_migrate(sender=None)  # must NOT raise

    assert owned_cron_jobs() == []
