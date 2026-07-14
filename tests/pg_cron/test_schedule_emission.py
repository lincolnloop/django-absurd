from pathlib import Path

import pytest
from django.core.management import call_command

from django_absurd.pg_cron.models import ScheduledTask
from tests.pg_cron.utils import build_beat_tasks, build_pg_cron_tasks

pytestmark = pytest.mark.django_db(transaction=True)

LOADED_SCHEDULE_FIXTURE = str(
    Path(__file__).parent / "fixtures" / "loaded_schedule.json"
)


def test_saving_admin_schedule_schedules_the_job(settings):
    settings.TASKS = build_pg_cron_tasks({})
    scheduled_task = ScheduledTask.objects.create(
        source="a",
        alias="default",
        name="nightly",
        task="tests.tasks.add",
        cron="0 2 * * *",
        enabled=True,
    )
    _, schedule, _, active = scheduled_task.get_pg_cron_job()
    assert schedule == "0 2 * * *"
    assert active is True


def test_saving_disabled_admin_schedule_is_inactive(settings):
    settings.TASKS = build_pg_cron_tasks({})
    ScheduledTask.objects.create(
        source="a",
        alias="default",
        name="paused",
        task="tests.tasks.add",
        cron="0 2 * * *",
        enabled=False,
    )
    assert ScheduledTask.pg_cron.get_job("default", "paused", "a")[3] is False


def test_saving_settings_schedule_also_schedules_the_job(settings):
    """Unified path: a settings row emits through the same signal (reconcile upserts
    rows; the signal schedules the jobs)."""
    settings.TASKS = build_pg_cron_tasks({})
    ScheduledTask.objects.create(
        source="s",
        alias="default",
        name="via_reconcile",
        task="tests.tasks.add",
        cron="0 2 * * *",
    )
    assert ScheduledTask.pg_cron.get_job("default", "via_reconcile", "s") is not None


def test_deleting_admin_schedule_unschedules_the_job(settings):
    settings.TASKS = build_pg_cron_tasks({})
    scheduled_task = ScheduledTask.objects.create(
        source="a",
        alias="default",
        name="gone",
        task="tests.tasks.add",
        cron="0 2 * * *",
    )
    assert ScheduledTask.pg_cron.get_job("default", "gone", "a") is not None
    scheduled_task.delete()
    assert ScheduledTask.pg_cron.get_job("default", "gone", "a") is None


def test_saving_non_pg_cron_backend_schedule_is_a_noop(settings):
    """A row whose backend isn't pg_cron has no job to (un)schedule — save and delete
    are clean no-ops (the signal skips it)."""
    settings.TASKS = build_beat_tasks({})
    scheduled_task = ScheduledTask.objects.create(
        source="s",
        alias="default",
        name="beat_row",
        task="tests.tasks.add",
        cron="0 2 * * *",
    )
    assert ScheduledTask.pg_cron.get_job("default", "beat_row", "s") is None
    scheduled_task.delete()  # unschedule no-op, no error


def test_saving_unconfigured_alias_is_a_noop(settings):
    """An alias mapping to no configured backend has nothing to (un)schedule — neither
    save nor delete errors."""
    settings.TASKS = build_pg_cron_tasks({})
    scheduled_task = ScheduledTask.objects.create(
        source="a",
        alias="ghost",
        name="x",
        task="tests.tasks.add",
        cron="0 2 * * *",
    )
    assert ScheduledTask.pg_cron.get_job("ghost", "x", "a") is None
    assert ScheduledTask.pg_cron.get_managed_jobs(source="a") == []
    scheduled_task.delete()  # no error


@pytest.mark.django_db(transaction=True, databases=["default", "replica"])
def test_cross_database_write_is_rejected(settings):
    """A ScheduledTask forced onto a non-absurd database (here via .using on a second
    alias) is rejected before the row is inserted — schedules live only on the absurd
    DB, so no misplaced row and no phantom job is created."""
    settings.TASKS = build_pg_cron_tasks({})
    with pytest.raises(NotImplementedError) as exc:
        ScheduledTask.objects.using("replica").create(
            source="a",
            alias="default",
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
def test_cross_database_row_stays_deletable(settings):
    """A stray row created out-of-band on a foreign DB (bulk_create bypasses the pre_save
    guard) must stay deletable — the delete receiver skips it instead of raising, so it
    isn't trapped in the database."""
    settings.TASKS = build_pg_cron_tasks({})
    ScheduledTask.objects.using("replica").bulk_create(
        [
            ScheduledTask(
                source="a",
                alias="default",
                name="stray",
                task="tests.tasks.add",
                cron="0 2 * * *",
            )
        ]
    )
    ScheduledTask.objects.using("replica").filter(name="stray").delete()  # no raise
    assert not ScheduledTask.objects.using("replica").filter(name="stray").exists()


def test_loaddata_schedules_the_job(settings):
    """loaddata is a real write, so the row's job materializes — the row is the source
    of truth, so a loaded/restored schedule is a live schedule."""
    settings.TASKS = build_pg_cron_tasks({})
    call_command("loaddata", LOADED_SCHEDULE_FIXTURE)
    assert ScheduledTask.objects.filter(source="a", name="loaded").exists()
    _, schedule, _, active = ScheduledTask.pg_cron.get_job("default", "loaded", "a")
    assert schedule == "0 5 * * *"
    assert active is True
