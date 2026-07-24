import pytest
import pytest_django.fixtures

from django_absurd.pg_cron import catalog
from django_absurd.pg_cron.choices import Source
from django_absurd.pg_cron.models import ScheduledTask
from tests.pg_cron import utils

pytestmark = pytest.mark.django_db(transaction=True)


def test_settings_and_admin_schedule_may_share_a_name(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    """Namespaced by source, a settings and an admin schedule with the same
    name coexist as two distinct pg_cron jobs — no clash, no double-fire."""
    settings.TASKS = utils.build_pg_cron_tasks({})
    ScheduledTask.objects.create(
        source="s",
        name="nightly",
        task="tests.tasks.add",
        cron="0 2 * * *",
    )
    ScheduledTask.objects.create(
        source="a",
        name="nightly",
        task="tests.tasks.add",
        cron="0 3 * * *",
    )
    live_db = utils.fetch_live_database()
    assert (
        utils.fetch_cron_job(catalog.build_jobname(live_db, Source.SETTINGS, "nightly"))
        is not None
    )
    assert (
        utils.fetch_cron_job(catalog.build_jobname(live_db, Source.ADMIN, "nightly"))
        is not None
    )


def test_revalidating_a_saved_admin_schedule_does_not_self_clash(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    """full_clean's uniqueness check excludes the row's own pk, so re-validating an
    existing admin schedule (e.g. after editing a field) does not clash with itself."""
    settings.TASKS = utils.build_pg_cron_tasks({})
    scheduled_task = ScheduledTask.objects.create(
        source="a",
        name="nightly",
        task="tests.tasks.add",
        queue="default",
        cron="0 2 * * *",
    )
    scheduled_task.enabled = False
    scheduled_task.full_clean()
