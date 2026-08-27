"""Where a load-harness run lands: one timestamped JSON, with its plan dumps beside it.

``loadtest/results/`` is gitignored, so the harness writes there freely and you keep
what is worth keeping by hand. Every run is stamped, so successive runs accumulate and
can be diffed — and that stamp covers the plan dumps too, not just the JSON. A run
opens its own directory and writes its ``EXPLAIN`` output there; the JSON records that
directory as ``results_dir``, so the relative plan paths in it resolve to the plans
*that run* produced, and no later run can overwrite them.

The stamp carries microseconds: two runs in the same second must still not collide,
or the second silently takes the first's name.
"""

import importlib.metadata
import json
import pathlib
import platform
import statistics
import sys
import typing as t

if t.TYPE_CHECKING:
    import argparse

import django
from django.db import connections
from django.utils import timezone

import django_absurd
from django_absurd.queues import resolve_absurd_database

RESULTS_DIR = pathlib.Path(__file__).resolve().parent / "results"
STAMP_FORMAT = "%Y%m%dT%H%M%S.%fZ"


def open_run(name: str) -> pathlib.Path:
    """Create and return the directory this run's plan dumps belong in."""
    run_dir = resolve_results_dir() / name_run(name)
    run_dir.mkdir(exist_ok=True)
    return run_dir


def write_plan(run_dir: pathlib.Path, filename: str, plan: str) -> str:
    """Dump one query plan into the run's own directory.

    Returns the path to record in the JSON, relative to the ``results_dir`` that same
    JSON reports — which is this run's directory, so the two always agree.
    """
    (run_dir / filename).write_text(plan + "\n", encoding="utf-8")
    return filename


def write_run(
    name: str,
    payload: dict[str, t.Any],
    run_dir: pathlib.Path | None = None,
) -> pathlib.Path:
    """Write one run's payload as ``<name>-<UTC stamp>.json``, returning its path.

    Pass the ``open_run`` directory holding the plans this payload references, so the
    JSON and those plans carry the one stamp. A run that dumps no plans passes nothing
    and gets a stamp of its own.
    """
    results_dir = resolve_results_dir()
    stem = run_dir.name if run_dir is not None else name_run(name)
    document = {
        "name": name,
        "created_at": timezone.now().isoformat(),
        "results_dir": str(run_dir if run_dir is not None else results_dir),
        "environment": describe_environment(),
        **payload,
    }
    path = results_dir / f"{stem}.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


DEFAULT_REPEAT = 3


def add_repeat_argument(parser: "argparse.ArgumentParser") -> None:
    """Give a probe the ``--repeat`` every probe takes, worded the same way."""
    parser.add_argument(
        "--repeat",
        type=int,
        default=DEFAULT_REPEAT,
        help=(
            f"Times to measure each arm (default: {DEFAULT_REPEAT}). The reported "
            "number is the median and the table carries the spread; --repeat 1 is "
            "one draw, which is fast to take and unsafe to quote."
        ),
    )


def summarize_repeats(
    measure: "t.Callable[[], dict[str, t.Any]]",
    repeat: int,
    rank_by: str,
    metrics: "t.Sequence[str]" = (),
) -> dict[str, t.Any]:
    """Run ``measure`` ``repeat`` times and report the median run of the batch.

    The reported entry is one real observation — the run sitting at the median of
    ``rank_by`` — not a field-by-field blend of all of them. Averaging each field
    independently pulls an entry apart: a barrier arm's ``utilization`` and
    ``idle_share`` are two views of one timeline and must come from the same run, or
    they stop summing to the whole. Every run is kept under ``runs``, and ``spread``
    carries min, max and coefficient of variation for ``rank_by`` plus anything named
    in ``metrics``.
    """
    if repeat < 1:
        msg = f"repeat must be at least 1, got {repeat}"
        raise ValueError(msg)

    runs = [measure() for _ in range(repeat)]
    # Lower median, so an even batch reports a run it actually took.
    ranked = sorted(runs, key=lambda run: run[rank_by])
    entry = dict(ranked[(len(ranked) - 1) // 2])
    entry["runs"] = runs
    entry["spread"] = {name: measure_spread(runs, name) for name in (rank_by, *metrics)}
    return entry


def format_spread(entry: dict[str, t.Any], metric: str) -> str:
    """The range a median is hiding, as ``min-max (cv%)``."""
    spread = entry["spread"][metric]
    return f"{spread['min']:g}-{spread['max']:g} ({spread['cv']:g}%)"


def measure_spread(runs: "list[dict[str, t.Any]]", name: str) -> dict[str, float]:
    """Min, max and coefficient of variation for one metric across ``runs``."""
    values = [float(run[name]) for run in runs]
    mean = statistics.fmean(values)
    # One run has no spread rather than an undefined one, and a zero mean has no
    # meaningful relative spread — both report 0.0 instead of raising mid-probe.
    deviation = statistics.stdev(values) if len(values) > 1 else 0.0
    return {
        "min": min(values),
        "max": max(values),
        "cv": round(deviation / mean * 100, 1) if mean else 0.0,
    }


def describe_environment() -> dict[str, str]:
    """What this run was measured on — the half of a number that is not the number."""
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute("SELECT version()")
        [(version,)] = cursor.fetchall()
        settings = {}
        for name in (
            "shared_buffers",
            "effective_cache_size",
            "max_parallel_workers_per_gather",
        ):
            cursor.execute("SELECT setting FROM pg_settings WHERE name = %s", [name])
            [(setting,)] = cursor.fetchall()
            settings[name] = setting
    return {
        "python": platform.python_version(),
        "implementation": sys.implementation.name,
        "django": django.get_version(),
        "absurd_sdk": importlib.metadata.version("absurd-sdk"),
        "absurd_schema": django_absurd.ABSURD_SCHEMA_VERSION,
        "machine": platform.machine(),
        "system": platform.system(),
        "postgres": version,
        "git_sha": read_git_sha(),
        **settings,
    }


def read_git_sha() -> str:
    """The checkout's HEAD commit, read from git's own files.

    Read rather than shelled out to: ``S603`` is waived only under ``tests/``, and this
    is not worth the project's first production ``noqa``. Returns ``"unknown"`` rather
    than raising — a stamp must never be the thing that fails a probe.
    """
    try:
        git_dir = resolve_git_dir()
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head
        ref = head.removeprefix("ref:").strip()
        # A linked worktree keeps HEAD of its own but shares refs/ with the main
        # checkout, so the loose ref is looked for in both before packed-refs.
        common = git_dir / (git_dir / "commondir").read_text(encoding="utf-8").strip()
        for candidate in (git_dir / ref, common.resolve() / ref):
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8").strip()
        packed = (common.resolve() / "packed-refs").read_text(encoding="utf-8")
        return next(
            line.split(" ", 1)[0] for line in packed.splitlines() if line.endswith(ref)
        )
    except (OSError, StopIteration, ValueError):
        return "unknown"


def resolve_git_dir() -> pathlib.Path:
    repo = pathlib.Path(__file__).resolve().parent.parent
    dot_git = repo / ".git"
    if not dot_git.is_file():
        return dot_git
    pointer = dot_git.read_text(encoding="utf-8").removeprefix("gitdir:").strip()
    return pathlib.Path(pointer)


def resolve_results_dir() -> pathlib.Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    return RESULTS_DIR


def name_run(name: str) -> str:
    return f"{name}-{timezone.now().strftime(STAMP_FORMAT)}"
