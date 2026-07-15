import logging
from io import StringIO

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import connection

from django_absurd.pg_cron.apps import reconcile_crons_after_migrate
from django_absurd.pg_cron.models import ScheduledTask
from django_absurd.queues import get_absurd_client
from tests.pg_cron.utils import build_beat_tasks, build_pg_cron_tasks

pytestmark = pytest.mark.django_db(transaction=True)


def run_scheduled(source: str, alias: str, name: str) -> None:
    with connection.cursor() as cur:
        cur.execute(
            "select public.django_absurd_run_scheduled(%s, %s, %s)",
            [source, alias, name],
        )


@pytest.fixture(params=["absurd_sync_crons", "migrate"])
def run_cron_sync(request):
    """Run a reconcile through each real entrypoint — the absurd_sync_crons command and a
    full migrate (which fires post_migrate). Behavioral tests assert the same outcome for
    both, so the pg_cron jobs stay correct however the reconcile is triggered."""

    def run():
        if request.param == "migrate":
            call_command("migrate", verbosity=0)
        else:
            call_command("absurd_sync_crons")

    return run


def test_reconcile_creates_owned_cron_jobs_under_pg_cron(settings, run_cron_sync):
    settings.TASKS = build_pg_cron_tasks(
        {
            "a": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
            "b": {"task": "tests.tasks.add", "cron": "0 3 * * *"},
        }
    )
    run_cron_sync()

    assert [r[0] for r in ScheduledTask.pg_cron.get_managed_jobs()] == [
        "absurd:s:default:a",
        "absurd:s:default:b",
    ]
    assert ScheduledTask.objects.filter(source="s", alias="default").count() == 2


def test_reconcile_emits_jobs_for_admin_rows_created_without_signal(
    settings, run_cron_sync
):
    """A source="a" row created without firing post_save (a data migration's
    historical model, or bulk_create) has no pg_cron job; the reconcile re-emits it so
    pg_cron matches the rows."""
    settings.TASKS = build_pg_cron_tasks({})
    ScheduledTask.objects.bulk_create(
        [
            ScheduledTask(
                source="a",
                alias="default",
                name="seeded",
                task="tests.tasks.add",
                cron="0 3 * * *",
                enabled=True,
            )
        ]
    )
    assert ScheduledTask.pg_cron.get_job("default", "seeded", "a") is None

    run_cron_sync()

    _, schedule, _, active = ScheduledTask.pg_cron.get_job("default", "seeded", "a")
    assert schedule == "0 3 * * *"
    assert active is True


def test_reconcile_admin_rows_is_idempotent(settings, run_cron_sync):
    """Re-running the reconcile re-emits admin jobs harmlessly (cron.schedule is an
    upsert) — one row still maps to exactly one job, unchanged."""
    settings.TASKS = build_pg_cron_tasks({})
    ScheduledTask.objects.bulk_create(
        [
            ScheduledTask(
                source="a",
                alias="default",
                name="seeded",
                task="tests.tasks.add",
                cron="0 3 * * *",
                enabled=True,
            )
        ]
    )
    run_cron_sync()
    run_cron_sync()

    jobs = ScheduledTask.pg_cron.get_managed_jobs(source="a")
    assert len(jobs) == 1
    jobname, schedule, _, active = jobs[0]
    assert jobname == "absurd:a:default:seeded"
    assert schedule == "0 3 * * *"
    assert active is True


def test_reconcile_prunes_owned_settings_job_whose_row_vanished(
    settings, run_cron_sync
):
    """A settings job with no backing row — its row was removed out-of-band (a
    signal-less delete), so no post_delete unscheduled it — is orphaned; the reconcile
    prunes it so cron.job reconverges to the declared state. Set up by scheduling from an
    unsaved instance: a job with no row behind it."""
    settings.TASKS = build_pg_cron_tasks({})  # nothing declared
    ScheduledTask(
        source="s",
        alias="default",
        name="orphan",
        task="tests.tasks.add",
        cron="0 2 * * *",
    ).schedule_pg_cron_job()
    assert ScheduledTask.pg_cron.get_job("default", "orphan", "s") is not None

    run_cron_sync()

    assert ScheduledTask.pg_cron.get_job("default", "orphan", "s") is None


def test_reconcile_prunes_admin_job_whose_row_vanished(settings, run_cron_sync):
    """An admin job with no backing row is orphaned; the reconcile prunes it (symmetric
    with the settings lane). Set up by scheduling from an unsaved instance."""
    settings.TASKS = build_pg_cron_tasks({})
    ScheduledTask(
        source="a",
        alias="default",
        name="orphan",
        task="tests.tasks.add",
        cron="0 3 * * *",
    ).schedule_pg_cron_job()
    assert ScheduledTask.pg_cron.get_job("default", "orphan", "a") is not None

    run_cron_sync()

    assert ScheduledTask.pg_cron.get_job("default", "orphan", "a") is None


def test_reconcile_tears_down_when_scheduler_switches_to_beat(settings):
    settings.TASKS = build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    reconcile_crons_after_migrate(sender=None)
    assert [r[0] for r in ScheduledTask.pg_cron.get_managed_jobs()] == [
        "absurd:s:default:a"
    ]

    settings.TASKS = build_beat_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    reconcile_crons_after_migrate(sender=None)

    assert ScheduledTask.pg_cron.get_managed_jobs() == []
    assert not ScheduledTask.objects.filter(source="s", alias="default").exists()


