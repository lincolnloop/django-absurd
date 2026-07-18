import pytest
import pytest_django.fixtures

from django_absurd.pg_cron.models import ScheduledTask
from tests.pg_cron.utils import build_pg_cron_tasks

pytestmark = pytest.mark.django_db(transaction=True)


def test_settings_and_admin_schedule_may_share_a_name(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    """Namespaced by source, a settings and an admin schedule with the same
    name coexist as two distinct pg_cron jobs — no clash, no double-fire."""
    settings.TASKS = build_pg_cron_tasks({})
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
    assert ScheduledTask.pg_cron.get_job("nightly", "s") is not None
    assert ScheduledTask.pg_cron.get_job("nightly", "a") is not None


def test_revalidating_a_saved_admin_schedule_does_not_self_clash(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    """full_clean's uniqueness check excludes the row's own pk, so re-validating an
    existing admin schedule (e.g. after editing a field) does not clash with itself."""
    settings.TASKS = build_pg_cron_tasks({})
    scheduled_task = ScheduledTask.objects.create(
        source="a",
        name="nightly",
        task="tests.tasks.add",
        queue="default",
        cron="0 2 * * *",
    )
    scheduled_task.enabled = False
    scheduled_task.full_clean()
