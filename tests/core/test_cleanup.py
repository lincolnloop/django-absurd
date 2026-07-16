import re

import pytest
from django.core.management import call_command

from django_absurd.cleanup import cleanup_all_queues
from tests.tasks import add, cleanup_queues

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


def sync_queue(settings, cleanup_ttl="0 seconds", cleanup_limit=1000):
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "QUEUES": {
                    "default": {
                        "cleanup_ttl": cleanup_ttl,
                        "cleanup_limit": cleanup_limit,
                    }
                }
            },
        }
    }
    call_command("absurd_sync_queues")


def drain(queue="default"):
    call_command("absurd_worker", queue=queue, burst=True)


@pytest.fixture(params=["command", "direct"])
def cleanup(request, capsys):
    """Run cleanup through both entrypoints (management command + direct call), each
    normalized to the per-queue count dicts, so behavioral tests cover both. The command
    path parses its stdout back into dicts."""

    def run():
        if request.param == "direct":
            return cleanup_all_queues()
        capsys.readouterr()  # discard any prior output
        call_command("absurd_cleanup")
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


def test_wrapper_task_result_is_deleted_counts(settings):
    sync_queue(settings)
    add.enqueue(2, 3)
    drain()  # one completed task now eligible
    result = cleanup_queues.enqueue()
    drain()
    got = cleanup_queues.get_result(result.id)
    assert got.return_value == [
        {"queue_name": "default", "tasks_deleted": 1, "events_deleted": 0}
    ]