def test_reconcile_missing_row_fires_clean_noop(settings):
    settings.TASKS = build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    reconcile_crons_after_migrate(sender=None)

    # Drop the backing row out-of-band; the committed cron.job wrapper must fire
    # as a clean no-op (the reconcile does not leave a firing job that errors).
    ScheduledTask.objects.filter(source="s", alias="default", name="a").delete()

    run_scheduled("s", "default", "a")  # no exception


def test_reconcile_survives_missing_scheduledtask_table(settings, caplog):
    settings.TASKS = build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    # Simulate a faked/adopted migration or a multi-DB deploy where post_migrate
    # fires before this app's tables exist on the Absurd DB: the reconcile must
    # never break migrate — it skips the backend and logs instead of raising.
    with connection.schema_editor() as editor:
        editor.delete_model(ScheduledTask)
    try:
        with caplog.at_level(logging.WARNING, logger="django_absurd"):
            reconcile_crons_after_migrate(sender=None)
    finally:
        with connection.schema_editor() as editor:
            editor.create_model(ScheduledTask)

    assert "skipped cron reconcile for backend 'default'" in caplog.text


def test_reconcile_skips_on_malformed_schedule_spec(settings):
    settings.TASKS = build_pg_cron_tasks({"broken": {}})  # no task/cron keys

    reconcile_crons_after_migrate(sender=None)  # must NOT raise

    assert ScheduledTask.pg_cron.get_managed_jobs() == []


def test_reconcile_skips_on_bad_dotted_path(settings):
    settings.TASKS = build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.does_not_exist", "cron": "0 2 * * *"}}
    )

    reconcile_crons_after_migrate(sender=None)  # must NOT raise

    assert ScheduledTask.pg_cron.get_managed_jobs() == []


def test_pg_cron_app_registered_after_core():
    # post_migrate receivers fire in INSTALLED_APPS order; reconcile must run
    # after core queue provisioning, so the app must be listed after the core app.
    labels = [config.label for config in apps.get_app_configs()]
    assert labels.index("django_absurd") < labels.index("django_absurd_pg_cron")


def test_migrate_provisions_queues_and_reconciles_crons(settings):
    settings.TASKS = build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )

    call_command("migrate", verbosity=0)

    assert set(get_absurd_client().list_queues()) == {"default", "other", "reports"}
    assert [r[0] for r in ScheduledTask.pg_cron.get_managed_jobs()] == [
        "absurd:s:default:a"
    ]
    assert ScheduledTask.objects.filter(source="s", alias="default").count() == 1


def test_reconcile_emits_migrate_stdout_on_sync(settings):
    settings.TASKS = build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    buf = StringIO()
    reconcile_crons_after_migrate(sender=None, verbosity=1, stdout=buf)
    out = buf.getvalue()
    assert "Reconciling pg_cron schedules (default):" in out
    assert "Scheduled 1" in out


def test_reconcile_is_quiet_on_noop_migrate(settings):
    """A second migrate with an unchanged SCHEDULE creates and prunes nothing,
    so it must emit no reconcile output (parity with queue provisioning)."""
    settings.TASKS = build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    reconcile_crons_after_migrate(sender=None)  # first run creates the row + job
    buf = StringIO()
    reconcile_crons_after_migrate(sender=None, verbosity=1, stdout=buf)
    assert buf.getvalue() == ""


def test_reconcile_emits_prune_line_on_sync(settings):
    settings.TASKS = build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    reconcile_crons_after_migrate(sender=None)
    settings.TASKS = build_pg_cron_tasks({})
    buf = StringIO()
    reconcile_crons_after_migrate(sender=None, verbosity=1, stdout=buf)
    out = buf.getvalue()
    assert "Reconciling pg_cron schedules (default):" in out
    assert "Pruned 1" in out


def test_reconcile_emits_teardown_notice_when_backend_switches(settings):
    settings.TASKS = build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    reconcile_crons_after_migrate(sender=None)
    settings.TASKS = build_beat_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    buf = StringIO()
    reconcile_crons_after_migrate(sender=None, verbosity=1, stdout=buf)
    out = buf.getvalue()
    assert "Removed 1 pg_cron schedule(s)" in out
    assert 'no longer uses SCHEDULER="pg_cron"' in out


def test_reconcile_warns_on_none_task_path(settings, caplog):
    settings.TASKS = build_pg_cron_tasks({"x": {"task": None, "cron": "0 2 * * *"}})
    with caplog.at_level(logging.WARNING, logger="django_absurd"):
        reconcile_crons_after_migrate(sender=None)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "skipped cron reconcile" in warnings[0].message


def test_reconcile_warns_on_string_kwargs(settings, caplog):
    settings.TASKS = build_pg_cron_tasks(
        {"x": {"task": "tests.tasks.add", "cron": "0 2 * * *", "kwargs": "abc"}}
    )
    with caplog.at_level(logging.WARNING, logger="django_absurd"):
        reconcile_crons_after_migrate(sender=None)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "skipped cron reconcile" in warnings[0].message
