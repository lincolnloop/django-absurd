import asyncio
import contextlib
import datetime as dt
import logging
import os
import signal
import threading
import time

import psycopg.errors
import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command, load_command_class
from django.core.management.base import CommandError
from django.db import connection
from django.tasks import task
from pytest_django.fixtures import Settings

from django_absurd.backends import AbsurdBackend, get_absurd_backends
from django_absurd.exceptions import (
    DjangoAbsurdError,
    QueueNotDeclaredError,
    QueueNotProvisionedError,
    SchemaNotInstalledError,
)
from django_absurd.models import Queue
from django_absurd.queues import get_absurd_client
from django_absurd.test import AbsurdTestRuntime
from django_absurd.worker import (
    WorkerOptions,
    aworker_client,
    drain_queue,
    run_blocking_worker,
    run_worker,
)
from tests import atasks, tasks, utils
from tests.jobs import record_from_jobs

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("_isolate_queues"),
]


def backend() -> AbsurdBackend:
    backends = get_absurd_backends()
    return backends["default"]


def test_worker_client_uses_dedicated_connection() -> None:
    call_command("absurd_sync_queues")

    async def _enter() -> bool:
        async with aworker_client(backend(), "default") as client:
            return "default" in await client.list_queues()

    assert asyncio.run(_enter())


@pytest.mark.django_db(databases=["default", "sqlite"], transaction=True)
def test_worker_client_rejects_non_psycopg3(settings: Settings) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "QUEUES": ["default"],
            "OPTIONS": {"DATABASE": "sqlite"},
        }
    }
    with pytest.raises(CommandError, match="psycopg"):
        call_command("absurd_worker", queue="default")


def test_worker_client_opens_without_provisioning_check() -> None:
    # No absurd_sync_queues; 'default' unprovisioned (schema present).
    # aworker_client must NOT raise — the provisioned-or-die check is gone.
    async def _enter() -> list[str]:
        async with aworker_client(backend(), "default") as client:
            return await client.list_queues()

    assert "default" not in asyncio.run(_enter())  # unprovisioned, yet no error


def test_worker_client_absent_schema_errors() -> None:
    async def _enter() -> None:
        # Entering is what raises, so the entry is the whole body — a statement after it
        # would never run.
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(aworker_client(backend(), "default"))

    with (
        utils.hide_absurd_schema(),
        pytest.raises(
            SchemaNotInstalledError,
            match=r"^Absurd schema is not installed\. Run: manage\.py migrate$",
        ),
    ):
        asyncio.run(_enter())


def test_end_to_end_executes_and_records_result(dj_absurd: AbsurdTestRuntime) -> None:
    dj_absurd.sync_queues()
    result = tasks.make_group.enqueue("alpha")
    utils.run_absurd_worker()
    assert Group.objects.filter(name="alpha").exists()
    snap = dj_absurd.get_result(result.id)
    assert snap.state == "completed"
    assert snap.result == "alpha"


def test_failing_task_records_failure(dj_absurd: AbsurdTestRuntime) -> None:
    dj_absurd.sync_queues()
    result = tasks.boom.enqueue()
    utils.run_absurd_worker()
    snap = dj_absurd.get_result(result.id)
    assert snap.state == "failed"


