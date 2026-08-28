import argparse
import dataclasses
import json
import os
import typing as t
from pathlib import Path

import django

from benchmarks import analysis, cells, host, producer, runner
from benchmarks.report import DEFAULT_RESULTS_DIR, format_spread
from django_absurd.flush import truncate_queue_tables

NOOP_ASYNC = "benchmarks.tasks.noop_async"
NOOP_SYNC = "benchmarks.tasks.noop_sync"
RUN_STEPS = "benchmarks.tasks.run_steps"
SLEEP_ASYNC = "benchmarks.tasks.sleep_async"
SLEEP_SYNC = "benchmarks.tasks.sleep_sync"

STAGE_NAMES = ("a", "b", "c", "d", "e", "f", "g")

SATURATION_TASKS = 5000
SATURATION_TIMEOUT_S = 900.0
RATE_OFFER_SECONDS = 60.0
RATE_TIMEOUT_S = 300.0
IDLE_PROBE_SECONDS = 30.0
IDLE_PROBE_WORKERS = 4
POLL_INTERVALS = (0.05, 0.25, 1.0)
PRODUCER_ENQUEUE_COUNT = 5000
PRODUCER_SPREAD_LIMIT = 0.15
WORKFLOW_TASKS = 2000


class MissingStageError(Exception):
    def __init__(self, path: Path, required_stage: str) -> None:
        super().__init__(
            f"{path} is missing, and this stage is calibrated from it. "
            f"Run `python -m benchmarks.sweep --stage {required_stage}` first."
        )


class UncalibratableStageError(Exception):
    def __init__(self, stage_size: int) -> None:
        super().__init__(
            f"None of the {stage_size} recorded cell(s) measured any throughput, so "
            f"there is no winning configuration to calibrate the next stage from. "
            f"Re-run the earlier stage on a quiet machine and check its flags."
        )


@dataclasses.dataclass(frozen=True)
class SweepOptions:
    results_dir: Path
    reps: int | None = None


def run_sweep(stage_names: list[str], options: SweepOptions) -> None:
    options.results_dir.mkdir(parents=True, exist_ok=True)
    for name in stage_names:
        run_stage(name, options)


def run_stage(name: str, options: SweepOptions) -> None:
    if name == "a":
        run_stage_a(options)
    elif name == "b":
        run_stage_b(options)
    elif name == "c":
        run_stage_c(options)
    elif name == "d":
        record_cells("d", build_stage_d_cells(), [], options)
    elif name == "e":
        record_cells(
            "e", build_stage_e_cells(read_winning_worker(options)), [], options
        )
    elif name == "f":
        run_stage_f(options)
    else:
        run_stage_g(options)


def run_stage_a(options: SweepOptions) -> None:
    """Claim amortization and slot parallelism, then the async dispatch ratio."""
    recorded: list[dict[str, t.Any]] = []
    record_cells("a", build_a1_cells(), recorded, options)
    winner = pick_winning_worker(recorded)
    record_cells("a", build_a2_cells(winner), recorded, options)
    record_cells("a", build_a3_cells(winner), recorded, options)


def run_stage_b(options: SweepOptions) -> None:
    """How throughput scales with worker processes at stage A's winning config."""
    worker = read_winning_worker(options)
    record_cells("b", build_stage_b_cells(worker), [], options)


def run_stage_c(options: SweepOptions) -> None:
    """What poll_interval buys in latency and costs in idle transactions."""
    worker = read_winning_worker(options)
    recorded: list[dict[str, t.Any]] = []
    record_cells("c", build_stage_c_cells(worker), recorded, options)
    probes = measure_idle_probes(worker)
    write_stage_file("c", recorded, options, {"idle_probes": probes})


def run_stage_f(options: SweepOptions) -> None:
    """The producer's own ceiling: one connection, eight threads, batched commits."""
    recorded: list[dict[str, t.Any]] = []
    for mode in ("single", "threaded", "atomic"):
        reps = []
        for _ in range(options.reps or 3):
            truncate_queue_tables("bench")
            reps.append(measure_producer_rep(mode))
        recorded.append(summarize_producer_reps(mode, reps))
        write_stage_file("f", recorded, options)
        median = recorded[-1]["median"]
        print(f"f_{mode}: {median.get('enqueues_per_s', 0.0):.1f} enqueues/s")


def measure_producer_rep(
    mode: t.Literal["single", "threaded", "atomic"],
) -> dict[str, t.Any]:
    """One stage F rep, bracketed like every other measured phase.

    Not because a nap deflates the numbers — perf_counter stops with the host — but so
    stage F refuses a slept-through rep instead of being the one stage that keeps it.
    """
    try:
        with host.measure_phase():
            metrics = producer.run_producer_benchmark(mode, PRODUCER_ENQUEUE_COUNT)
    except host.SuspendedPhaseError as exc:
        return {"valid": False, "error": str(exc)}
    else:
        return {"valid": True, **metrics}


