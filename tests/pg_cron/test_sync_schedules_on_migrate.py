"""Tests for SYNC_SCHEDULES_ON_MIGRATE / SYNC_SCHEDULES_ON_TEST_DB
(django_absurd/pg_cron/apps.py). Not in test_pg_cron_post_migrate.py: that file's
run_cron_sync fixture exists specifically to prove absurd_sync_crons/migrate are
IDENTICAL — the opposite of what these tests show."""

import os
import subprocess
import sys
import typing as t
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import connections

from django_absurd.pg_cron.models import ScheduledTask
from tests.pg_cron.utils import build_pg_cron_tasks

if t.TYPE_CHECKING:
    import pytest_django.fixtures

pytestmark = pytest.mark.django_db(transaction=True)

REPO_ROOT = Path(__file__).resolve().parents[2]


def migrate_real_db_in_subprocess(*, sync_on_migrate: bool | None) -> None:
    # sync_on_migrate=None omits the env var entirely, exercising the real shipped
    # default rather than an explicit True standing in for it.
    params = connections["default"].get_connection_params()
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "SUBPROCESS_MIGRATE_DBNAME": params["dbname"],
        "SUBPROCESS_MIGRATE_USER": params.get("user", ""),
        "SUBPROCESS_MIGRATE_PASSWORD": params.get("password", ""),
        "SUBPROCESS_MIGRATE_HOST": params.get("host", "localhost"),
        "SUBPROCESS_MIGRATE_PORT": str(params.get("port", "")),
    }
    if sync_on_migrate is None:
        # Hermetic: a stray SYNC_SCHEDULES_ON_MIGRATE in the outer environment must
        # not leak into the subprocess and stand in for the real shipped default.
        env.pop("SYNC_SCHEDULES_ON_MIGRATE", None)
    else:
        env["SYNC_SCHEDULES_ON_MIGRATE"] = "1" if sync_on_migrate else "0"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "django",
            "migrate",
            "--settings=tests.pg_cron.live_settings",
            "--verbosity=0",
        ],
        env=env,
        cwd=REPO_ROOT,
        check=True,
    )


@pytest.fixture
def real_db_migration_cycle() -> "t.Iterator[None]":
    """Roll back JUST django_absurd_pg_cron's own migrations (not the whole
    database — that app owns CREATE EXTENSION pg_cron, so this is a genuine
    from-scratch migrate for the exact feature under test) so the subprocess's
    migrate is a real first-time provisioning run. Restores the schema afterward
    so the rest of this --reuse-db session's tests aren't affected."""
    call_command("migrate", "django_absurd_pg_cron", "zero", verbosity=0)
    try:
        yield
    finally:
        call_command("migrate", "django_absurd_pg_cron", "zero", verbosity=0)
        call_command("migrate", verbosity=0)


def test_migrate_syncs_by_default_on_a_real_non_test_database(
    real_db_migration_cycle: None,
) -> None:
    # No SYNC_SCHEDULES_ON_MIGRATE key at all — proves the real shipped default
    # (True), not an explicit True standing in for it.
    migrate_real_db_in_subprocess(sync_on_migrate=None)
    scheduled_task = ScheduledTask.objects.get(name="nightly", source="s")
    assert scheduled_task.get_pg_cron_job() is not None


def test_migrate_skips_sync_on_a_real_database_when_explicitly_disabled(
    real_db_migration_cycle: None,
) -> None:
    # Genuine RED before the guard exists: SYNC_SCHEDULES_ON_MIGRATE doesn't exist
    # yet, so it's silently ignored and sync happens anyway — this assertion fails.
    # GREEN once the guard respects it.
    migrate_real_db_in_subprocess(sync_on_migrate=False)
    assert not ScheduledTask.objects.filter(name="nightly", source="s").exists()


def test_migrate_skips_sync_by_default_on_a_test_database(
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    settings.TASKS = build_pg_cron_tasks(
        {"nightly": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    # No SYNC_SCHEDULES_ON_TEST_DB key at all (build_pg_cron_tasks's own default is
    # True, for this project's own suite) — proves the real shipped default
    # (False), not an explicit False standing in for it.
    del settings.TASKS["default"]["OPTIONS"]["SYNC_SCHEDULES_ON_TEST_DB"]

    call_command("migrate", verbosity=0)

    assert ScheduledTask.pg_cron.get_managed_jobs() == []
    assert ScheduledTask.objects.filter(source="s").count() == 0


def test_migrate_syncs_on_a_test_database_when_explicitly_enabled(
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    settings.TASKS = build_pg_cron_tasks(
        {"nightly": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    settings.TASKS["default"]["OPTIONS"]["SYNC_SCHEDULES_ON_TEST_DB"] = True

    call_command("migrate", verbosity=0)

    assert [r[0] for r in ScheduledTask.pg_cron.get_managed_jobs()] == ["_dj:s:nightly"]


def test_scheduled_task_create_and_delete_are_unaffected_by_either_setting(
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    settings.TASKS = build_pg_cron_tasks({})
    settings.TASKS["default"]["OPTIONS"]["SYNC_SCHEDULES_ON_TEST_DB"] = False

    scheduled_task = ScheduledTask.objects.create(
        source="a",
        name="direct_create",
        task="tests.tasks.add",
        cron="0 2 * * *",
    )
    assert scheduled_task.get_pg_cron_job() is not None

    scheduled_task.delete()
    assert ScheduledTask.pg_cron.get_job("direct_create", "a") is None
