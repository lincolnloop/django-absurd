"""E008: SCHEDULER='pg_cron' requires the pg_cron app — genuine absence in this suite."""

import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"
BASE_QUEUES: dict = {"default": {}, "other": {}, "reports": {}}


def run_check(capsys, settings, scheduler):
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {"SCHEDULER": scheduler, "QUEUES": BASE_QUEUES},
        }
    }
    try:
        call_command("check", "django_absurd")
    except SystemCheckError as exc:
        cap = capsys.readouterr()
        return cap.out + cap.err + str(exc)
    cap = capsys.readouterr()
    return cap.out + cap.err


def test_pg_cron_scheduler_without_app_errors(capsys, settings):
    out = run_check(capsys, settings, "pg_cron")
    assert "absurd.E008" in out
    assert (
        "django-absurd: SCHEDULER is 'pg_cron' but 'django_absurd.pg_cron'"
        " is not in INSTALLED_APPS."
    ) in out
    assert (
        "Add 'django_absurd.pg_cron' to INSTALLED_APPS, after 'django_absurd'." in out
    )


def test_beat_scheduler_without_app_clean(capsys, settings):
    out = run_check(capsys, settings, "beat")
    assert "absurd.E008" not in out
    assert "absurd.W003" not in out