def run_stage_g(options: SweepOptions) -> None:
    """End-to-end latency at fractions of stage B's measured ceiling."""
    worker, workers, ceiling = read_ceiling(options)
    record_cells(
        "g",
        [
            cells.CellSpec(
                name=f"g_rate_{int(fraction * 100)}pct",
                mode="rate",
                task_path=NOOP_SYNC,
                worker=worker,
                workers=workers,
                rate_per_s=ceiling * fraction,
                duration_s=RATE_OFFER_SECONDS,
                timeout_s=RATE_TIMEOUT_S,
            )
            for fraction in (0.25, 0.50, 0.75, 0.90)
        ],
        [],
        options,
    )


def build_a1_cells() -> list[cells.CellSpec]:
    return [
        cells.CellSpec(
            name=f"a1_c{concurrency}",
            mode="saturation",
            task_path=NOOP_SYNC,
            worker=runner.WorkerSpec(concurrency=concurrency),
            tasks=SATURATION_TASKS,
            timeout_s=SATURATION_TIMEOUT_S,
        )
        for concurrency in (1, 2, 4, 8, 16)
    ]


def build_a2_cells(winner: runner.WorkerSpec) -> list[cells.CellSpec]:
    return [
        cells.CellSpec(
            name=f"a2_batch_{batch_size}",
            mode="saturation",
            task_path=NOOP_SYNC,
            worker=dataclasses.replace(winner, batch_size=batch_size),
            tasks=SATURATION_TASKS,
            timeout_s=SATURATION_TIMEOUT_S,
        )
        for batch_size in (1, 2 * winner.concurrency)
    ]


def build_a3_cells(winner: runner.WorkerSpec) -> list[cells.CellSpec]:
    return [
        cells.CellSpec(
            name="a3_async",
            mode="saturation",
            task_path=NOOP_ASYNC,
            worker=winner,
            tasks=SATURATION_TASKS,
            timeout_s=SATURATION_TIMEOUT_S,
        )
    ]


def build_stage_b_cells(winner: runner.WorkerSpec) -> list[cells.CellSpec]:
    return [
        cells.CellSpec(
            name=f"b_workers_{count}",
            mode="saturation",
            task_path=NOOP_SYNC,
            worker=winner,
            workers=count,
            tasks=max(4000, 2000 * count),
            timeout_s=SATURATION_TIMEOUT_S,
        )
        for count in (1, 2, 4, 6, 8)
    ]


def build_stage_c_cells(winner: runner.WorkerSpec) -> list[cells.CellSpec]:
    return [
        cells.CellSpec(
            name=f"c_poll_{poll_interval:g}",
            mode="rate",
            task_path=NOOP_SYNC,
            worker=dataclasses.replace(winner, poll_interval=poll_interval),
            rate_per_s=5.0,
            duration_s=RATE_OFFER_SECONDS,
            timeout_s=RATE_TIMEOUT_S,
        )
        for poll_interval in POLL_INTERVALS
    ]


def build_stage_d_cells() -> list[cells.CellSpec]:
    return [
        cells.CellSpec(
            name=f"d_{flavour}_c{concurrency}",
            mode="saturation",
            task_path=task_path,
            worker=runner.WorkerSpec(concurrency=concurrency),
            tasks=250 * concurrency,
            timeout_s=SATURATION_TIMEOUT_S,
        )
        for concurrency in (4, 16, 32)
        for flavour, task_path in (("async", SLEEP_ASYNC), ("sync", SLEEP_SYNC))
    ]


def build_stage_e_cells(winner: runner.WorkerSpec) -> list[cells.CellSpec]:
    return [
        cells.CellSpec(
            name=name,
            mode="saturation",
            task_path=task_path,
            worker=winner,
            tasks=WORKFLOW_TASKS,
            timeout_s=SATURATION_TIMEOUT_S,
        )
        for name, task_path in (("e_flat", NOOP_SYNC), ("e_workflow", RUN_STEPS))
    ]


def measure_idle_probes(winner: runner.WorkerSpec) -> list[dict[str, t.Any]]:
    probes: list[dict[str, t.Any]] = []
    for poll_interval in POLL_INTERVALS:
        truncate_queue_tables(winner.queue)
        procs = runner.start_workers(
            dataclasses.replace(winner, poll_interval=poll_interval),
            IDLE_PROBE_WORKERS,
        )
        try:
            commits_per_s = analysis.measure_idle_commit_rate(IDLE_PROBE_SECONDS)
        finally:
            runner.stop_workers(procs)
        per_worker = commits_per_s / IDLE_PROBE_WORKERS
        probes.append(
            {
                "poll_interval": poll_interval,
                "workers": IDLE_PROBE_WORKERS,
                "claims_per_s_per_worker": per_worker,
            }
        )
        print(f"idle poll={poll_interval:g}: {per_worker:.2f} claims/s/worker")
    return probes


