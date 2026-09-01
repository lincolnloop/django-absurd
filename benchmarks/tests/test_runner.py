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