def test_takes_context_attempt_is_one_on_first_run(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    result = tasks.report_attempt.enqueue()
    utils.run_absurd_worker()
    snap = dj_absurd.get_result(result.id)
    assert snap.result == 1


def test_takes_context_task_result_carries_real_args(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    result = tasks.report_args.enqueue("x", "y")
    utils.run_absurd_worker()
    snap = dj_absurd.get_result(result.id)
    assert snap.result == ["x", "y"]


def test_using_queue_name_routes_to_worker_queue() -> None:
    call_command("absurd_sync_queues")
    tasks.routed.using(queue_name="default").enqueue()
    utils.run_absurd_worker()
    assert Group.objects.filter(name="routed").exists()


def test_handler_logs_task_outcome(caplog: pytest.LogCaptureFixture) -> None:
    call_command("absurd_sync_queues")
    tasks.make_group.enqueue("logged")
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        utils.run_absurd_worker()
    assert "tests.tasks.make_group" in caplog.text
    assert "completed" in caplog.text


def test_unregistered_name_defers_not_crashes(dj_absurd: AbsurdTestRuntime) -> None:
    dj_absurd.sync_queues()
    spawn = get_absurd_client("default").spawn(
        "not.a.real.task", {"args": [], "kwargs": {}}, queue="default"
    )
    utils.run_absurd_worker()
    snap = dj_absurd.get_result(spawn["task_id"])
    assert snap.state != "failed"


def test_task_outside_tasks_py_runs(dj_absurd: AbsurdTestRuntime) -> None:
    # record_from_jobs is in tests/jobs.py, NOT tests/tasks.py — the old scan would
    # never find it (it would defer forever). Lazy resolution runs it by module_path.
    dj_absurd.sync_queues()
    result = record_from_jobs.enqueue("from-jobs")
    utils.run_absurd_worker()
    assert Group.objects.filter(name="from-jobs").exists()
    snap = dj_absurd.get_result(result.id)
    assert snap.result == "from-jobs"


def test_queue_defaults_to_default(
    capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "QUEUES": ["default"],
        }
    }
    utils.provision_declared_queues()
    tasks.make_group.enqueue("dflt")
    utils.start_worker_until_done(  # no --queue -> "default"
        lambda: Group.objects.filter(name="dflt").exists()
    )
    out = capsys.readouterr().out
    assert out == (
        "🐘 Started worker on queue 'default'.\n"
        "🐘 Stop requested on queue 'default'; finishing in-flight tasks.\n"
        "🐘 Stopped worker on queue 'default'.\n"
    )
    assert Group.objects.filter(name="dflt").exists()


def test_unknown_queue_errors_listing_valid(settings: Settings) -> None:
    with pytest.raises(CommandError) as exc:
        call_command("absurd_worker", queue="nope")
    message = str(exc.value)
    assert "nope" in message
    assert "Valid queues" in message
    assert "default" in message


def test_worker_rejects_alias_flag(settings: Settings) -> None:
    with pytest.raises(CommandError):
        call_command("absurd_worker", "--alias", "default")


def test_worker_uses_single_backend_at_nondefault_alias(
    capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    settings.TASKS = {
        "myabsurd": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "QUEUES": ["default"],
        }
    }
    utils.provision_declared_queues()
    utils.start_worker()
    assert "Started worker on queue 'default'." in capsys.readouterr().out


def test_worker_no_backend_errors(settings: Settings) -> None:
    settings.TASKS = {
        "default": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}
    }
    with pytest.raises(
        CommandError,
        match=(
            r"No Absurd backend configured\. Add a "
            r"django_absurd\.backends\.AbsurdBackend entry to TASKS\."
        ),
    ):
        call_command("absurd_worker")


def test_worker_multiple_backends_errors(settings: Settings) -> None:
    # absurd.E004 is a system check, not a runtime guard, so a command run with
    # two Absurd backends still reaches resolve_backend's defensive branch.
    settings.TASKS = {
        "a": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "QUEUES": ["default"],
        },
        "b": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "QUEUES": ["default"],
        },
    }
    with pytest.raises(CommandError) as exc:
        call_command("absurd_worker")
    assert str(exc.value) == (
        "django-absurd supports one Absurd backend per project; "
        "configure exactly one AbsurdBackend in TASKS."
    )


def test_command_parses_all_flags_with_defaults() -> None:
    cmd = load_command_class("django_absurd", "absurd_worker")
    parser = cmd.create_parser("manage.py", "absurd_worker")
    opts = vars(parser.parse_args([]))
    assert opts["queue"] == "default"  # --queue defaults to "default"
    assert opts["concurrency"] == 1
    assert opts["claim_timeout"] == 120
    assert opts["poll_interval"] == 0.25
    assert opts["batch_size"] is None
    assert opts["worker_id"] is None


