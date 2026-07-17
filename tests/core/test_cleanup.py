import contextlib
import datetime as dt
import io
import logging
import re
import sys

import pytest
from django.core.management import call_command
from django.db import connection
from django.utils import timezone
from freezegun import freeze_time

from django_absurd.backends import get_absurd_backends
from django_absurd.cleanup import cleanup_queues
from django_absurd.queues import get_absurd_client
from django_absurd.scheduler import run_beat
from tests.tasks import add, routed

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


def sync_queue(
    settings,
    cleanup_ttl="0 seconds",
    cleanup_limit=1000,
    names=("default",),
    cleanup=None,
):
    options = {
        "QUEUES": {
            name: {"cleanup_ttl": cleanup_ttl, "cleanup_limit": cleanup_limit}
            for name in names
        }
    }
    if cleanup is not None:
        options["CLEANUP"] = cleanup
    settings.TASKS = {"default": {"BACKEND": ABSURD, "OPTIONS": options}}
    call_command("absurd_sync_queues")


def drain(queue="default"):
    call_command("absurd_worker", queue=queue, burst=True)


@pytest.fixture(params=["command", "direct"])
def cleanup(request, capsys):
    """Run cleanup through both entrypoints (management command + direct call), each
    normalized to the per-queue count dicts, so behavioral tests cover both. The command
    path parses its stdout back into dicts."""

    def run(queues=None):
        if request.param == "direct":
            return cleanup_queues(queues)
        capsys.readouterr()  # discard any prior output
        call_command("absurd_cleanup", *(queues or []))
        return [
            parse_cleanup_line(line) for line in capsys.readouterr().out.splitlines()
        ]

    return run


def parse_cleanup_line(line):
    match = re.fullmatch(r"(.+): (\d+) tasks, (\d+) events deleted", line)
    return {
        "queue_name": match[1],
        "tasks_deleted": int(match[2]),
        "events_deleted": int(match[3]),
    }


@contextlib.contextmanager
def answer(text):
    """Feed a line to the next input() prompt via a real stdin (no mock)."""
    original = sys.stdin
    sys.stdin = io.StringIO(text)
    try:
        yield
    finally:
        sys.stdin = original


def test_cleanup_deletes_aged_terminal_tasks(settings, cleanup):
    sync_queue(settings)
    add.enqueue(2, 3)
    drain()
    assert cleanup() == [
        {"queue_name": "default", "tasks_deleted": 1, "events_deleted": 0}
    ]


def test_cleanup_skips_non_terminal_tasks(settings, cleanup):
    sync_queue(settings)
    add.enqueue(2, 3)  # pending — worker not run, so not terminal
    assert cleanup() == [
        {"queue_name": "default", "tasks_deleted": 0, "events_deleted": 0}
    ]
    drain()  # now completed → terminal
    assert cleanup() == [
        {"queue_name": "default", "tasks_deleted": 1, "events_deleted": 0}
    ]


def test_cleanup_respects_batch_limit(settings, cleanup):
    sync_queue(settings, cleanup_limit=2)
    for _ in range(3):
        add.enqueue(2, 3)
    drain()
    assert cleanup() == [
        {"queue_name": "default", "tasks_deleted": 2, "events_deleted": 0}
    ]
    assert cleanup() == [
        {"queue_name": "default", "tasks_deleted": 1, "events_deleted": 0}
    ]
    assert cleanup() == [
        {"queue_name": "default", "tasks_deleted": 0, "events_deleted": 0}
    ]


def test_cleanup_targets_specific_queue(settings, cleanup):
    sync_queue(settings, names=("default", "other"))
    add.enqueue(2, 3)  # default
    routed.enqueue()  # routed is @task(queue_name="other")
    drain("default")
    drain("other")
    assert cleanup(["default"]) == [
        {"queue_name": "default", "tasks_deleted": 1, "events_deleted": 0}
    ]
    # 'other' was untouched, so its aged task is still there to clean
    assert cleanup(["other"]) == [
        {"queue_name": "other", "tasks_deleted": 1, "events_deleted": 0}
    ]


