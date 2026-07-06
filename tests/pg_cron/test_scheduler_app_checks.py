"""W003: pg_cron app ordered before django_absurd — app genuinely present in this suite."""

import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError

from django_absurd.checks import W003_HINT, W003_MSG

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"
BASE_QUEUES: dict = {"default": {}, "other": {}, "reports": {}}


def run_check(capsys, settings, installed_apps=None):
    if installed_apps is not None:
        settings.INSTALLED_APPS = installed_apps
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {"SCHEDULER": "pg_cron", "QUEUES": BASE_QUEUES},
        }
    }
    try:
        call_command("check", "django_absurd")
    except SystemCheckError as exc:
        cap = capsys.readouterr()
        return cap.out + cap.err + str(exc)
    cap = capsys.readouterr()
    return cap.out + cap.err


def build_apps_with_pg_cron_first(settings):
    apps_without = [
        app for app in settings.INSTALLED_APPS if app != "django_absurd.pg_cron"
    ]
    return ["django_absurd.pg_cron", *apps_without]


def test_pg_cron_app_before_core_warns(capsys, settings):
    out = run_check(capsys, settings, build_apps_with_pg_cron_first(settings))
    assert "absurd.W003" in out
    assert W003_MSG in out
    assert W003_HINT in out


def test_pg_cron_app_after_core_clean(capsys, settings):
    out = run_check(capsys, settings)
    assert "absurd.E008" not in out
    assert "absurd.W003" not in out


def test_pg_cron_app_config_path_before_core_warns(capsys, settings):
    """Dotted AppConfig path for pg_cron listed before core must still trigger W003."""
    apps_with_config_path_first = [
        "django_absurd.pg_cron.apps.PgCronConfig",
        *[
            app
            for app in settings.INSTALLED_APPS
            if app
            not in ("django_absurd.pg_cron", "django_absurd.pg_cron.apps.PgCronConfig")
        ],
    ]
    out = run_check(capsys, settings, installed_apps=apps_with_config_path_first)
    assert "absurd.W003" in out
    assert W003_MSG in out
    assert W003_HINT in out
