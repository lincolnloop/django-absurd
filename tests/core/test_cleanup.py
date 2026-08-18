import collections.abc
import contextlib
import datetime as dt
import io
import logging
import re
import sys
import typing as t

import psycopg.errors
import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import connection
from django.db.utils import ProgrammingError
from django.utils import timezone
from pytest_django import Settings

from django_absurd import worker
from django_absurd.backends import get_absurd_backends
from django_absurd.cleanup import QueueCleanup, cleanup_queues
from django_absurd.queues import get_absurd_client
from django_absurd.scheduler import run_beat
from django_absurd.test import AbsurdTestRuntime, FrozenTime
from tests import tasks, utils

if t.TYPE_CHECKING:
    import django_absurd.backends

    CleanupCallable = t.Callable[..., list[QueueCleanup]]

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("_isolate_queues"),
]

ABSURD = "django_absurd.backends.AbsurdBackend"
BEAT_EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def sync_queue(
    settings: Settings,
    cleanup_ttl: str = "0 seconds",
    cleanup_limit: int = 1000,
    names: tuple[str, ...] = ("default",),
    cleanup: dict[str, t.Any] | None = None,
) -> None:
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


def drain(queue: str = "default") -> None:
    worker.drain_queue(queue)


@pytest.fixture(params=["command", "direct"])
def cleanup(
    capsys: pytest.CaptureFixture[str],
    request: pytest.FixtureRequest,
) -> "CleanupCallable":
    """Run cleanup through both entrypoints (management command + direct call),
    each normalized to the per-queue count dicts, so behavioral tests cover both.
    The command path parses its stdout back into dicts."""

    def run(queues: list[str] | None = None) -> list[QueueCleanup]:
        if request.param == "direct":
            return cleanup_queues(queues)
        capsys.readouterr()  # discard any prior output
        call_command("absurd_cleanup", *(queues or []))
        return [
            parse_cleanup_line(line) for line in capsys.readouterr().out.splitlines()
        ]

    return run


def parse_cleanup_line(line: str) -> QueueCleanup:
    match = re.fullmatch(r"(.+): (\d+) tasks, (\d+) events deleted", line)
    assert match is not None
    return {
        "queue_name": match[1],
        "tasks_deleted": int(match[2]),
        "events_deleted": int(match[3]),
    }


@contextlib.contextmanager
def answer(text: str) -> collections.abc.Iterator[None]:
    """Feed a line to the next input() prompt via a real stdin (no mock)."""
    original = sys.stdin
    sys.stdin = io.StringIO(text)
    try:
        yield
    finally:
        sys.stdin = original


def test_cleanup_deletes_aged_terminal_tasks(
    caplog: pytest.LogCaptureFixture,
    cleanup: "CleanupCallable",
    settings: Settings,
) -> None:
    sync_queue(settings)
    tasks.add.enqueue(2, 3)
    drain()
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        result = cleanup()
    assert result == [
        {"queue_name": "default", "tasks_deleted": 1, "events_deleted": 0}
    ]
    records = [r for r in caplog.records if r.name == "django_absurd.cleanup"]
    assert len(records) == 1
    assert records[0].getMessage() == "cleanup removed rows: default: tasks=1 events=0"


def test_cleanup_skips_non_terminal_tasks(
    cleanup: "CleanupCallable",
    settings: Settings,
) -> None:
    sync_queue(settings)
    tasks.add.enqueue(2, 3)  # pending — worker not run, so not terminal
    assert cleanup() == [
        {"queue_name": "default", "tasks_deleted": 0, "events_deleted": 0}
    ]
    drain()  # now completed → terminal
    assert cleanup() == [
        {"queue_name": "default", "tasks_deleted": 1, "events_deleted": 0}
    ]


def test_cleanup_respects_batch_limit(
    cleanup: "CleanupCallable",
    settings: Settings,
) -> None:
    sync_queue(settings, cleanup_limit=2)
    for _ in range(3):
        tasks.add.enqueue(2, 3)
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


def test_cleanup_targets_specific_queue(
    cleanup: "CleanupCallable",
    settings: Settings,
) -> None:
    sync_queue(settings, names=("default", "other"))
    tasks.add.enqueue(2, 3)  # default
    tasks.routed.enqueue()  # routed is @task(queue_name="other")
    drain("default")
    drain("other")
    assert cleanup(["default"]) == [
        {"queue_name": "default", "tasks_deleted": 1, "events_deleted": 0}
    ]
    # 'other' was untouched, so its aged task is still there to clean
    assert cleanup(["other"]) == [
        {"queue_name": "other", "tasks_deleted": 1, "events_deleted": 0}
    ]


