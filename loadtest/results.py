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

import json
import pathlib
import typing as t

from django.utils import timezone

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
        **payload,
    }
    path = results_dir / f"{stem}.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def resolve_results_dir() -> pathlib.Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    return RESULTS_DIR


def name_run(name: str) -> str:
    return f"{name}-{timezone.now().strftime(STAMP_FORMAT)}"
