import logging
from pathlib import Path

import pytest
import pytest_django.fixtures
from django.core.management import call_command
from django.db import transaction

from django_absurd.pg_cron import catalog
from django_absurd.pg_cron.choices import Source
from django_absurd.pg_cron.models import ScheduledTask
from tests.pg_cron import utils

pytestmark = pytest.mark.django_db(transaction=True)

LOADED_SCHEDULE_FIXTURE = str(
    Path(__file__).parent / "fixtures" / "loaded_schedule.json"
)


def test_save_emits_job_only_after_commit(
    settings: pytest_django.fixtures.Settings,
) -> None:
    """Emission rides the row's transaction.on_commit, never synchronously in post_save:
    inside an open transaction the central job is still absent; it appears only once the
    transaction commits."""
    settings.TASKS = utils.build_pg_cron_tasks({})
    jobname = catalog.build_jobname(utils.fetch_live_database(), Source.ADMIN, "onsave")
    with transaction.atomic():
        ScheduledTask.objects.create(
            source="a",
            name="onsave",
            task="tests.tasks.add",
            cron="0 2 * * *",
        )
        assert utils.fetch_cron_job(jobname) is None
    assert utils.fetch_cron_job(jobname) is not None


def test_central_failure_after_commit_is_swallowed_and_logged(
    caplog: pytest.LogCaptureFixture,
    settings: pytest_django.fixtures.Settings,
) -> None:
    """A central-connection failure that lands AFTER the row committed (here pg_cron
    rejecting an invalid cron) is swallowed-and-logged, never propagated as a 500 — the
    row stays saved and the next reconcile self-heals the missing job."""
    settings.TASKS = utils.build_pg_cron_tasks({})
    with caplog.at_level(logging.WARNING, logger="django_absurd"):
        scheduled_task = ScheduledTask.objects.create(
            source="a",
            name="badcron",
            task="tests.tasks.add",
            cron="not a valid cron",
        )
    assert ScheduledTask.objects.filter(pk=scheduled_task.pk).exists()
    assert (
        utils.fetch_cron_job(
            catalog.build_jobname(utils.fetch_live_database(), Source.ADMIN, "badcron")
        )
        is None
    )
    records = [r for r in caplog.records if r.name == "django_absurd.pg_cron.signals"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].getMessage() == "pg_cron schedule emission failed after commit"


