"""E008/W003 checks: pg_cron scheduler requires the pg_cron app, ordered after core."""

import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError

ABSURD = "django_absurd.backends.AbsurdBackend"

BASE_QUEUES: dict = {"default": {}, "other": {}, "reports": {}}


@pytest.fixture
def run_check(capsys, settings):
    def _run(scheduler, installed_apps=None):
        if installed_apps is not None:
            settings.INSTALLED_APPS = installed_apps
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

    return _run


def apps_without_pg_cron(settings):
    return [app for app in settings.INSTALLED_APPS if app != "django_absurd.pg_cron"]


def apps_with_pg_cron_first(settings):
    return ["django_absurd.pg_cron", *apps_without_pg_cron(settings)]


def test_pg_cron_scheduler_without_app_errors(settings, run_check):
    out = run_check("pg_cron", installed_apps=apps_without_pg_cron(settings))
    assert "absurd.E008" in out
    assert (
        "django-absurd: SCHEDULER is 'pg_cron' but 'django_absurd.pg_cron'"
        " is not in INSTALLED_APPS."
    ) in out
    assert "Add 'django_absurd.pg_cron' to INSTALLED_APPS," in out


def test_beat_scheduler_without_app_clean(settings, run_check):
    out = run_check("beat", installed_apps=apps_without_pg_cron(settings))
    assert "absurd.E008" not in out
    assert "absurd.W003" not in out


def test_pg_cron_app_before_core_warns(settings, run_check):
    out = run_check("pg_cron", installed_apps=apps_with_pg_cron_first(settings))
    assert "absurd.W003" in out
    assert (
        "django-absurd: 'django_absurd.pg_cron' is ordered before 'django_absurd'"
        " in INSTALLED_APPS"
    ) in out


def test_pg_cron_app_after_core_clean(run_check):
    out = run_check("pg_cron")
    assert "absurd.E008" not in out
    assert "absurd.W003" not in out
