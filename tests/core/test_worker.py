import asyncio
import datetime as dt
import logging

import psycopg.errors
import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command, load_command_class
from django.core.management.base import CommandError
from django.db import connection
from pytest_django.fixtures import SettingsWrapper

from django_absurd.backends import AbsurdBackend, get_absurd_backends
from django_absurd.exceptions import (
    DjangoAbsurdError,
    QueueNotDeclaredError,
    QueueNotProvisionedError,
)
from django_absurd.models import Queue
from django_absurd.queues import get_absurd_client
from django_absurd.test import AbsurdTestRuntime
from django_absurd.worker import (
    WorkerOptions,
    aworker_client,
    drain_queue,
    run_blocking_worker,
)
from tests import atasks, tasks
from tests.jobs import record_from_jobs
from tests.utils import run_absurd_worker

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
def test_worker_client_rejects_non_psycopg3(settings: SettingsWrapper) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "QUEUES": ["default"],
            "OPTIONS": {"DATABASE": "sqlite"},
        }
    }
    with pytest.raises(CommandError, match="psycopg"):
        call_command("absurd_worker", queue="default", burst=True)


def test_worker_client_opens_without_provisioning_check() -> None:
    # No absurd_sync_queues; 'default' unprovisioned (schema present).
    # aworker_client must NOT raise — the provisioned-or-die check is gone.
    async def _enter() -> list[str]:
        async with aworker_client(backend(), "default") as client:
            return await client.list_queues()

    assert "default" not in asyncio.run(_enter())  # unprovisioned, yet no error


def test_worker_client_absent_schema_errors() -> None:
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS absurd CASCADE")
    try:

        async def _enter() -> None:
            async with aworker_client(backend(), "default"):
                pass

        with pytest.raises(ImproperlyConfigured, match="migrate"):
            asyncio.run(_enter())
    finally:
        call_command("migrate", "django_absurd", "zero", verbosity=0)
        call_command("migrate", verbosity=0)  # restore absurd schema


def test_end_to_end_executes_and_records_result(dj_absurd: AbsurdTestRuntime) -> None:
    dj_absurd.sync_queues()
    result = tasks.make_group.enqueue("alpha")
    run_absurd_worker()
    assert Group.objects.filter(name="alpha").exists()
    snap = dj_absurd.get_result(result.id)
    assert snap.state == "completed"
    assert snap.result == "alpha"


def test_failing_task_records_failure(dj_absurd: AbsurdTestRuntime) -> None:
    dj_absurd.sync_queues()
    result = tasks.boom.enqueue()
    run_absurd_worker()
    snap = dj_absurd.get_result(result.id)
    assert snap.state == "failed"


def test_takes_context_attempt_is_one_on_first_run(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    result = tasks.report_attempt.enqueue()
    run_absurd_worker()
    snap = dj_absurd.get_result(result.id)
    assert snap.result == 1


def test_takes_context_task_result_carries_real_args(
    dj_absurd: AbsurdTestRuntime,
) -> None:
    dj_absurd.sync_queues()
    result = tasks.report_args.enqueue("x", "y")
    run_absurd_worker()
    snap = dj_absurd.get_result(result.id)
    assert snap.result == ["x", "y"]


def test_using_queue_name_routes_to_worker_queue() -> None:
    call_command("absurd_sync_queues")
    tasks.routed.using(queue_name="default").enqueue()
    run_absurd_worker()
    assert Group.objects.filter(name="routed").exists()


def test_handler_logs_task_outcome(caplog: pytest.LogCaptureFixture) -> None:
    call_command("absurd_sync_queues")
    tasks.make_group.enqueue("logged")
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        run_absurd_worker()
    assert "tests.tasks.make_group" in caplog.text
    assert "completed" in caplog.text


def test_unregistered_name_defers_not_crashes(dj_absurd: AbsurdTestRuntime) -> None:
    dj_absurd.sync_queues()
    spawn = get_absurd_client("default").spawn(
        "not.a.real.task", {"args": [], "kwargs": {}}, queue="default"
    )
    run_absurd_worker()
    snap = dj_absurd.get_result(spawn["task_id"])
    assert snap.state != "failed"


def test_task_outside_tasks_py_runs(dj_absurd: AbsurdTestRuntime) -> None:
    # record_from_jobs is in tests/jobs.py, NOT tests/tasks.py — the old scan would
    # never find it (it would defer forever). Lazy resolution runs it by module_path.
    dj_absurd.sync_queues()
    result = record_from_jobs.enqueue("from-jobs")
    run_absurd_worker()
    assert Group.objects.filter(name="from-jobs").exists()
    snap = dj_absurd.get_result(result.id)
    assert snap.result == "from-jobs"


def test_queue_defaults_to_default(
    capsys: pytest.CaptureFixture[str], settings: SettingsWrapper
) -> None:
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "QUEUES": ["default"],
        }
    }
    tasks.make_group.enqueue("dflt")  # auto-creates the default queue
    call_command("absurd_worker", burst=True)  # no --queue -> "default"
    out = capsys.readouterr().out
    assert out == "Started worker on queue 'default'.\n"
    assert Group.objects.filter(name="dflt").exists()


