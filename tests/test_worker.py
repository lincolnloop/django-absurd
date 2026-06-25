import asyncio
import logging
from datetime import timedelta

import psycopg
import pytest
from absurd_sdk import Absurd
from django.contrib.auth.models import Group
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command, load_command_class
from django.core.management.base import CommandError
from django.db import connection, connections

from django_absurd.backends import get_absurd_backends
from django_absurd.connection import register_jsonb_loader
from django_absurd.models import Queue
from django_absurd.queues import get_absurd_client
from django_absurd.worker import WorkerOptions, aworker_client, run_blocking_worker
from tests.atasks import aecho
from tests.jobs import record_from_jobs
from tests.tasks import boom, make_group, report_args, report_attempt, routed

pytestmark = pytest.mark.django_db(transaction=True)


def backend():
    return get_absurd_backends()["default"]


def run_absurd_worker(queue="default", concurrency=1):
    """Run the absurd_worker management command in burst mode (drain then exit)."""
    call_command("absurd_worker", queue=queue, burst=True, concurrency=concurrency)


def get_task_result(task_id, queue="default"):
    raw_task_id = str(task_id).rsplit(":", 1)[-1]
    params = connections["default"].get_connection_params()
    conn = psycopg.connect(**params, autocommit=True)
    try:
        register_jsonb_loader(conn)
        return Absurd(conn).fetch_task_result(raw_task_id, queue)
    finally:
        conn.close()


def test_worker_client_uses_dedicated_connection():
    call_command("absurd_sync_queues")

    async def _enter():
        async with aworker_client(backend(), "default") as client:
            return "default" in await client.list_queues()

    assert asyncio.run(_enter())


@pytest.mark.django_db(databases=["default", "sqlite"], transaction=True)
def test_worker_client_rejects_non_psycopg3(settings):
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "QUEUES": ["default"],
            "OPTIONS": {"DATABASE": "sqlite"},
        }
    }
    with pytest.raises(CommandError, match="psycopg"):
        call_command("absurd_worker", queue="default", burst=True)


def test_worker_client_opens_without_provisioning_check():
    # No absurd_sync_queues; 'default' unprovisioned (schema present). aworker_client must
    # NOT raise — the provisioned-or-die check is gone.
    async def _enter():
        async with aworker_client(backend(), "default") as client:
            return await client.list_queues()

    assert "default" not in asyncio.run(_enter())  # unprovisioned, yet no error


def test_worker_client_absent_schema_errors():
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS absurd CASCADE")
    try:

        async def _enter():
            async with aworker_client(backend(), "default"):
                pass

        with pytest.raises(ImproperlyConfigured, match="migrate"):
            asyncio.run(_enter())
    finally:
        call_command("migrate", "django_absurd", "zero", verbosity=0)
        call_command("migrate", "django_absurd", verbosity=0)


def test_end_to_end_executes_and_records_result():
    call_command("absurd_sync_queues")
    result = make_group.enqueue("alpha")
    run_absurd_worker()
    assert Group.objects.filter(name="alpha").exists()
    snap = get_task_result(result.id)
    assert snap.state == "completed"
    assert snap.result == "alpha"


def test_failing_task_records_failure():
    call_command("absurd_sync_queues")
    result = boom.enqueue()
    run_absurd_worker()
    assert get_task_result(result.id).state == "failed"


def test_takes_context_attempt_is_one_on_first_run():
    call_command("absurd_sync_queues")
    result = report_attempt.enqueue()
    run_absurd_worker()
    assert get_task_result(result.id).result == 1


def test_takes_context_task_result_carries_real_args():
    call_command("absurd_sync_queues")
    result = report_args.enqueue("x", "y")
    run_absurd_worker()
    assert get_task_result(result.id).result == ["x", "y"]


def test_using_queue_name_routes_to_worker_queue():
    call_command("absurd_sync_queues")
    routed.using(queue_name="default").enqueue()
    run_absurd_worker()
    assert Group.objects.filter(name="routed").exists()


def test_handler_logs_task_outcome(caplog):
    call_command("absurd_sync_queues")
    make_group.enqueue("logged")
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        run_absurd_worker()
    assert "tests.tasks.make_group" in caplog.text
    assert "completed" in caplog.text


def test_unregistered_name_defers_not_crashes():
    call_command("absurd_sync_queues")
    spawn = get_absurd_client("default").spawn(
        "not.a.real.task", {"args": [], "kwargs": {}}, queue="default"
    )
    run_absurd_worker()
    assert get_task_result(spawn["task_id"]).state != "failed"


def test_task_outside_tasks_py_runs():
    # record_from_jobs is in tests/jobs.py, NOT tests/tasks.py — the old scan would
    # never find it (it would defer forever). Lazy resolution runs it by module_path.
    call_command("absurd_sync_queues")
    result = record_from_jobs.enqueue("from-jobs")
    run_absurd_worker()
    assert Group.objects.filter(name="from-jobs").exists()
    assert get_task_result(result.id).result == "from-jobs"