def test_command_runs_task_end_to_end(dj_absurd: AbsurdTestRuntime) -> None:
    dj_absurd.sync_queues()
    result = tasks.make_group.enqueue("via-command")
    utils.start_worker_until_done(
        lambda: Group.objects.filter(name="via-command").exists(), queue="default"
    )
    assert Group.objects.filter(name="via-command").exists()
    snap = dj_absurd.get_result(result.id)
    assert snap.state == "completed"


def test_worker_start_provisions_all_declared_queues(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # full provision on start: every declared queue, not just the served one
    utils.start_worker(queue="default")
    created_line, started_line, stop_requested_line, stopped_line = (
        capsys.readouterr().out.splitlines()
    )
    assert set(created_line.removeprefix("Created: ").split(", ")) == {
        "default",
        "other",
        "reports",
    }
    assert started_line == "🐘 Started worker on queue 'default'."
    assert stop_requested_line == (
        "🐘 Stop requested on queue 'default'; finishing in-flight tasks."
    )
    assert stopped_line == "🐘 Stopped worker on queue 'default'."
    assert Queue.objects.filter(queue_name="default").exists()
    assert Queue.objects.filter(queue_name="other").exists()


def test_worker_command_reconciles_changed_mutable_option(
    capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"QUEUES": {"default": {"cleanup_limit": 100}}},
        }
    }
    call_command("absurd_sync_queues")
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"QUEUES": {"default": {"cleanup_limit": 250}}},
        }
    }
    capsys.readouterr()  # drop sync output
    utils.start_worker(queue="default")
    out = capsys.readouterr().out
    assert out == (
        "Reconciled: default\n"
        "🐘 Started worker on queue 'default'.\n"
        "🐘 Stop requested on queue 'default'; finishing in-flight tasks.\n"
        "🐘 Stopped worker on queue 'default'.\n"
    )
    assert Queue.objects.get(queue_name="default").cleanup_limit == 250  # DB proof


def test_worker_command_reconciles_changed_interval_option(
    capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    # Two mutable opts: cleanup_limit unchanged (loop continues), cleanup_ttl changed
    # (interval drift via parse_interval).
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {
                "QUEUES": {"default": {"cleanup_limit": 100, "cleanup_ttl": "30 days"}}
            },
        }
    }
    call_command("absurd_sync_queues")
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {
                "QUEUES": {"default": {"cleanup_limit": 100, "cleanup_ttl": "60 days"}}
            },
        }
    }
    capsys.readouterr()
    utils.start_worker(queue="default")
    out = capsys.readouterr().out
    assert out == (
        "Reconciled: default\n"
        "🐘 Started worker on queue 'default'.\n"
        "🐘 Stop requested on queue 'default'; finishing in-flight tasks.\n"
        "🐘 Stopped worker on queue 'default'.\n"
    )
    assert Queue.objects.get(queue_name="default").cleanup_ttl == dt.timedelta(days=60)


def test_worker_command_no_reconcile_when_unchanged(
    capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"QUEUES": {"default": {"cleanup_ttl": "30 days"}}},
        }
    }
    call_command("absurd_sync_queues")
    before = Queue.objects.get(queue_name="default").cleanup_ttl
    capsys.readouterr()
    utils.start_worker(queue="default")
    out = capsys.readouterr().out
    # Drift-gated no-op: no Created/Reconciled, no "No queues to sync.", just
    # the start and stop lines.
    assert out == (
        "🐘 Started worker on queue 'default'.\n"
        "🐘 Stop requested on queue 'default'; finishing in-flight tasks.\n"
        "🐘 Stopped worker on queue 'default'.\n"
    )
    assert Queue.objects.get(queue_name="default").cleanup_ttl == before