def test_cleanup_command_reports_per_queue_counts(settings, capsys):
    sync_queue(settings)
    add.enqueue(2, 3)
    drain()
    capsys.readouterr()  # discard sync/worker output
    call_command("absurd_cleanup")
    assert capsys.readouterr().out == "default: 1 tasks, 0 events deleted\n"


def test_cleanup_command_reports_no_backends(settings, capsys):
    settings.TASKS = {}
    call_command("absurd_cleanup")
    assert capsys.readouterr().out == "No Absurd task backends configured.\n"


def test_flush_reports_no_backends(settings, capsys):
    settings.TASKS = {}
    call_command("absurd_flush")
    assert capsys.readouterr().out == "No Absurd task backends configured.\n"


def test_flush_reports_no_queues(capsys):
    call_command("absurd_flush")
    assert capsys.readouterr().out == "No queues to flush.\n"


def test_flush_noinput_drops_all_queues(settings, capsys):
    sync_queue(settings, names=("default", "other"))
    capsys.readouterr()  # discard sync output
    call_command("absurd_flush", "--noinput")
    assert capsys.readouterr().out == "Dropped 2 queue(s): default, other\n"
    assert get_absurd_client().list_queues() == []


def test_flush_interactive_yes_drops_all_queues(settings, capsys):
    sync_queue(settings, names=("default", "other"))
    capsys.readouterr()  # discard sync output
    with answer("yes\n"):
        call_command("absurd_flush")
    assert capsys.readouterr().out == (
        "This will DROP 2 queue(s) and ALL their data: default, other\n"
        "Type 'yes' to continue, or 'no' to cancel: "
        "Dropped 2 queue(s): default, other\n"
    )
    assert get_absurd_client().list_queues() == []


def test_flush_interactive_no_keeps_queues(settings, capsys):
    sync_queue(settings, names=("default", "other"))
    capsys.readouterr()  # discard sync output
    with answer("no\n"):
        call_command("absurd_flush")
    assert capsys.readouterr().out == (
        "This will DROP 2 queue(s) and ALL their data: default, other\n"
        "Type 'yes' to continue, or 'no' to cancel: "
        "Flush cancelled.\n"
    )
    assert sorted(get_absurd_client().list_queues()) == ["default", "other"]


def test_flush_non_interactive_eof_keeps_queues(settings, capsys):
    sync_queue(settings, names=("default", "other"))
    capsys.readouterr()  # discard sync output
    with answer(""):  # empty stdin → input() raises EOFError
        call_command("absurd_flush")
    assert capsys.readouterr().out == (
        "This will DROP 2 queue(s) and ALL their data: default, other\n"
        "Type 'yes' to continue, or 'no' to cancel: "
        "Flush cancelled.\n"
    )
    assert sorted(get_absurd_client().list_queues()) == ["default", "other"]


def run_beat_until(backend, cutoff):
    with freeze_time("2026-01-01 00:00:00") as frozen:

        def fake_wait(timeout: float) -> bool:
            frozen.tick(dt.timedelta(seconds=timeout))
            return timezone.now() >= cutoff

        run_beat(backend, wait=fake_wait)


def test_beat_fires_cleanup_on_cadence(settings, cleanup):
    sync_queue(settings, cleanup={"schedule": "* * * * *"})
    backend = get_absurd_backends()["default"]
    add.enqueue(2, 3)
    drain()  # completed → aged-terminal (cleanup_ttl="0 seconds")
    run_beat_until(backend, dt.datetime(2026, 1, 1, 0, 1, 30, tzinfo=dt.UTC))
    assert cleanup() == [
        {"queue_name": "default", "tasks_deleted": 0, "events_deleted": 0}
    ]


def test_beat_isolates_failing_cleanup(settings, caplog):
    sync_queue(settings, cleanup={"schedule": "* * * * *"})
    backend = get_absurd_backends()["default"]
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS absurd CASCADE")
    try:
        with caplog.at_level(logging.ERROR, logger="django_absurd"):
            run_beat_until(backend, dt.datetime(2026, 1, 1, 0, 1, 30, tzinfo=dt.UTC))
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert [r.getMessage() for r in errors] == ["django-absurd cleanup failed"]
    finally:
        call_command("migrate", "django_absurd", "zero", verbosity=0)
        call_command("migrate", verbosity=0)  # restore absurd schema
