import pytest
from django.db import IntegrityError

from django_absurd.pg_cron.models import ScheduledJob

pytestmark = pytest.mark.django_db(transaction=True)


def test_roundtrip_defaults():
    job = ScheduledJob.objects.create(
        name="nightly",
        alias="default",
        task="tests.tasks.add",
        params={"args": [], "kwargs": {}},
        options={},
        cron="0 2 * * *",
    )
    assert job.source == "settings"
    assert job.enabled is True


def test_unique_per_source_alias_name():
    kw = {
        "alias": "default",
        "task": "tests.tasks.add",
        "params": {"args": [], "kwargs": {}},
        "options": {},
        "cron": "0 2 * * *",
    }
    ScheduledJob.objects.create(name="dup", source="settings", **kw)
    ScheduledJob.objects.create(name="dup", source="admin", **kw)  # other source OK
    with pytest.raises(IntegrityError):
        ScheduledJob.objects.create(name="dup", source="settings", **kw)
