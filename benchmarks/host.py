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
        # Uptime, `cluster_name` and the two settings ride along with the version:
        # nothing else says which server a run got, and a live server is the truth.
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

    Neither limit is visible over SQL, and an absent variable means unknown rather than
    unlimited: whoever ran `docker compose up` may have set one in another shell.
    """
    return os.environ.get(variable) or None


def read_load_average() -> float:
    """The 1-minute load average right now, for a caller that samples it itself.

    The host block is collected once a measurement is OVER, so its own figure counts
    the load the harness had just made; a rep samples this on each side of itself.
    """
    return os.getloadavg()[0]


def read_git_sha() -> str:
    # Provenance is best-effort: an unpacked tarball has no .git directory, and a
    # missing SHA must not abort a measurement.
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
