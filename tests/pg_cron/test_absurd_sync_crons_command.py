import io
import sys

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from pytest_django import Settings

from django_absurd.backends import get_absurd_backends
from django_absurd.pg_cron import catalog
from django_absurd.pg_cron.choices import Source
from django_absurd.pg_cron.models import ScheduledTask
from django_absurd.pg_cron.reconcile import sync_crons
from tests.pg_cron import utils

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("kwargs", [{}, {"no_input": True, "teardown": True}])
def test_command_errors_when_inert(
    kwargs: dict[str, bool],
    settings: Settings,
) -> None:
    """Both sync and --teardown refuse to run when pg_cron scheduling is inert
    (a test DB / active test run without PG_CRON_ON_TEST_DB)."""
    settings.TASKS = utils.build_pg_cron_tasks({}, pg_cron_on_test_db=False)
    with pytest.raises(CommandError) as excinfo:
        call_command("absurd_sync_crons", **kwargs)
    assert str(excinfo.value) == (
        "Refusing to reconcile pg_cron jobs: scheduling is inert here — this is a "
        "test database or an active test run and PG_CRON_ON_TEST_DB is not enabled "
        "for backend 'default'."
    )


def test_sync_crons_command_malformed_schedule_raises_commanderror(
    settings: Settings,
) -> None:
    """A SCHEDULE entry missing task/cron must surface as a clean CommandError,
    not a raw KeyError traceback."""
    settings.TASKS = utils.build_pg_cron_tasks({"broken": {}})
    with pytest.raises(CommandError):
        call_command("absurd_sync_crons")


def test_sync_crons_command_no_backend_errors(
    settings: Settings,
) -> None:
    settings.TASKS = {
        "default": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}
    }
    with pytest.raises(CommandError) as excinfo:
        call_command("absurd_sync_crons")
    assert str(excinfo.value) == (
        "No Absurd backend configured. Add a django_absurd.backends.AbsurdBackend "
        "entry to TASKS."
    )


def test_sync_crons_command_creates_cron_jobs(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks(
        {
            "a": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
            "b": {"task": "tests.tasks.add", "cron": "0 3 * * *"},
        }
    )
    call_command("absurd_sync_crons")

    live_db = utils.fetch_live_database()
    jobs = [r[0] for r in utils.fetch_managed_jobs(live_db)]
    assert catalog.build_jobname(live_db, Source.SETTINGS, "a") in jobs
    assert catalog.build_jobname(live_db, Source.SETTINGS, "b") in jobs
    assert len(jobs) == 2

    out = capsys.readouterr().out
    assert out.strip() == "Synced 2 cron(s); pruned 0 — backend 'default'."


def test_sync_crons_command_writes_summary_to_stdout(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    call_command("absurd_sync_crons")

    out = capsys.readouterr().out
    assert out.strip() == "Synced 1 cron(s); pruned 0 — backend 'default'."


def test_sync_crons_command_is_idempotent(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    call_command("absurd_sync_crons")
    call_command("absurd_sync_crons")

    assert len(utils.fetch_managed_jobs(utils.fetch_live_database())) == 1


def test_teardown_removes_owned_cron_jobs(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks(
        {
            "a": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
            "b": {"task": "tests.tasks.add", "cron": "0 3 * * *"},
        }
    )
    be = get_absurd_backends()["default"]
    sync_crons(be)
    assert len(utils.fetch_managed_jobs(utils.fetch_live_database())) == 2

    call_command("absurd_sync_crons", teardown=True, no_input=True)

    assert utils.fetch_managed_jobs(utils.fetch_live_database()) == []
    assert not ScheduledTask.objects.filter(source="s").exists()

    out = capsys.readouterr().out
    assert (
        out.strip() == "Unscheduled all pg_cron jobs and removed 2 schedule row(s) "
        "— backend 'default'."
    )


def test_teardown_command_deletes_admin_job_and_row_after_confirmation(
    settings: Settings,
) -> None:
    settings.TASKS = utils.build_pg_cron_tasks({})
    ScheduledTask.objects.create(
        source="a",
        name="killme",
        task="tests.tasks.add",
        cron="0 2 * * *",
    )
    assert (
        utils.fetch_cron_job(
            catalog.build_jobname(utils.fetch_live_database(), Source.ADMIN, "killme")
        )
        is not None
    )

    original_stdin = sys.stdin
    sys.stdin = io.StringIO("yes\n")  # confirm the destructive teardown
    try:
        call_command("absurd_sync_crons", teardown=True)
    finally:
        sys.stdin = original_stdin

    assert (
        utils.fetch_cron_job(
            catalog.build_jobname(utils.fetch_live_database(), Source.ADMIN, "killme")
        )
        is None
    )
    assert not ScheduledTask.objects.filter(source="a", name="killme").exists()


def test_teardown_admin_schedule_does_not_resurrect_on_next_sync(
    settings: Settings,
) -> None:
    """--teardown deletes the admin rows, so the next reconcile (which re-emits admin
    rows) has nothing to resurrect — the destructive teardown is terminal."""
    settings.TASKS = utils.build_pg_cron_tasks({})
    ScheduledTask.objects.create(
        source="a",
        name="gone-for-good",
        task="tests.tasks.add",
        cron="0 2 * * *",
    )
    call_command("absurd_sync_crons", teardown=True, no_input=True)
    assert not ScheduledTask.objects.filter(source="a", name="gone-for-good").exists()

    call_command("absurd_sync_crons")  # reconcile + admin re-emit
    assert (
        utils.fetch_cron_job(
            catalog.build_jobname(
                utils.fetch_live_database(), Source.ADMIN, "gone-for-good"
            )
        )
        is None
    )


@pytest.mark.parametrize("stdin_text", ["", "no\n"])
def test_teardown_command_aborts_without_confirmation(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    stdin_text: str,
) -> None:
    # "no\n" declines; "" is a non-interactive EOF (CI / docker exec -T) — both abort
    # without touching the job
    settings.TASKS = utils.build_pg_cron_tasks({})
    ScheduledTask.objects.create(
        source="a",
        name="keepme",
        task="tests.tasks.add",
        cron="0 2 * * *",
    )
    original_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin_text)
    try:
        call_command("absurd_sync_crons", teardown=True)
    finally:
        sys.stdin = original_stdin

    assert "Aborted." in capsys.readouterr().out  # (stdout also holds input()'s prompt)
    assert (
        utils.fetch_cron_job(
            catalog.build_jobname(utils.fetch_live_database(), Source.ADMIN, "keepme")
        )
        is not None
    )
