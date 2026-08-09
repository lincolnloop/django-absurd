"""W003: pg_cron app ordered before django_absurd — app genuinely present in this
suite."""

import typing as t

import pytest
import pytest_django.fixtures
from django.core.management import call_command
from django.core.management.base import SystemCheckError

from tests.utils import make_tasks_settings

pytestmark = pytest.mark.django_db(transaction=True)


def run_check(
    capsys: pytest.CaptureFixture[str],
    settings: pytest_django.fixtures.SettingsWrapper,
    installed_apps: t.Sequence[str] | None = None,
    schedule: dict[str, dict[str, object]] | None = None,
) -> str:
    if installed_apps is not None:
        settings.INSTALLED_APPS = installed_apps
    settings.TASKS = make_tasks_settings(schedule=schedule)
    try:
        call_command("check", "django_absurd")
    except SystemCheckError as exc:
        cap = capsys.readouterr()
        return cap.out + cap.err + str(exc)
    cap = capsys.readouterr()
    return cap.out + cap.err


def build_apps_with_pg_cron_first(
    settings: pytest_django.fixtures.SettingsWrapper,
) -> list[str]:
    apps_without = [
        app for app in settings.INSTALLED_APPS if app != "django_absurd.pg_cron"
    ]
    return ["django_absurd.pg_cron", *apps_without]


def test_pg_cron_app_before_core_warns(
    capsys: pytest.CaptureFixture[str],
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    out = run_check(capsys, settings, build_apps_with_pg_cron_first(settings))
    assert "absurd.W003" in out
    assert (
        "django-absurd: 'django_absurd.pg_cron' is ordered before 'django_absurd'"
        " in INSTALLED_APPS (its post_migrate cron reconcile runs before queue"
        " provisioning)."
    ) in out
    assert (
        "Place 'django_absurd.pg_cron' after 'django_absurd' in INSTALLED_APPS." in out
    )


def test_pg_cron_app_after_core_clean(
    capsys: pytest.CaptureFixture[str],
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    out = run_check(capsys, settings)
    assert "absurd.W003" not in out


def test_pg_cron_schedule_error_reported(
    capsys: pytest.CaptureFixture[str],
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    out = run_check(
        capsys,
        settings,
        schedule={
            "nightly": {
                "task": "tests.tasks.add",
                "cron": "0 2 * * *",
                "queue": "ghost",
            }
        },
    )
    assert "absurd.E007" in out
    assert "queue 'ghost' is not declared." in out


def test_pg_cron_app_config_path_before_core_warns(
    capsys: pytest.CaptureFixture[str],
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
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
    assert (
        "django-absurd: 'django_absurd.pg_cron' is ordered before 'django_absurd'"
        " in INSTALLED_APPS (its post_migrate cron reconcile runs before queue"
        " provisioning)."
    ) in out
    assert (
        "Place 'django_absurd.pg_cron' after 'django_absurd' in INSTALLED_APPS." in out
    )


def test_pg_cron_installed_without_an_absurd_backend_is_reported(
    capsys: pytest.CaptureFixture[str],
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    # A typo'd BACKEND, a wrong alias, or an app-ordering slip leaves the scheduler app
    # installed with nothing to schedule for. Every ScheduledTask then saves normally
    # and never gets a job, so say it at check time — before any row exists.
    settings.TASKS = {
        "default": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}
    }
    try:
        call_command("check", "django_absurd")
    except SystemCheckError as exc:
        out = capsys.readouterr().err + str(exc)
    else:
        out = capsys.readouterr().err
    assert "absurd.E013" in out
    assert (
        "django-absurd: 'django_absurd.pg_cron' is installed, but no AbsurdBackend"
        " is configured." in out
    )


def test_pg_cron_installed_with_a_backend_is_not_reported(
    capsys: pytest.CaptureFixture[str],
    settings: pytest_django.fixtures.SettingsWrapper,
) -> None:
    assert "absurd.E013" not in run_check(capsys, settings)
