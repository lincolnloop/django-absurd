"""The harness's process entry points — everything that starts a Python process here.

Nothing at module level READS a Django setting or touches a model, and that is
load-bearing for ``run_drain_worker``: ``load_drain`` spawns its workers (never forks),
so a child imports this module in a fresh interpreter that has not been configured yet.
Importing ``django_absurd`` here is fine — it only defines — but nothing may call into
it before the ``django.setup()`` inside each entry point.
"""

import asyncio
import io
import os
import sys

import django
from django.core.management import call_command, execute_from_command_line

from django_absurd.management.base import resolve_backend
from django_absurd.worker import WorkerOptions, arun_drain

SETTINGS_MODULE = "loadtest.settings"


def run_management_command() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", SETTINGS_MODULE)
    execute_from_command_line(sys.argv)


def run_drain_worker(queue: str, concurrency: int, environ: dict[str, str]) -> None:
    """Drain ``queue`` in this process and return — one worker of a ``load_drain`` cell.

    ``environ`` names the database the parent is really using, which under pytest is
    the test database rather than ``loadtest.settings``'s default; it is applied before
    Django starts, so the child cannot resolve a setting from anything else.
    """
    os.environ.update(environ)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", SETTINGS_MODULE)
    django.setup()
    # arun_drain rather than drain_queue: a cell's whole point is a stated
    # concurrency, and drain_queue hardcodes WorkerOptions() because worker knobs
    # belong on the CLI. The CLI no longer exposes a drain, so this is the only way
    # left to drain N-at-a-time in one process.
    asyncio.run(
        arun_drain(resolve_backend(), queue, WorkerOptions(concurrency=concurrency))
    )


def run_resident_worker(
    queue: str,
    concurrency: int,
    environ: dict[str, str],
    batch_size: int | None = None,
) -> None:
    """Serve ``queue`` until signalled — the resident worker a probe's arm runs on.

    Not a draining worker: a probe needs one that stays up across the phases of an
    arm, because a draining one exits the moment the backlog is momentarily empty —
    which is exactly the state a queue of sleeping tasks presents. The parent stops it
    with ``SIGTERM``, which ``absurd_worker`` handles by leaving its poll loop.

    ``batch_size`` defaults to ``None``, which is what ``absurd_worker`` itself
    defaults to, so a caller that does not care passes nothing and the SDK falls back
    to claiming one batch per concurrency slot.
    """
    os.environ.update(environ)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", SETTINGS_MODULE)
    django.setup()
    call_command(
        "absurd_worker",
        queue=queue,
        concurrency=concurrency,
        batch_size=batch_size,
        stdout=io.StringIO(),
    )


if __name__ == "__main__":
    run_management_command()