def test_cleanup_command_reports_per_queue_counts(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    sync_queue(settings)
    tasks.add.enqueue(2, 3)
    drain()
    capsys.readouterr()  # discard sync/worker output
    call_command("absurd_cleanup")
    assert capsys.readouterr().out == "default: 1 tasks, 0 events deleted\n"


def test_cleanup_does_not_relabel_an_unrelated_missing_relation(
    settings: Settings,
) -> None:
    """A missing relation inside ``absurd.cleanup_all_queues`` that is not the
    schema-absent shape (``InvalidSchemaName``/``UndefinedFunction``) surfaces as
    itself. Relabeling it "run migrate" would send the reader to the wrong door, and
    dropping the cause would hide which relation is actually missing.

    Driven the way it happens in production: a queue's own table renamed out from
    under the cleanup RPC, e.g. mid-migration or by an operator error — a case
    ``cleanup_queues`` never classifies as schema-absent, since the exception is a
    plain ``UndefinedTable``, not ``InvalidSchemaName``/``UndefinedFunction``.
    """
    sync_queue(settings)
    with connection.cursor() as cur:
        cur.execute("alter table absurd.t_default rename to t_default_probe")
    try:
        with pytest.raises(ProgrammingError) as excinfo:
            call_command("absurd_cleanup")
        cause = excinfo.value.__cause__
        assert isinstance(cause, psycopg.errors.UndefinedTable)
        assert (
            cause.diag.message_primary == 'relation "absurd.t_default" does not exist'
        )
    finally:
        with connection.cursor() as cur:
            cur.execute("alter table absurd.t_default_probe rename to t_default")


def test_cleanup_command_reports_no_backends(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    settings.TASKS = {}
    call_command("absurd_cleanup")
    assert capsys.readouterr().out == "No Absurd task backends configured.\n"


def test_flush_reports_no_backends(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    settings.TASKS = {}
    call_command("absurd_flush")
    assert capsys.readouterr().out == "No Absurd task backends configured.\n"


def test_flush_reports_no_queues(capsys: pytest.CaptureFixture[str]) -> None:
    call_command("absurd_flush")
    assert capsys.readouterr().out == "No queues to flush.\n"


def test_flush_noinput_drops_all_queues(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    sync_queue(settings, names=("default", "other"))
    capsys.readouterr()  # discard sync output
    call_command("absurd_flush", "--noinput")
    assert capsys.readouterr().out == "Dropped 2 queue(s): default, other\n"
    assert get_absurd_client().list_queues() == []


def test_flush_interactive_yes_drops_all_queues(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
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


def test_flush_interactive_no_keeps_queues(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
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


def test_flush_non_interactive_eof_keeps_queues(
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
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


def run_beat_until(
    frozen_time: FrozenTime,
    backend: "django_absurd.backends.AbsurdBackend",
    cutoff: dt.datetime,
) -> None:
    def fake_wait(timeout: float) -> bool:
        frozen_time.shift(dt.timedelta(seconds=timeout))
        return timezone.now() >= cutoff

    run_beat(backend, wait=fake_wait)


def test_beat_fires_cleanup_on_cadence(
    caplog: pytest.LogCaptureFixture,
    cleanup: "CleanupCallable",
    dj_absurd: AbsurdTestRuntime,
    settings: Settings,
) -> None:
    sync_queue(settings, cleanup={"schedule": "* * * * *"})
    backend = get_absurd_backends()["default"]
    with (
        dj_absurd.freeze_time(BEAT_EPOCH) as frozen_time,
        caplog.at_level(logging.INFO, logger="django_absurd"),
    ):
        tasks.add.enqueue(2, 3)
        drain()  # completed → aged-terminal (cleanup_ttl="0 seconds")
        run_beat_until(
            frozen_time, backend, dt.datetime(2026, 1, 1, 0, 1, 30, tzinfo=dt.UTC)
        )
        assert cleanup() == [
            {"queue_name": "default", "tasks_deleted": 0, "events_deleted": 0}
        ]

    ran = [
        r.getMessage()
        for r in caplog.records
        if r.name == "django_absurd.scheduler" and r.getMessage().startswith("cleanup")
    ]
    assert ran == ['cleanup ran: slot="2026-01-01T00:01:00Z"']


def test_beat_isolates_failing_cleanup(
    caplog: pytest.LogCaptureFixture,
    dj_absurd: AbsurdTestRuntime,
    settings: Settings,
) -> None:
    sync_queue(settings, cleanup={"schedule": "* * * * *"})
    backend = get_absurd_backends()["default"]
    with (
        utils.hide_absurd_schema(),
        dj_absurd.freeze_time(BEAT_EPOCH) as frozen_time,
        caplog.at_level(logging.ERROR, logger="django_absurd"),
    ):
        run_beat_until(
            frozen_time, backend, dt.datetime(2026, 1, 1, 0, 1, 30, tzinfo=dt.UTC)
        )
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [r.getMessage() for r in errors] == ["cleanup failed"]


def test_beat_fires_cleanup_and_task_same_slot(
    dj_absurd: AbsurdTestRuntime,
    settings: Settings,
) -> None:
    # A scheduled task and CLEANUP sharing a cron slot both fire in the one tick:
    # the task enqueues (pending → survives cleanup) and cleanup deletes the aged row.
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "QUEUES": {"default": {"cleanup_ttl": "0 seconds"}},
                "SCHEDULE": {
                    "g": {
                        "task": "tests.tasks.make_group",
                        "cron": "*/1 * * * *",
                        "args": ["fired"],
                    }
                },
                "CLEANUP": {"schedule": "*/1 * * * *"},
            },
        }
    }
    call_command("absurd_sync_queues")
    backend = get_absurd_backends()["default"]
    with dj_absurd.freeze_time(BEAT_EPOCH) as frozen_time:
        tasks.add.enqueue(2, 3)
        drain()  # completed → aged-terminal, cleanup-eligible
        run_beat_until(
            frozen_time, backend, dt.datetime(2026, 1, 1, 0, 1, 30, tzinfo=dt.UTC)
        )
        # cleanup fired this tick: the aged task is already gone, nothing left to delete
        assert cleanup_queues() == [
            {"queue_name": "default", "tasks_deleted": 0, "events_deleted": 0}
        ]
        # the scheduled task fired the same tick: run it and assert its side effect
        drain()
        assert Group.objects.filter(name="fired").exists()