def test_worker_command_warns_on_storage_mode_drift(
    capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"QUEUES": {"default": {}}},
        }
    }
    call_command("absurd_sync_queues")  # create 'default' unpartitioned
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"QUEUES": {"default": {"storage_mode": "partitioned"}}},
        }
    }
    capsys.readouterr()
    utils.start_worker(queue="default")
    cap = capsys.readouterr()
    assert cap.out == (
        "🐘 Started worker on queue 'default'.\n"
        "🐘 Stop requested on queue 'default'; finishing in-flight tasks.\n"
        "🐘 Stopped worker on queue 'default'.\n"
    )
    # The command's own warning shares stderr with the console handler the worker
    # attaches, whose StreamHandler defaults to stderr as Django's own console
    # handler does.
    assert cap.err == (
        "queues provisioned: no changes\n"
        "Queue 'default': storage_mode cannot be changed "
        "(existing: 'unpartitioned', declared: 'partitioned'); skipping.\n"
        'worker started: alias="default" queue="default" database="default"'
        " concurrency=1\n"
        "worker stop requested: finishing in-flight tasks\n"
        'worker stopped: alias="default" queue="default" database="default"\n'
    )


def test_worker_command_schema_absent_errors_migrate() -> None:
    # The provision_backend error translation errors before ever
    # reaching the blocking worker loop. Driven through the live-worker helper: a
    # command that fails this early installs no signal handler, so the stop signal
    # that helper exists to send must never go out — pytest installs no SIGTERM
    # handler of its own, so a stray kill would hit Python's default (SIG_DFL) and
    # take the session down with it.
    with utils.hide_absurd_schema(), pytest.raises(CommandError) as excinfo:
        utils.start_worker(queue="default")
    assert (
        str(excinfo.value) == "Absurd schema is not installed. Run: manage.py migrate"
    )


def test_start_worker_drains_concurrently() -> None:
    call_command("absurd_sync_queues")
    for i in range(5):
        tasks.make_group.enqueue(f"g{i}")

    utils.run_absurd_worker()
    assert Group.objects.filter(name__startswith="g").count() == 5


def test_async_task_runs_end_to_end(dj_absurd: AbsurdTestRuntime) -> None:
    dj_absurd.sync_queues()
    r = atasks.aecho.enqueue("hi-async")
    utils.run_absurd_worker()
    snap = dj_absurd.get_result(r.id)
    assert snap.state == "completed"
    assert snap.result == "hi-async"


def test_blocking_worker_drains_then_stops() -> None:
    # Exercises the blocking (live-worker) path deterministically — no sleeps:
    # the stopper awaits each task to a terminal state (SDK await_task_result),
    # THEN sets the stop event the worker loop polls.
    # run_blocking_worker returns once stopped.
    call_command("absurd_sync_queues")
    results = [tasks.make_group.enqueue(f"blk-{i}") for i in range(3)]
    task_ids = [r.id.rsplit(":", 1)[-1] for r in results]

    async def drive() -> None:
        stop = asyncio.Event()
        async with aworker_client(backend(), "default") as client:

            async def stopper() -> None:
                for tid in task_ids:
                    await client.await_task_result(tid)
                stop.set()

            await asyncio.gather(
                run_blocking_worker(client, WorkerOptions(concurrency=2), stop=stop),
                stopper(),
            )

    asyncio.run(drive())
    assert Group.objects.filter(name__startswith="blk-").count() == 3


@pytest.mark.parametrize(
    ("entrypoint", "expected_error"),
    [("command", CommandError), ("function", QueueNotDeclaredError)],
)
def test_undeclared_queue_is_rejected(
    entrypoint: str, expected_error: type[Exception]
) -> None:
    # Same rule, two enforcing entrypoints, two vocabularies: the management command
    # raises CommandError, drain_queue raises the package's own QueueNotDeclaredError.
    def invoke() -> None:
        if entrypoint == "command":
            call_command("absurd_worker", queue="nope")
        else:
            drain_queue("nope")

    with pytest.raises(expected_error) as exc:
        invoke()
    message = str(exc.value)
    expected = (
        "Queue 'nope' is not declared for backend 'default'. "
        "Valid queues: default, other, reports. "
        "Add it to the QUEUES list in your TASKS backend settings."
    )
    assert message == expected