def test_unknown_queue_errors_listing_valid(settings: SettingsWrapper) -> None:
    with pytest.raises(CommandError) as exc:
        call_command("absurd_worker", queue="nope")
    message = str(exc.value)
    assert "nope" in message
    assert "Valid queues" in message
    assert "default" in message


def test_worker_rejects_alias_flag(settings: SettingsWrapper) -> None:
    with pytest.raises(CommandError):
        call_command("absurd_worker", "--alias", "default", burst=True)


def test_worker_uses_single_backend_at_nondefault_alias(
    capsys: pytest.CaptureFixture[str], settings: SettingsWrapper
) -> None:
    settings.TASKS = {
        "myabsurd": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "QUEUES": ["default"],
        }
    }
    call_command("absurd_worker", burst=True)
    assert "Started worker on queue 'default'." in capsys.readouterr().out


def test_worker_no_backend_errors(settings: SettingsWrapper) -> None:
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
        call_command("absurd_worker", burst=True)


def test_worker_multiple_backends_errors(settings: SettingsWrapper) -> None:
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
        call_command("absurd_worker", burst=True)
    assert str(exc.value) == (
        "django-absurd supports one Absurd backend per project; "
        "configure exactly one AbsurdBackend in TASKS."
    )


def test_command_parses_all_flags_with_defaults() -> None:
    cmd = load_command_class("django_absurd", "absurd_worker")
    parser = cmd.create_parser("manage.py", "absurd_worker")
    opts = vars(parser.parse_args([]))
    assert opts["queue"] == "default"  # --queue defaults to "default"
    assert opts["burst"] is False
    assert opts["concurrency"] == 1
    assert opts["claim_timeout"] == 120
    assert opts["poll_interval"] == 0.25
    assert opts["batch_size"] is None
    assert opts["worker_id"] is None


def test_command_burst_runs_task_end_to_end(dj_absurd: AbsurdTestRuntime) -> None:
    dj_absurd.sync_queues()
    result = tasks.make_group.enqueue("via-command")
    call_command("absurd_worker", queue="default", burst=True)
    assert Group.objects.filter(name="via-command").exists()
    snap = dj_absurd.get_result(result.id)
    assert snap.state == "completed"


