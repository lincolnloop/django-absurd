"""absurd.E011 (test-DB composition) / absurd.E012 (central-extension fail-safe)."""

import pytest
from django.core.checks import Tags
from django.core.checks.registry import registry
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from pytest_django import Settings

from django_absurd import connection
from django_absurd.pg_cron import checks, detection
from tests.pg_cron import utils

pytestmark = pytest.mark.django_db(transaction=True)


def test_composition_check_rejects_sync_on_test_db_without_opt_in(
    settings: Settings,
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=False)
    settings.TASKS["default"]["OPTIONS"]["SYNC_SCHEDULES_ON_TEST_DB"] = True
    with pytest.raises(SystemCheckError) as excinfo:
        call_command("check", "django_absurd")
    assert (
        "?: (absurd.E011) django-absurd: OPTIONS['SYNC_SCHEDULES_ON_TEST_DB'] is True"
        " without OPTIONS['PG_CRON_ON_TEST_DB'].\n"
        "\tHINT: Set OPTIONS['PG_CRON_ON_TEST_DB'] = True as well, or turn off"
        " SYNC_SCHEDULES_ON_TEST_DB."
    ) in str(excinfo.value)


def test_composition_check_passes_with_both_opted_in(
    settings: Settings,
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)
    settings.TASKS["default"]["OPTIONS"]["SYNC_SCHEDULES_ON_TEST_DB"] = True
    call_command("check", "django_absurd")


def test_composition_check_passes_with_sync_off(
    settings: Settings,
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=False)
    settings.TASKS["default"]["OPTIONS"]["SYNC_SCHEDULES_ON_TEST_DB"] = False
    call_command("check", "django_absurd")


def test_central_extension_check_registered_under_database_tag() -> None:
    assert checks.check_pg_cron_central_extension in registry.registered_checks
    assert Tags.database in checks.check_pg_cron_central_extension.tags


def test_central_extension_check_skips_under_test_environment(
    settings: Settings,
) -> None:
    """The check body is gated to skip while the test env is active — call_command
    passes --database, so the check runs, but the test-env skip means no error."""
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)
    call_command("check", "django_absurd", "--database", "default")


def test_central_extension_check_skips_beat_scheduler_backend(
    settings: Settings,
) -> None:
    """When pg_cron is uninstalled process-wide, backend.scheduler resolves to
    'beat'; the central-extension check must not fire for it."""
    settings.INSTALLED_APPS = [
        app for app in settings.INSTALLED_APPS if app != "django_absurd.pg_cron"
    ]
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)
    call_command("check", "django_absurd", "--database", "default")


def test_central_extension_check_skips_backend_on_other_database(
    settings: Settings,
) -> None:
    """The backend's DATABASE is "default"; checking a different alias ("replica")
    must not touch it, exercising the databases-filter branch."""
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=True)
    call_command("check", "django_absurd", "--database", "replica")


def test_central_extension_check_reports_a_database_without_the_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guards off, central lookup pointed at this suite's own test DB — which carries no
    extension — so the probe's real query answers the missing-extension branch."""
    monkeypatch.setattr(detection, "test_environment_active", lambda: False)
    monkeypatch.setattr(detection, "is_test_database", lambda _alias: False)
    monkeypatch.setattr(
        connection, "resolve_cron_database", lambda _alias: utils.fetch_live_database()
    )
    with pytest.raises(SystemCheckError) as excinfo:
        call_command("check", "django_absurd", "--database", "default")
    assert str(excinfo.value) == (
        "SystemCheckError: System check identified some issues:\n"
        "\n"
        "ERRORS:\n"
        "?: (absurd.E012) django-absurd: the central pg_cron catalog is unreachable"
        " or missing the pg_cron extension.\n"
        "\tHINT: Install pg_cron on the database named by cron.database_name"
        " (CREATE EXTENSION pg_cron) and ensure it's reachable from this server.\n"
        "\n"
        "System check identified 1 issue (0 silenced)."
    )


def test_central_extension_check_passes_against_the_real_central_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guards off and the resolver left alone: the central `postgres` DB HAS pg_cron, so
    the probe's success path runs and the check reports nothing."""
    monkeypatch.setattr(detection, "test_environment_active", lambda: False)
    monkeypatch.setattr(detection, "is_test_database", lambda _alias: False)
    call_command("check", "django_absurd", "--database", "default")