def test_undeclared_queue_error_is_also_a_django_absurd_error() -> None:
    # QueueNotDeclaredError is a DjangoAbsurdError: the base catches it too, pinning
    # the base's purpose (a caller can catch the package's typed errors generically).
    with pytest.raises(DjangoAbsurdError):
        drain_queue("nope")


def test_drain_queue_on_an_unprovisioned_queue_errors_sync_queues() -> None:
    # Declared but never provisioned (no absurd_sync_queues, and _isolate_queues
    # dropped every queue's tables): drain_queue does not provision, so the missing
    # table surfaces as the curated error naming the command that fixes it. No
    # enqueue() here — that one auto-creates the queue it writes to.
    with pytest.raises(QueueNotProvisionedError) as exc:
        drain_queue("default")

    assert str(exc.value) == (
        "Queue 'default' is declared but its Absurd table is not provisioned. "
        "Run: manage.py absurd_sync_queues"
    )


def test_drain_queue_does_not_relabel_an_unrelated_missing_relation() -> None:
    """A missing relation that is NOT one of this queue's own Absurd tables surfaces as
    itself. Relabeling it "run absurd_sync_queues" would send the reader to the wrong
    door, and dropping the cause would hide which relation is actually missing.

    Driven the way it happens in production: an audit trigger on a queue table whose
    target relation is gone. A plpgsql body carries no dependency on the tables it
    names, so nothing blocks the drop and the failure lands when the trigger next fires
    — from inside the claim, i.e. exactly where the curated error used to swallow it.
    """
    call_command("absurd_sync_queues")
    tasks.add.enqueue(2, 3)
    try:
        with connection.cursor() as cur:
            cur.execute(
                "create or replace function absurd.record_audit_probe() "
                "returns trigger language plpgsql as $$ begin "
                "insert into absurd.audit_probe (run_id) values (new.run_id); "
                "return new; end; $$"
            )
            cur.execute(
                "create trigger audit_probe_after_claim after update "
                "on absurd.r_default for each row "
                "execute function absurd.record_audit_probe()"
            )

        with pytest.raises(psycopg.errors.UndefinedTable) as undefined:
            drain_queue("default")

        assert (
            undefined.value.diag.message_primary
            == 'relation "absurd.audit_probe" does not exist'
        )
    finally:
        # cascade takes the trigger with it; `if exists` keeps the cleanup honest even
        # if the trigger DDL above is what failed.
        with connection.cursor() as cur:
            cur.execute("drop function if exists absurd.record_audit_probe() cascade")


def test_non_task_name_defers_not_crashes(dj_absurd: AbsurdTestRuntime) -> None:
    # A name that IMPORTS but is not a Task (asleep is the asyncio.sleep alias
    # in atasks) -> LazyTaskRegistry resolves it, sees it's not a Task, defers
    # (state not failed).
    dj_absurd.sync_queues()
    spawn = get_absurd_client("default").spawn(
        "tests.atasks.asleep", {"args": [], "kwargs": {}}, queue="default"
    )
    utils.run_absurd_worker()
    snap = dj_absurd.get_result(spawn["task_id"])
    assert snap.state != "failed"


STOP_REQUESTED_LOG = "worker stop requested: finishing in-flight tasks"

TASK_STARTED: dict[str, threading.Event] = {}
RELEASE_GATE: dict[str, threading.Event] = {}


@task(queue_name="default")
async def wait_for_release(name: str) -> None:
    """Announce it started, then park on a `threading.Event` a signal-sending OS
    thread can set — an `asyncio.Event` would not be thread-safe to set from there."""
    TASK_STARTED[name].set()
    await asyncio.to_thread(RELEASE_GATE[name].wait)