def test_worker_start_provisions_all_declared_queues(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # full provision on start: every declared queue, not just the served one
    call_command("absurd_worker", queue="default", burst=True)
    created_line, started_line = capsys.readouterr().out.splitlines()
    assert set(created_line.removeprefix("Created: ").split(", ")) == {
        "default",
        "other",
        "reports",
    }
    assert started_line == "Started worker on queue 'default'."
    assert Queue.objects.filter(queue_name="default").exists()
    assert Queue.objects.filter(queue_name="other").exists()


def test_worker_command_reconciles_changed_mutable_option(
    capsys: pytest.CaptureFixture[str], settings: SettingsWrapper
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
    call_command("absurd_worker", queue="default", burst=True)
    out = capsys.readouterr().out
    assert out == "Reconciled: default\nStarted worker on queue 'default'.\n"
    assert Queue.objects.get(queue_name="default").cleanup_limit == 250  # DB proof


def test_worker_command_reconciles_changed_interval_option(
    capsys: pytest.CaptureFixture[str], settings: SettingsWrapper
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
    call_command("absurd_worker", queue="default", burst=True)
    out = capsys.readouterr().out
    assert out == "Reconciled: default\nStarted worker on queue 'default'.\n"
    assert Queue.objects.get(queue_name="default").cleanup_ttl == dt.timedelta(days=60)


def test_worker_command_no_reconcile_when_unchanged(
    capsys: pytest.CaptureFixture[str], settings: SettingsWrapper
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
    call_command("absurd_worker", queue="default", burst=True)
    out = capsys.readouterr().out
    # Drift-gated no-op: no Created/Reconciled, no "No queues to sync.", just
    # the start line.
    assert out == "Started worker on queue 'default'.\n"
    assert Queue.objects.get(queue_name="default").cleanup_ttl == before


def test_worker_command_warns_on_storage_mode_drift(
    capsys: pytest.CaptureFixture[str], settings: SettingsWrapper
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
    call_command("absurd_worker", queue="default", burst=True)
    cap = capsys.readouterr()
    assert cap.out == "Started worker on queue 'default'.\n"
    assert cap.err == (
        "Queue 'default': storage_mode cannot be changed "
        "(existing: 'unpartitioned', declared: 'partitioned'); skipping.\n"
    )


def test_worker_command_schema_absent_errors_migrate() -> None:
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS absurd CASCADE")
    try:
        with pytest.raises(CommandError, match="migrate"):
            call_command("absurd_worker", queue="default", burst=True)
    finally:
        call_command("migrate", "django_absurd", "zero", verbosity=0)
        call_command("migrate", verbosity=0)  # restore absurd schema


def test_worker_non_burst_command_schema_absent_errors_migrate() -> None:
    # The provision_backend/ImproperlyConfigured translation errors before ever
    # reaching the blocking worker loop.
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS absurd CASCADE")
    try:
        with pytest.raises(CommandError, match="migrate"):
            call_command("absurd_worker", queue="default")
    finally:
        call_command("migrate", "django_absurd", "zero", verbosity=0)
        call_command("migrate", verbosity=0)  # restore absurd schema


def test_start_worker_drains_concurrently() -> None:
    call_command("absurd_sync_queues")
    for i in range(5):
        tasks.make_group.enqueue(f"g{i}")

    run_absurd_worker()
    assert Group.objects.filter(name__startswith="g").count() == 5


def test_async_task_runs_end_to_end(dj_absurd: AbsurdTestRuntime) -> None:
    dj_absurd.sync_queues()
    r = atasks.aecho.enqueue("hi-async")
    run_absurd_worker()
    snap = dj_absurd.get_result(r.id)
    assert snap.state == "completed"
    assert snap.result == "hi-async"


def test_blocking_worker_drains_then_stops() -> None:
    # Exercises the blocking (live-worker) path deterministically — no sleeps:
    # the stopper awaits each task to a terminal state (SDK await_task_result),
    # THEN calls stop_worker() (the flag start_worker's loop polls).
    # run_blocking_worker returns once stopped.
    call_command("absurd_sync_queues")
    results = [tasks.make_group.enqueue(f"blk-{i}") for i in range(3)]
    task_ids = [r.id.rsplit(":", 1)[-1] for r in results]

    async def drive() -> None:
        async with aworker_client(backend(), "default") as client:

            async def stopper() -> None:
                for tid in task_ids:
                    await client.await_task_result(tid)
                client.stop_worker()

            await asyncio.gather(
                run_blocking_worker(client, WorkerOptions(concurrency=2)),
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
            call_command("absurd_worker", queue="nope", burst=True)
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
    run_absurd_worker()
    snap = dj_absurd.get_result(spawn["task_id"])
    assert snap.state != "failed"