def test_saving_admin_schedule_schedules_the_job(
    settings: pytest_django.fixtures.Settings,
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({})
    ScheduledTask.objects.create(
        source="a",
        name="nightly",
        task="tests.tasks.add",
        cron="0 2 * * *",
        enabled=True,
    )
    rows = utils.fetch_managed_jobs(utils.fetch_live_database(), source=Source.ADMIN)
    assert len(rows) == 1
    _, schedule, _, active = rows[0]
    assert schedule == "0 2 * * *"
    assert active is True


def test_saving_disabled_admin_schedule_is_inactive(
    settings: pytest_django.fixtures.Settings,
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({})
    ScheduledTask.objects.create(
        source="a",
        name="paused",
        task="tests.tasks.add",
        cron="0 2 * * *",
        enabled=False,
    )
    job = utils.fetch_cron_job(
        catalog.build_jobname(utils.fetch_live_database(), Source.ADMIN, "paused")
    )
    assert job is not None
    assert job[1] is False


def test_saving_settings_schedule_also_schedules_the_job(
    settings: pytest_django.fixtures.Settings,
) -> None:
    """Unified path: a settings row emits through the same signal (reconcile upserts
    rows; the signal schedules the jobs)."""
    settings.TASKS = utils.build_pg_cron_tasks({})
    ScheduledTask.objects.create(
        source="s",
        name="via_reconcile",
        task="tests.tasks.add",
        cron="0 2 * * *",
    )
    assert (
        utils.fetch_cron_job(
            catalog.build_jobname(
                utils.fetch_live_database(), Source.SETTINGS, "via_reconcile"
            )
        )
        is not None
    )


def test_deleting_admin_schedule_unschedules_the_job(
    settings: pytest_django.fixtures.Settings,
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({})
    scheduled_task = ScheduledTask.objects.create(
        source="a",
        name="gone",
        task="tests.tasks.add",
        cron="0 2 * * *",
    )
    gone = catalog.build_jobname(utils.fetch_live_database(), Source.ADMIN, "gone")
    assert utils.fetch_cron_job(gone) is not None
    scheduled_task.delete()
    assert utils.fetch_cron_job(gone) is None


def test_saving_schedule_without_absurd_backend_is_a_noop(
    settings: pytest_django.fixtures.Settings,
) -> None:
    """No Absurd backend configured at all: (un)schedule are clean no-ops — the
    only surviving no-op condition once the scheduler-specific guard collapses."""
    settings.TASKS = {
        "default": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}
    }
    scheduled_task = ScheduledTask.objects.create(
        source="s",
        name="orphan_row",
        task="tests.tasks.add",
        cron="0 2 * * *",
    )
    assert (
        utils.fetch_cron_job(
            catalog.build_jobname(
                utils.fetch_live_database(), Source.SETTINGS, "orphan_row"
            )
        )
        is None
    )
    scheduled_task.delete()  # unschedule no-op, no error


@pytest.mark.django_db(transaction=True, databases=["default", "replica"])
def test_cross_database_write_is_rejected(
    settings: pytest_django.fixtures.Settings,
) -> None:
    """A ScheduledTask forced onto a non-absurd database (here via .using on a second
    alias) is rejected before the row is inserted — schedules live only on the absurd
    DB, so no misplaced row and no phantom job is created."""
    settings.TASKS = utils.build_pg_cron_tasks({})
    with pytest.raises(NotImplementedError) as exc:
        ScheduledTask.objects.using("replica").create(
            source="a",
            name="wrongdb",
            task="tests.tasks.add",
            cron="0 2 * * *",
        )
    assert str(exc.value) == (
        "ScheduledTask was written to database 'replica', but Absurd schedules live "
        "only on 'default' (the run-wrapper reads there). Cross-database schedule "
        "writes are not supported."
    )
    # pre_save fires before the INSERT, so no row was persisted
    assert not ScheduledTask.objects.using("replica").filter(name="wrongdb").exists()


@pytest.mark.django_db(transaction=True, databases=["default", "replica"])
def test_cross_database_row_stays_deletable(
    settings: pytest_django.fixtures.Settings,
) -> None:
    """A stray row created out-of-band on a foreign DB (bulk_create bypasses
    the pre_save guard) must stay deletable — the delete receiver skips it
    instead of raising, so it isn't trapped in the database."""
    settings.TASKS = utils.build_pg_cron_tasks({})
    ScheduledTask.objects.using("replica").bulk_create(
        [
            ScheduledTask(
                source="a",
                name="stray",
                task="tests.tasks.add",
                cron="0 2 * * *",
            )
        ]
    )
    ScheduledTask.objects.using("replica").filter(name="stray").delete()  # no raise
    assert not ScheduledTask.objects.using("replica").filter(name="stray").exists()


def test_loaddata_schedules_the_job(
    settings: pytest_django.fixtures.Settings,
) -> None:
    """loaddata is a real write, so the row's job materializes — the row is the source
    of truth, so a loaded/restored schedule is a live schedule."""
    settings.TASKS = utils.build_pg_cron_tasks({})
    call_command("loaddata", LOADED_SCHEDULE_FIXTURE)
    assert ScheduledTask.objects.filter(source="a", name="loaded").exists()
    rows = utils.fetch_managed_jobs(utils.fetch_live_database(), source=Source.ADMIN)
    assert len(rows) == 1
    _, schedule, _, active = rows[0]
    assert schedule == "0 5 * * *"
    assert active is True
