import logging
import typing as t

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import connection

from django_absurd.pg_cron.apps import reconcile_crons_after_migrate
from django_absurd.pg_cron.models import ScheduledTask
from django_absurd.queues import get_absurd_client

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("ensure_pg_cron", "_clear_owned_pg_cron_jobs"),
]

ABSURD = "django_absurd.backends.AbsurdBackend"


def pg_cron_tasks(schedule: dict[str, t.Any]) -> dict[str, t.Any]:
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


def run_scheduled(source: str, alias: str, name: str) -> None:
    with connection.cursor() as cur:
        cur.execute(
            "select public.django_absurd_run_scheduled(%s, %s, %s)",
            [source, alias, name],
        )


def test_reconcile_creates_owned_cron_jobs_under_pg_cron(settings, owned_cron_jobs):
    settings.TASKS = pg_cron_tasks(
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
    assert ScheduledTask.objects.filter(source="settings", alias="default").count() == 2


def test_reconcile_tears_down_when_scheduler_switches_to_beat(
    settings, owned_cron_jobs
):
    settings.TASKS = pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    reconcile_crons_after_migrate(sender=None)
    assert owned_cron_jobs() == ["absurd:settings:default:a"]

    settings.TASKS = beat_tasks({"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}})
    reconcile_crons_after_migrate(sender=None)

    assert owned_cron_jobs() == []
    assert not ScheduledTask.objects.filter(source="settings", alias="default").exists()


def test_reconcile_missing_row_fires_clean_noop(settings, owned_cron_jobs):
    settings.TASKS = pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    reconcile_crons_after_migrate(sender=None)

    # Drop the backing row out-of-band; the committed cron.job wrapper must fire
    # as a clean no-op (the reconcile does not leave a firing job that errors).
    ScheduledTask.objects.filter(source="settings", alias="default", name="a").delete()

    run_scheduled("settings", "default", "a")  # no exception

    with connection.cursor() as cur:
        cur.execute(
            "select status from cron.job_run_details d join cron.job j "
            "using (jobid) where j.jobname = %s and d.status = 'failed'",
            ["absurd:settings:default:a"],
        )
        assert cur.fetchall() == []


def test_reconcile_skips_when_extension_absent(settings, caplog):
    settings.TASKS = pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    with connection.cursor() as cur:
        cur.execute("drop extension pg_cron cascade")
    try:
        with caplog.at_level(logging.DEBUG, logger="django_absurd"):
            reconcile_crons_after_migrate(sender=None)  # must NOT raise
    finally:
        with connection.cursor() as cur:
            cur.execute("create extension if not exists pg_cron")

    # Expected case — a quiet no-op, not a warning-with-traceback on every migrate.
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == []


def test_reconcile_skips_on_malformed_schedule_spec(settings, owned_cron_jobs):
    settings.TASKS = pg_cron_tasks({"broken": {}})  # no task/cron keys

    reconcile_crons_after_migrate(sender=None)  # must NOT raise

    assert owned_cron_jobs() == []


def test_reconcile_skips_on_bad_dotted_path(settings, owned_cron_jobs):
    settings.TASKS = pg_cron_tasks(
        {"a": {"task": "tests.tasks.does_not_exist", "cron": "0 2 * * *"}}
    )

    reconcile_crons_after_migrate(sender=None)  # must NOT raise

    assert owned_cron_jobs() == []


def test_pg_cron_app_registered_after_core():
    # post_migrate receivers fire in INSTALLED_APPS order; reconcile must run
    # after core queue provisioning, so the app must be listed after the core app.
    labels = [config.label for config in apps.get_app_configs()]
    assert labels.index("django_absurd") < labels.index("django_absurd_pg_cron")


def test_migrate_provisions_queues_and_reconciles_crons(settings, owned_cron_jobs):
    settings.TASKS = pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )

    call_command("migrate", verbosity=0)

    assert set(get_absurd_client().list_queues()) == {"default", "other", "reports"}
    assert owned_cron_jobs() == ["absurd:settings:default:a"]
    assert ScheduledTask.objects.filter(source="settings", alias="default").count() == 1


def test_reconcile_warns_on_none_task_path(settings, caplog):
    settings.TASKS = pg_cron_tasks({"x": {"task": None, "cron": "0 2 * * *"}})
    with caplog.at_level(logging.WARNING, logger="django_absurd"):
        reconcile_crons_after_migrate(sender=None)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "skipped cron reconcile" in warnings[0].message


def test_reconcile_warns_on_string_kwargs(settings, caplog):
    settings.TASKS = pg_cron_tasks(
        {"x": {"task": "tests.tasks.add", "cron": "0 2 * * *", "kwargs": "abc"}}
    )
    with caplog.at_level(logging.WARNING, logger="django_absurd"):
        reconcile_crons_after_migrate(sender=None)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "skipped cron reconcile" in warnings[0].message
