"""The only tests here that enter below the command line, and why.

Every other file drives `stages.main`. These cover what the runner does when a child
misbehaves — never reporting readiness, talking past its deadline, ignoring its stop
signal, dying before the ladder finishes — and no stage can ask for that. A driver
flag that broke its own workers would exist for nothing but these tests.

So the child is a real process that misbehaves on purpose. Nothing is mocked; only
which program it runs is substituted, and the protocol under test is the real one.
"""

import re
import subprocess
import sys

import pytest

import runner

# More than the one child every other test here drives, few enough that the test costs
# a handful of worker start-ups.
FLEET_SIZE = 3


def test_refuses_a_worker_that_never_reports_readiness() -> None:
    silent_child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stderr=subprocess.STDOUT,
        stdout=subprocess.PIPE,
        text=True,
    )

    with pytest.raises(RuntimeError, match="never reported readiness"):
        runner.wait_for_worker_ready(silent_child, timeout_s=2.0)

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

    with pytest.raises(RuntimeError, match="never reported readiness within 2s"):
        runner.wait_for_worker_ready(chatty_child, timeout_s=2.0)

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
    """A fleet whose children cannot start takes the whole fleet down with it.

    Two are asked for on a queue the backend never declared, so both refuse at startup
    and neither is left behind. Matched rather than compared whole: under `--cov` the
    child's merged output carries coverage's own warnings.
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


@pytest.mark.django_db(transaction=True)
def test_starts_a_whole_fleet_and_returns_every_child_past_its_readiness_line() -> None:
    """One call, one fleet, and nothing half-started left behind it.

    A saturation rep preloads before it starts its fleet, so the launch order is a
    property worth having: every child goes out before any readiness line is waited
    for. What that buys is elapsed time and nothing else, which is the machine's to
    decide, so what is pinned here is the fleet a caller gets back — as many children
    as were asked for, each still running and each having printed the readiness line
    the harness waits on. `benchmarks/CLAUDE.md` carries the instrumented cost of
    launching them one at a time.
    """
    at_once = runner.start_workers(runner.WorkerSpec(), FLEET_SIZE)

    try:
        assert {
            "children": len(at_once),
            "still_running": [worker.proc.poll() for worker in at_once],
            "reported_ready": [
                runner.WORKER_READY_MARKER in "".join(worker.tail) for worker in at_once
            ],
        } == {
            "children": FLEET_SIZE,
            "still_running": [None] * FLEET_SIZE,
            "reported_ready": [True] * FLEET_SIZE,
        }
    finally:
        runner.stop_workers(at_once)
