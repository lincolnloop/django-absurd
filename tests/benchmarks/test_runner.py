import re
import subprocess
import sys
import time

import pytest

from benchmarks import runner


def test_refuses_a_worker_that_never_reports_readiness() -> None:
    silent_child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stderr=subprocess.STDOUT,
        stdout=subprocess.PIPE,
        text=True,
    )
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="never reported readiness"):
        runner.wait_for_worker_ready(silent_child, timeout_s=2.0)

    assert (time.monotonic() - started < 10.0) is True
    assert (silent_child.poll() is not None) is True


def test_reports_a_crashed_workers_last_output() -> None:
    crashing_child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "print('Started worker on queue bench.');"
                "print('psycopg.errors.RaiseException: absurd.complete_run boom');"
                "raise SystemExit(1)"
            ),
        ],
        stderr=subprocess.STDOUT,
        stdout=subprocess.PIPE,
        text=True,
    )
    worker = runner.wait_for_worker_ready(crashing_child, timeout_s=10.0)
    crashing_child.wait()

    with pytest.raises(RuntimeError, match=re.escape("absurd.complete_run boom")):
        runner.stop_workers([worker])


def test_refuses_a_worker_still_talking_when_its_deadline_passes() -> None:
    """Output is not readiness: a worker can print forever and never claim anything.

    The deadline has to end the wait on its own, so the two seconds are the point —
    a child this chatty keeps a line in the queue at every poll, which is what leaves
    the loop's own condition as the only way out.
    """
    chatty_child = subprocess.Popen(
        [sys.executable, "-c", "\nwhile True:\n    print('claiming nothing')\n"],
        stderr=subprocess.STDOUT,
        stdout=subprocess.PIPE,
        text=True,
    )
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="never reported readiness within 2s"):
        runner.wait_for_worker_ready(chatty_child, timeout_s=2.0)

    assert (time.monotonic() - started < 10.0) is True
    assert (chatty_child.poll() is not None) is True


def test_kills_a_worker_that_ignores_the_stop_signal() -> None:
    """A worker wedged in a task would otherwise outlive the run and keep claiming.

    Takes the full ``WORKER_STOP_TIMEOUT_S`` by construction — the deadline is what is
    under test — so it is the slowest test in the suite and cannot be made faster
    without giving the harness a knob nothing but a test would turn.
    """
    deaf_child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import signal, time;"
                "signal.signal(signal.SIGTERM, lambda *_: None);"
                "print('Started worker on queue bench.', flush=True);"
                "time.sleep(600)"
            ),
        ],
        stderr=subprocess.STDOUT,
        stdout=subprocess.PIPE,
        text=True,
    )
    worker = runner.wait_for_worker_ready(deaf_child, timeout_s=10.0)

    runner.stop_workers([worker])

    assert (deaf_child.poll() is not None) is True


@pytest.mark.django_db(transaction=True)
def test_abandons_the_worker_ladder_when_a_child_cannot_start() -> None:
    """A spawn that fails part way through a ladder takes the ladder down with it.

    Two are asked for on a queue the backend never declared, so the first child
    refuses at startup and the second is never spawned. Matched rather than compared
    whole: under `--cov` the child's merged output carries coverage's own warnings.
    """
    with pytest.raises(
        RuntimeError,
        match=re.escape(
            "CommandError: Queue 'undeclared' is not declared for backend 'default'. "
            "Valid queues: bench. Add it to the QUEUES list in your TASKS backend "
            "settings."
        ),
    ):
        runner.start_workers(runner.WorkerSpec(queue="undeclared"), 2)