def test_worker_command_reports_the_stop_request_on_both_channels(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        utils.start_worker(queue="default")

    assert capsys.readouterr().out == (
        "🐘 Started worker on queue 'default'.\n"
        "🐘 Stop requested on queue 'default'; finishing in-flight tasks.\n"
        "🐘 Stopped worker on queue 'default'.\n"
    )
    messages = [
        r.getMessage() for r in caplog.records if r.name == "django_absurd.worker"
    ]
    assert messages == [
        (
            'worker started: alias="default" queue="default" database="default"'
            " concurrency=1"
        ),
        STOP_REQUESTED_LOG,
        'worker stopped: alias="default" queue="default" database="default"',
    ]


def test_worker_command_logs_the_stop_request_again_on_a_second_signal(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    dj_absurd: AbsurdTestRuntime,
) -> None:
    # A held task keeps the worker mid-shutdown (looping never re-checked, handler
    # still installed) long enough to deliver a SECOND signal deterministically —
    # gated on the first stop-requested log line landing, never on a sleep — and
    # proves the operator's second Ctrl-C gets answered too, not swallowed.
    dj_absurd.sync_queues()
    TASK_STARTED["repeat"] = threading.Event()
    RELEASE_GATE["repeat"] = threading.Event()
    wait_for_release.enqueue("repeat")
    previous_handler = signal.getsignal(signal.SIGTERM)

    def count_stop_requested() -> int:
        return sum(
            1
            for r in caplog.records
            if r.name == "django_absurd.worker" and r.getMessage() == STOP_REQUESTED_LOG
        )

    def deliver_two_signals_then_release() -> None:
        assert TASK_STARTED["repeat"].wait(5)
        if utils.stop_handler_is_installed(previous_handler):  # pragma: no branch
            os.kill(os.getpid(), signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:  # pragma: no branch
            if count_stop_requested() >= 1:
                break
            time.sleep(0.005)
        if utils.stop_handler_is_installed(previous_handler):  # pragma: no branch
            os.kill(os.getpid(), signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:  # pragma: no branch
            if count_stop_requested() >= 2:
                break
            time.sleep(0.005)
        RELEASE_GATE["repeat"].set()

    killer = threading.Thread(target=deliver_two_signals_then_release, daemon=True)
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        killer.start()
        try:
            call_command("absurd_worker", poll_interval=0.05, queue="default")
        finally:
            killer.join(timeout=5)
            signal.signal(signal.SIGTERM, previous_handler)

    assert count_stop_requested() == 2
    assert capsys.readouterr().out == (
        "🐘 Started worker on queue 'default'.\n"
        "🐘 Stop requested on queue 'default'; finishing in-flight tasks.\n"
        "🐘 Stop requested on queue 'default'; finishing in-flight tasks.\n"
        "🐘 Stopped worker on queue 'default'.\n"
    )


def test_run_worker_without_on_stop_requested_writes_nothing_to_stdout(
    capsys: pytest.CaptureFixture[str], dj_absurd: AbsurdTestRuntime
) -> None:
    # run_worker (the library entrypoint, not the command) is the "default stays
    # silent" case: nothing under django_absurd/ writes to stdout on its own, so a
    # real stop signal through it must produce no output even though the log line
    # still fires.
    dj_absurd.sync_queues()
    previous_handler = signal.getsignal(signal.SIGTERM)

    def fire_sigterm_once_installed() -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:  # pragma: no branch
            if utils.stop_handler_is_installed(previous_handler):
                os.kill(os.getpid(), signal.SIGTERM)
                break
            time.sleep(0.005)

    killer = threading.Thread(target=fire_sigterm_once_installed, daemon=True)
    try:
        killer.start()
        run_worker(backend(), "default", options=WorkerOptions(poll_interval=0.05))
    finally:
        killer.join(timeout=5)
        signal.signal(signal.SIGTERM, previous_handler)

    assert capsys.readouterr().out == ""
