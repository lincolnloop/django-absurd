"""E008: SCHEDULER='pg_cron' requires the pg_cron app — genuine absence in this suite."""

import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError

from django_absurd.checks import E008_HINT, E008_MSG

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
    assert E008_MSG in out
    assert E008_HINT in out


def test_beat_scheduler_without_app_clean(capsys, settings):
    out = run_check(capsys, settings, "beat")
    assert "absurd.E008" not in out
    assert "absurd.W003" not in out
