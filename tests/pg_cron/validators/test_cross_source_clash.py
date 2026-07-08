from django_absurd.pg_cron.models import ScheduledTask
from tests.pg_cron.validators.utils import validate_from_model


def test_admin_row_clashing_with_settings_rejected(settings):
    # model-only: needs a committed settings row to clash against
    ScheduledTask.objects.create(
        source="settings",
        alias="default",
        name="nightly",
        task="tests.tasks.add",
        cron="0 2 * * *",
    )
    result = validate_from_model(settings, source="admin", name="nightly")
    assert result
    assert (
        "a settings schedule 'nightly' already exists on backend 'default'." in result
    )


def test_same_source_does_not_clash(settings):
    ScheduledTask.objects.create(
        source="settings",
        alias="default",
        name="nightly",
        task="tests.tasks.add",
        cron="0 2 * * *",
    )
    result = validate_from_model(settings, source="settings", name="nightly")
    assert not result or "already exists on backend" not in result