def test_queue_defaults_to_default(capsys):
    make_group.enqueue("dflt")  # auto-creates the default queue
    call_command("absurd_worker", burst=True)  # no --queue -> "default"
    out = capsys.readouterr().out
    assert out == "Started worker on queue 'default'.\n"
    assert Group.objects.filter(name="dflt").exists()


def test_unknown_queue_errors_listing_valid(settings):
    with pytest.raises(CommandError) as exc:
        call_command("absurd_worker", queue="nope")
    message = str(exc.value)
    assert "nope" in message
    assert "Valid queues" in message
    assert "default" in message


def test_ambiguous_alias_requires_flag(settings):
    settings.TASKS = {
        "a": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "QUEUES": ["default"],
            "OPTIONS": {"DATABASE": "default"},
        },
        "b": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "QUEUES": ["default"],
            "OPTIONS": {"DATABASE": "default"},
        },
    }
    with pytest.raises(CommandError) as exc:
        call_command("absurd_worker", queue="default")
    message = str(exc.value)
    assert "a" in message
    assert "b" in message


def test_command_parses_all_flags_with_defaults():
    cmd = load_command_class("django_absurd", "absurd_worker")
    parser = cmd.create_parser("manage.py", "absurd_worker")
    opts = vars(parser.parse_args([]))
    assert opts["queue"] == "default"  # --queue defaults to "default"
    assert opts["alias"] is None
    assert opts["burst"] is False
    assert opts["concurrency"] == 1
    assert opts["claim_timeout"] == 120
    assert opts["poll_interval"] == 0.25
    assert opts["batch_size"] is None
    assert opts["worker_id"] is None


def test_command_burst_runs_task_end_to_end():
    call_command("absurd_sync_queues")
    result = make_group.enqueue("via-command")
    call_command("absurd_worker", queue="default", burst=True)
    assert Group.objects.filter(name="via-command").exists()
    assert get_task_result(result.id).state == "completed"


def test_worker_command_reports_created_on_unprovisioned_queue(capsys):
    call_command("absurd_worker", queue="default", burst=True)
    out = capsys.readouterr().out
    assert out == "Created: default\nStarted worker on queue 'default'.\n"
    assert Queue.objects.filter(queue_name="default").exists()


def test_worker_command_reconciles_changed_mutable_option(settings, capsys):
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


def test_worker_command_reconciles_changed_interval_option(settings, capsys):
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
    assert Queue.objects.get(queue_name="default").cleanup_ttl == timedelta(days=60)


def test_worker_command_no_reconcile_when_unchanged(settings, capsys):
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
    # Drift-gated no-op: no Created/Reconciled, no "No queues to sync.", just the start line.
    assert out == "Started worker on queue 'default'.\n"
    assert Queue.objects.get(queue_name="default").cleanup_ttl == before


def test_worker_command_warns_on_storage_mode_drift(settings, capsys):
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


def test_worker_command_schema_absent_errors_migrate():
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS absurd CASCADE")
    try:
        with pytest.raises(CommandError, match="migrate"):
            call_command("absurd_worker", queue="default", burst=True)
    finally:
        call_command("migrate", "django_absurd", "zero", verbosity=0)
        call_command("migrate", "django_absurd", verbosity=0)


def test_start_worker_drains_concurrently():
    call_command("absurd_sync_queues")
    for i in range(5):
        make_group.enqueue(f"g{i}")

    run_absurd_worker()
    assert Group.objects.filter(name__startswith="g").count() == 5


def test_async_task_runs_end_to_end():
    call_command("absurd_sync_queues")
    r = aecho.enqueue("hi-async")
    run_absurd_worker()
    snap = get_task_result(r.id)
    assert snap.state == "completed"
    assert snap.result == "hi-async"


def test_blocking_worker_drains_then_stops():
    # Exercises the blocking (live-worker) path deterministically — no sleeps: the stopper
    # awaits each task to a terminal state (SDK await_task_result), THEN calls stop_worker()
    # (the flag start_worker's loop polls). run_blocking_worker returns once stopped.
    call_command("absurd_sync_queues")
    results = [make_group.enqueue(f"blk-{i}") for i in range(3)]
    task_ids = [r.id.rsplit(":", 1)[-1] for r in results]

    async def drive():
        async with aworker_client(backend(), "default") as client:

            async def stopper():
                for tid in task_ids:
                    await client.await_task_result(tid)
                client.stop_worker()

            await asyncio.gather(
                run_blocking_worker(client, WorkerOptions(concurrency=2)),
                stopper(),
            )

    asyncio.run(drive())
    assert Group.objects.filter(name__startswith="blk-").count() == 3


def test_non_task_name_defers_not_crashes():
    # A name that IMPORTS but is not a Task (asleep is the asyncio.sleep alias in atasks)
    # -> LazyTaskRegistry resolves it, sees it's not a Task, defers (state not failed).
    call_command("absurd_sync_queues")
    spawn = get_absurd_client("default").spawn(
        "tests.atasks.asleep", {"args": [], "kwargs": {}}, queue="default"
    )
    run_absurd_worker()
    assert get_task_result(spawn["task_id"]).state != "failed"
