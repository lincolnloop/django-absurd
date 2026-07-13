import io
import sys

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from django_absurd.backends import get_absurd_backends
from django_absurd.pg_cron.models import ScheduledTask
from django_absurd.pg_cron.reconcile import sync_crons
from tests.pg_cron.utils import build_beat_tasks, build_pg_cron_tasks

pytestmark = pytest.mark.django_db(transaction=True)


def test_sync_crons_command_malformed_schedule_raises_commanderror(settings):
    """A SCHEDULE entry missing task/cron must surface as a clean CommandError,
    not a raw KeyError traceback."""
    settings.TASKS = build_pg_cron_tasks({"broken": {}})
    with pytest.raises(CommandError):
        call_command("absurd_sync_crons")


def test_sync_crons_command_creates_cron_jobs(settings, capsys):
    settings.TASKS = build_pg_cron_tasks(
        {
            "a": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
            "b": {"task": "tests.tasks.add", "cron": "0 3 * * *"},
        }
    )
    call_command("absurd_sync_crons")

    jobs = [r[0] for r in ScheduledTask.pg_cron.get_managed_jobs("default")]
    assert "absurd:settings:default:a" in jobs
    assert "absurd:settings:default:b" in jobs
    assert len(jobs) == 2

    out = capsys.readouterr().out
    assert out.strip() == "Synced 2 cron(s); pruned 0 — backend 'default'."


def test_sync_crons_command_writes_summary_to_stdout(settings, capsys):
    settings.TASKS = build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    call_command("absurd_sync_crons")

    out = capsys.readouterr().out
    assert out.strip() == "Synced 1 cron(s); pruned 0 — backend 'default'."


def test_sync_crons_command_refuses_when_scheduler_is_beat(settings):
    settings.TASKS = build_beat_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    with pytest.raises(CommandError, match="pg_cron"):
        call_command("absurd_sync_crons")


def test_sync_crons_command_is_idempotent(settings, capsys):
    settings.TASKS = build_pg_cron_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    call_command("absurd_sync_crons")
    call_command("absurd_sync_crons")

    assert len(ScheduledTask.pg_cron.get_managed_jobs("default")) == 1


def test_teardown_removes_owned_cron_jobs(settings, capsys):
    settings.TASKS = build_pg_cron_tasks(
        {
            "a": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
            "b": {"task": "tests.tasks.add", "cron": "0 3 * * *"},
        }
    )
    be = get_absurd_backends()["default"]
    sync_crons(be)
    assert len(ScheduledTask.pg_cron.get_managed_jobs("default")) == 2

    call_command("absurd_sync_crons", teardown=True, no_input=True)

    assert ScheduledTask.pg_cron.get_managed_jobs("default") == []
    assert not ScheduledTask.objects.filter(source="settings", alias="default").exists()

    out = capsys.readouterr().out
    assert (
        out.strip() == "Unscheduled all pg_cron jobs and removed 2 schedule row(s) "
        "— backend 'default'."
    )


def test_teardown_allowed_when_scheduler_is_beat(settings, capsys):
    settings.TASKS = build_beat_tasks(
        {"a": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    call_command("absurd_sync_crons", teardown=True, no_input=True)

    out = capsys.readouterr().out
    assert (
        out.strip() == "Unscheduled all pg_cron jobs and removed 0 schedule row(s) "
        "— backend 'default'."
    )


def test_teardown_command_deletes_admin_job_and_row_after_confirmation(settings):
    settings.TASKS = build_pg_cron_tasks({})
    ScheduledTask.objects.create(
        source="admin",
        alias="default",
        name="killme",
        task="tests.tasks.add",
        cron="0 2 * * *",
    )
    assert ScheduledTask.pg_cron.get_job("default", "killme", "admin") is not None

    original_stdin = sys.stdin
    sys.stdin = io.StringIO("yes\n")  # confirm the destructive teardown
    try:
        call_command("absurd_sync_crons", teardown=True)
    finally:
        sys.stdin = original_stdin

    assert ScheduledTask.pg_cron.get_job("default", "killme", "admin") is None
    assert not ScheduledTask.objects.filter(source="admin", name="killme").exists()


def test_teardown_admin_schedule_does_not_resurrect_on_next_sync(settings):
    """--teardown deletes the admin rows, so the next reconcile (which re-emits admin
    rows) has nothing to resurrect — the destructive teardown is terminal."""
    settings.TASKS = build_pg_cron_tasks({})
    ScheduledTask.objects.create(
        source="admin",
        alias="default",
        name="gone-for-good",
        task="tests.tasks.add",
        cron="0 2 * * *",
    )
    call_command("absurd_sync_crons", teardown=True, no_input=True)
    assert not ScheduledTask.objects.filter(
        source="admin", name="gone-for-good"
    ).exists()

    call_command("absurd_sync_crons")  # reconcile + admin re-emit
    assert ScheduledTask.pg_cron.get_job("default", "gone-for-good", "admin") is None


@pytest.mark.parametrize("stdin_text", ["", "no\n"])
def test_teardown_command_aborts_without_confirmation(settings, capsys, stdin_text):
    # "no\n" declines; "" is a non-interactive EOF (CI / docker exec -T) — both abort
    # without touching the job
    settings.TASKS = build_pg_cron_tasks({})
    ScheduledTask.objects.create(
        source="admin",
        alias="default",
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
    assert ScheduledTask.pg_cron.get_job("default", "keepme", "admin") is not None
