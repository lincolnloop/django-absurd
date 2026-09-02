import contextlib
import datetime as dt
import importlib.metadata
import os
import platform
import subprocess
import time
import typing as t
from dataclasses import dataclass
from pathlib import Path

import django
from django.db import connections

# A phase whose wall clock outruns its monotonic clock by more than this napped.
SUSPENSION_TOLERANCE_S = 2.0

REPO_ROOT = Path(__file__).resolve().parent.parent


class SuspendedPhaseError(Exception):
    def __init__(self, elapsed_s: float, wall_s: float) -> None:
        super().__init__(
            f"Wall clock advanced {wall_s:.1f}s over a phase the monotonic clock "
            f"measured at {elapsed_s:.1f}s: the host suspended or stalled mid-phase, "
            f"so every number this phase produced is fiction. Re-run the measurement "
            f"on a "
            f"machine that stays awake."
        )


@dataclass
class Phase:
    elapsed_s: float = 0.0
    wall_s: float = 0.0


@contextlib.contextmanager
def measure_phase() -> t.Iterator[Phase]:
    phase = Phase()
    started_monotonic = time.perf_counter()
    started_wall = time.time()
    yield phase
    phase.elapsed_s = time.perf_counter() - started_monotonic
    phase.wall_s = time.time() - started_wall
    check_phase_uninterrupted(phase.elapsed_s, phase.wall_s)


def check_phase_uninterrupted(elapsed_s: float, wall_s: float) -> None:
    # Only wall running AHEAD is a nap; behind is an NTP step, which costs no work.
    if wall_s - elapsed_s > SUSPENSION_TOLERANCE_S:
        raise SuspendedPhaseError(elapsed_s, wall_s)


def collect_host_context() -> dict[str, t.Any]:
    with connections["default"].cursor() as cursor:
        # Uptime rides along with the version: a server hammered for hours measures
        # slower than a freshly started one, and nothing else records which you got.
        # `cluster_name` rides along for the same reason and a sharper one: `db_bench`
        # sets it to `bench-tmpfs` and nothing else in a results file says the data
        # directory was RAM, where an absolute rate is not a durable figure at all.
        # Unset it reads back as the empty string, which is every ordinary server.
        # `shared_buffers` and `max_connections` are read off the server rather than
        # from `BENCH_SHARED_BUFFERS`/`BENCH_MAX_CONNECTIONS`: the harness process need
        # not have those set at all, and a running server is authoritative.
        cursor.execute(
            "select version(), "
            "extract(epoch from now() - pg_postmaster_start_time())::float8, "
            "current_setting('cluster_name'), "
            "current_setting('shared_buffers'), "
            "current_setting('max_connections')"
        )
        (
            postgres,
            postgres_uptime_s,
            cluster_name,
            shared_buffers,
            max_connections,
        ) = cursor.fetchone()
    return {
        "absurd_sdk": importlib.metadata.version("absurd-sdk"),
        "captured_at": dt.datetime.now(tz=dt.UTC).isoformat(),
        "cluster_name": cluster_name,
        # The HOST's cores — where the worker fleet runs and what the process_scaling
        # ladder derives from. `requested_container_*` below speaks for the server.
        "cpu_count": os.cpu_count() or 1,
        "django": django.get_version(),
        "git_sha": read_git_sha(),
        "load_avg_1m": read_load_average(),
        "max_connections": max_connections,
        "postgres": postgres,
        "postgres_uptime_s": float(postgres_uptime_s),
        "python": platform.python_version(),
        "requested_container_cpus": read_requested_limit("BENCH_CPUS"),
        "requested_container_memory": read_requested_limit("BENCH_MEMORY"),
        "shared_buffers": shared_buffers,
    }


def read_requested_limit(variable: str) -> str | None:
    """A container CPU or memory limit as REQUESTED, which is all that is knowable.

    Neither limit is visible over SQL, so there is nothing to measure and no honest
    way to derive one: an absent variable means the limit is unknown, not that the
    server was unlimited — whoever ran `docker compose up` may have set it in a shell
    this process never saw. Recorded so the report can say `unknown` in the one place
    a reader would otherwise take `cpu_count` for the server's cores.
    """
    return os.environ.get(variable) or None


def read_load_average() -> float:
    """The 1-minute load average right now, for a caller that samples it itself.

    This block is collected once a measurement is OVER, so the figure in it counts the
    load the harness had just made and can never answer "was the machine otherwise
    busy". A rep samples this on each side of itself instead.
    """
    return os.getloadavg()[0]


def read_git_sha() -> str:
    # Provenance is best-effort: an unpacked tarball has no .git directory and a
    # machine may have no git at all, and a missing SHA should not abort a
    # measurement.
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            cwd=REPO_ROOT,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return completed.stdout.strip()
