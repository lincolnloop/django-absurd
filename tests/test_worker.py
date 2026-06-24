import asyncio
import logging

import psycopg
import pytest
from absurd_sdk import Absurd
from django.contrib.auth.models import Group
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command, load_command_class
from django.core.management.base import CommandError
from django.db import connection, connections

from django_absurd.connection import register_jsonb_loader
from django_absurd.queues import get_absurd_backends, get_absurd_client
from django_absurd.worker import aworker_client
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


def test_worker_client_unprovisioned_queue_errors(settings):
    call_command("absurd_sync_queues")
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "QUEUES": ["default", "unsynced"],
            "OPTIONS": {"DATABASE": "default"},
        }
    }

    async def _enter():
        async with aworker_client(backend(), "unsynced"):
            pass

    with pytest.raises(ImproperlyConfigured) as exc:
        asyncio.run(_enter())
    message = str(exc.value)
    assert "unsynced" in message
    assert "absurd_sync_queues" in message


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


def test_queue_is_required():
    with pytest.raises(CommandError):
        call_command("absurd_worker")


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
    opts = vars(parser.parse_args(["--queue", "default"]))
    assert opts["queue"] == "default"
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


def test_command_maps_improperly_configured_to_commanderror(settings):
    call_command("absurd_sync_queues")
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "QUEUES": ["default", "unsynced"],
            "OPTIONS": {"DATABASE": "default"},
        }
    }
    with pytest.raises(CommandError) as exc:
        call_command("absurd_worker", queue="unsynced", burst=True)
    assert "unsynced" in str(exc.value)


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
