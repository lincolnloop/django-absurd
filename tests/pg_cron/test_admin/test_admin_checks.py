import typing as t

import pytest
from django.contrib import admin
from django.core import checks

from django_absurd.pg_cron.admin import ScheduledTaskAdmin
from django_absurd.pg_cron.models import ScheduledTask

pytestmark = pytest.mark.django_db(transaction=True)


def test_scheduledtask_admin_is_registered() -> None:
    """Guard the check test below: it only catches a bad field if the admin is
    actually registered when checks run."""
    assert admin.site._registry.get(ScheduledTask).__class__ is ScheduledTaskAdmin


def test_scheduledtask_admin_has_no_config_errors() -> None:
    """The hand-listed list_display/fieldsets field names must reference real
    fields — Django surfaces a typo only as admin.E0* under the check framework,
    and registration happens under contextlib.suppress at import, so without this
    guard a bad field name would ship silently."""
    admin_errors: list[t.Any] = [
        e
        for e in checks.run_checks(tags=["admin"])
        if e.id is not None and e.id.startswith("admin.E")
    ]
    assert admin_errors == [], admin_errors