def record_cells(
    stage: str,
    specs: list[cells.CellSpec],
    recorded: list[dict[str, t.Any]],
    options: SweepOptions,
) -> None:
    for spec in specs:
        recorded.append(cells.run_cell(apply_reps_override(spec, options)))
        write_stage_file(stage, recorded, options)
        print(summarize_cell(recorded[-1]))


def apply_reps_override(spec: cells.CellSpec, options: SweepOptions) -> cells.CellSpec:
    if options.reps is None:
        return spec
    return dataclasses.replace(spec, reps=options.reps)


def write_stage_file(
    stage: str,
    recorded: list[dict[str, t.Any]],
    options: SweepOptions,
    extra: dict[str, t.Any] | None = None,
) -> None:
    # Rewritten after every cell so a sweep killed at hour two keeps what it measured.
    path = options.results_dir / f"stage_{stage}.json"
    staged = path.with_suffix(".json.tmp")
    staged.write_text(
        json.dumps({"stage": stage, "cells": recorded, **(extra or {})}, indent=2)
        + "\n"
    )
    staged.replace(path)


def summarize_cell(result: dict[str, t.Any]) -> str:
    median = result["median"]
    line = (
        f"{result['spec']['name']}: "
        f"{median.get('throughput_per_s', 0.0):.1f} tasks/s, "
        f"e2e p50 {median.get('end_to_end_p50_s', 0.0) * 1000:.1f}ms, "
        f"spread {format_spread(result['spread'])}"
    )
    return f"{line} [FLAGGED]" if result["flagged"] else line


def summarize_producer_reps(
    mode: str, reps: list[dict[str, t.Any]]
) -> dict[str, t.Any]:
    valid = sorted(
        (rep for rep in reps if rep["valid"]), key=lambda rep: rep["enqueues_per_s"]
    )
    median: dict[str, t.Any] = valid[(len(valid) - 1) // 2] if valid else {}
    values = [rep["enqueues_per_s"] for rep in valid]
    spread = (
        (max(values) - min(values)) / median["enqueues_per_s"]
        if valid and median["enqueues_per_s"]
        else None
    )
    return {
        "spec": {"name": f"f_{mode}", "mode": "producer"},
        "reps": reps,
        "median": median,
        "spread": spread,
        "flagged": (
            spread is None or spread > PRODUCER_SPREAD_LIMIT or len(valid) != len(reps)
        ),
        "host": host.collect_host_context(),
    }


def read_winning_worker(options: SweepOptions) -> runner.WorkerSpec:
    return pick_winning_worker(read_stage_cells(options, "a"))


def read_ceiling(options: SweepOptions) -> tuple[runner.WorkerSpec, int, float]:
    recorded = read_stage_cells(options, "b")
    best = pick_best_cell(recorded)
    return (
        runner.WorkerSpec(**best["spec"]["worker"]),
        best["spec"]["workers"],
        best["median"]["throughput_per_s"],
    )


def read_stage_cells(options: SweepOptions, stage: str) -> list[dict[str, t.Any]]:
    path = options.results_dir / f"stage_{stage}.json"
    if not path.exists():
        raise MissingStageError(path, stage.upper())
    return t.cast("list[dict[str, t.Any]]", json.loads(path.read_text())["cells"])


def pick_winning_worker(recorded: list[dict[str, t.Any]]) -> runner.WorkerSpec:
    return runner.WorkerSpec(**pick_best_cell(recorded)["spec"]["worker"])


def pick_best_cell(recorded: list[dict[str, t.Any]]) -> dict[str, t.Any]:
    # Flagged cells are unreliable, but calibrating on nothing is worse than
    # calibrating on a noisy best, so they are the fallback rather than an error.
    candidates = [cell for cell in recorded if not cell["flagged"]] or recorded
    best = max(candidates, key=lambda cell: cell["median"].get("throughput_per_s", 0.0))
    # A zero-throughput winner would calibrate stages B/C/E/G on nothing at all.
    if best["median"].get("throughput_per_s", 0.0) <= 0:
        raise UncalibratableStageError(len(recorded))
    return best


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the django-absurd load sweep.")
    parser.add_argument("--stage", action="append", choices=STAGE_NAMES, type=str.lower)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR, type=Path)
    parser.add_argument("--reps", default=None, type=int)
    args = parser.parse_args()
    stages = list(STAGE_NAMES) if args.all else (args.stage or [])
    if not stages:
        parser.error("pass --stage <A-G> (repeatable) or --all")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "benchmarks.settings")
    django.setup()
    run_sweep(stages, SweepOptions(results_dir=args.results_dir, reps=args.reps))


if __name__ == "__main__":
    main()
