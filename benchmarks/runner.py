import collections
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import typing as t
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from django.db import connections

REPO_ROOT = Path(__file__).resolve().parent.parent
MANAGE_PY = Path(__file__).resolve().parent / "manage.py"
WORKER_READY_MARKER = "Started worker on queue"
WORKER_READY_TIMEOUT_S = 30.0
WORKER_STOP_TIMEOUT_S = 30.0
# Enough of a dying worker's output to carry a traceback, bounded so a chatty
# worker cannot grow it without limit.
WORKER_TAIL_LINES = 50


@dataclass(frozen=True)
class Worker:
    proc: "subprocess.Popen[str]"
    tail: "collections.deque[str]"
    pump: threading.Thread


@dataclass(frozen=True)
class WorkerSpec:
    concurrency: int = 1
    batch_size: int | None = None
    poll_interval: float = 0.25
    claim_timeout: int = 120
    queue: str = "bench"


def start_workers(spec: WorkerSpec, count: int) -> list[Worker]:
    """Spawn ``count`` ``absurd_worker`` children, each already claiming."""
    environ = build_worker_env()
    started: list[Worker] = []
    try:
        for index in range(count):
            # A comprehension would discard the children already started when one
            # of them fails, leaking them for the rest of the benchmark run.
            started.append(spawn_worker(spec, index, environ))  # noqa: PERF401
    except BaseException:
        stop_workers(started)
        raise
    return started


def stop_workers(workers: list[Worker]) -> None:
    """SIGTERM every worker, reap it, then report any that had already died."""
    crashed = [worker for worker in workers if worker.proc.poll() not in (None, 0)]
    for worker in workers:
        if worker.proc.poll() is None:
            worker.proc.send_signal(signal.SIGTERM)
    for worker in workers:
        try:
            worker.proc.wait(timeout=WORKER_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            worker.proc.kill()
            worker.proc.wait()
        worker.pump.join(timeout=WORKER_STOP_TIMEOUT_S)
    if crashed:
        last_output = "".join(line for worker in crashed for line in worker.tail)
        msg = (
            f"{len(crashed)} absurd_worker child(ren) exited before the measurement "
            f"finished (codes {[worker.proc.returncode for worker in crashed]}); it "
            f"measured a worker count it never had. Last output:\n{last_output}"
        )
        raise RuntimeError(msg)


def spawn_worker(spec: WorkerSpec, index: int, environ: dict[str, str]) -> Worker:
    argv = [
        sys.executable,
        str(MANAGE_PY),
        "absurd_worker",
        "--queue",
        spec.queue,
        "--concurrency",
        str(spec.concurrency),
        "--poll-interval",
        str(spec.poll_interval),
        "--claim-timeout",
        str(spec.claim_timeout),
        "--worker-id",
        f"bench-{index}",
    ]
    if spec.batch_size is not None:
        argv += ["--batch-size", str(spec.batch_size)]
    # Popen execs a fresh interpreter rather than forking, so the child opens its own
    # Postgres connection instead of inheriting the parent's socket.
    proc = subprocess.Popen(
        argv,
        cwd=REPO_ROOT,
        env=environ,
        stderr=subprocess.STDOUT,
        stdout=subprocess.PIPE,
        text=True,
    )
    return wait_for_worker_ready(proc)


def build_worker_env() -> dict[str, str]:
    # Children must reach the database the PARENT is on — under pytest that is the
    # test database, not the one benchmarks/settings.py falls back to.
    return {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "benchmarks.settings",
        "DATABASE_URL": build_database_url(),
        # Without this the readiness line sits in the child's block-buffered pipe.
        "PYTHONUNBUFFERED": "1",
    }


def build_database_url() -> str:
    """Render the parent's live connection as the URL its settings module reads."""
    settings_dict = connections["default"].settings_dict
    user = urllib.parse.quote(settings_dict["USER"], safe="")
    password = urllib.parse.quote(settings_dict["PASSWORD"], safe="")
    host = settings_dict["HOST"]
    port = settings_dict["PORT"]
    name = settings_dict["NAME"]
    return f"postgres://{user}:{password}@{host}:{port}/{name}"


def wait_for_worker_ready(
    proc: "subprocess.Popen[str]", timeout_s: float = WORKER_READY_TIMEOUT_S
) -> Worker:
    lines: queue.Queue[str | None] = queue.Queue()
    tail: collections.deque[str] = collections.deque(maxlen=WORKER_TAIL_LINES)
    discarding = threading.Event()
    # Read on a THREAD, not readline() here: readline blocks with no timeout, so a
    # child that hangs before printing would outlive any deadline this checks.
    pump = threading.Thread(
        target=pump_until_eof,
        args=(t.cast("t.IO[str]", proc.stdout), lines, tail, discarding),
        daemon=True,
    )
    pump.start()
    deadline = time.monotonic() + timeout_s
    captured: list[str] = []
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty:
            break
        if line is None:
            break
        captured.append(line)
        if WORKER_READY_MARKER in line:
            discarding.set()
            return Worker(proc=proc, tail=tail, pump=pump)
    discarding.set()
    proc.kill()
    proc.wait()
    msg = (
        f"absurd_worker never reported readiness within {timeout_s:.0f}s. "
        f"Child output:\n{''.join(captured)}"
    )
    raise RuntimeError(msg)


def pump_until_eof(
    stream: "t.IO[str]",
    lines: "queue.Queue[str | None]",
    tail: "collections.deque[str]",
    discarding: threading.Event,
) -> None:
    # Drains past readiness so a chatty worker can never block on a full stdout pipe,
    # keeping only a bounded tail rather than its whole output.
    for line in stream:
        tail.append(line)
        if not discarding.is_set():
            lines.put(line)
    lines.put(None)
