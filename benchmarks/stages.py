import argparse
import dataclasses
import json
import os
import typing as t
from pathlib import Path

import django

from benchmarks import analysis, host, measurement, producer, runner
from benchmarks.report import DEFAULT_RESULTS_DIR, format_spread
from django_absurd.flush import truncate_queue_tables

NOOP_ASYNC = "benchmarks.tasks.noop_async"
NOOP_SYNC = "benchmarks.tasks.noop_sync"
RUN_STEPS = "benchmarks.tasks.run_steps"
SLEEP_ASYNC = "benchmarks.tasks.sleep_async"
SLEEP_SYNC = "benchmarks.tasks.sleep_sync"

STAGE_NAMES = ("a", "b", "c", "d", "e", "f", "g")

# Which stage each one reads back off disk to calibrate itself. A partial order, not a
# sequence: D and F depend on nothing, so the letters imply an ordering that is not
# real. Naming several stages runs them in dependency order whatever order they arrive
# in; a stage named alone whose prerequisite is missing still refuses rather than
# quietly running it.
STAGE_DEPENDS_ON = {
    "b": "a",
    "c": "a",
    "e": "a",
    "g": "b",
}

STAGE_DESCRIPTIONS = {
    "a": "one worker's knobs: concurrency ladder, then batch size, then async dispatch",
    "b": "throughput scaling across worker processes, at stage A's winning config",
    "c": "poll_interval: latency under a paced offer, plus idle claim-rate probes",
    "d": "async vs sync task bodies at the same 50 ms of simulated IO",
    "e": "checkpoint cost: a 4-step workflow against a flat task",
    "f": "the producer's own ceiling: one connection, eight threads, batched commits",
    "g": "end-to-end latency at fractions of stage B's measured ceiling",
}

# Sized so the slowest stage A measurement (concurrency 1, ~67 tasks/s) drains a rep
# in about 75 s; throughput divides a p10-p90 window; a small backlog is mostly ramp.
SATURATION_TASKS = 5000
SATURATION_TIMEOUT_S = 900.0
RATE_OFFER_SECONDS = 60.0
HOST_CPUS = os.cpu_count() or 1
# A rate measurement's producer runs on the same box as its workers, so calibrating
# off the fastest saturation result asks for an offer it has no cores left to give.
RATE_WORKER_CAP = max(1, HOST_CPUS // 2)
RATE_TIMEOUT_S = 300.0
# Reps within 150 ms of each other are not called unstable however far apart they read
# relatively. A rate measurement ranks on `end_to_end_p50_s`, and relative spread
# divides by that median, so it RISES as the measurement gets faster.
RATE_SPREAD_FLOOR_S = 0.15
IDLE_PROBE_SECONDS = 30.0
IDLE_PROBE_WORKERS = 4
POLL_INTERVALS = (0.05, 0.25, 1.0)
# ~25 s per rep at the slowest mode's ~200 enqueues/s: enough for stable percentiles
# while 3 reps x 3 modes still finish in a couple of minutes.
PRODUCER_ENQUEUE_COUNT = 5000
PRODUCER_SPREAD_LIMIT = 0.15
# The 4-checkpoint task runs ~4.5x slower than a flat one, so SATURATION_TASKS would
# push a rep past two minutes; 2000 keeps stage E on the same per-rep budget.
WORKFLOW_TASKS = 2000


class MissingStageError(Exception):
    def __init__(self, path: Path, required_stage: str) -> None:
        super().__init__(
            f"{path} is missing, and this stage is calibrated from it. "
            f"Run `python -m benchmarks.stages --stage {required_stage}` first."
        )


class UncalibratableStageError(Exception):
    def __init__(self, stage_size: int) -> None:
        super().__init__(
            f"None of the {stage_size} recorded measurement(s) measured any "
            f"throughput, so there is no winning configuration to calibrate the next "
            f"stage from. "
            f"Re-run the earlier stage on a quiet machine and check its flags."
        )


@dataclasses.dataclass(frozen=True)
class StageOptions:
    results_dir: Path
    reps: int | None = None
    # Override a stage's production size, so the suite can drive one end to end in
    # seconds. Saturation stages are sized in tasks and rate stages in seconds, so
    # one flag scaling both would hide which mode a stage is in.
    tasks: int | None = None
    duration_s: float | None = None


def run_stages(stage_names: list[str], options: StageOptions) -> None:
    options.results_dir.mkdir(parents=True, exist_ok=True)
    for name in order_by_dependency(stage_names):
        run_stage(name, options)


def order_by_dependency(stage_names: list[str]) -> list[str]:
    """Sort the requested stages so a prerequisite runs before what calibrates on it.

    Only orders what was asked for — it never adds a missing prerequisite, because a
    stage that cannot calibrate should say so rather than silently run a second stage
    the caller did not ask for.
    """
    requested = set(stage_names)
    return [name for name in STAGE_NAMES if name in requested]


def run_stage(name: str, options: StageOptions) -> None:
    print(f"stage {name.upper()}: {STAGE_DESCRIPTIONS[name]}")
    if name == "a":
        run_stage_a(options)
    elif name == "b":
        run_stage_b(options)
    elif name == "c":
        run_stage_c(options)
    elif name == "d":
        record_measurements("d", build_stage_d_measurements(), [], options)
    elif name == "e":
        record_measurements(
            "e", build_stage_e_measurements(read_winning_worker(options)), [], options
        )
    elif name == "f":
        run_stage_f(options)
    else:
        run_stage_g(options)


def run_stage_a(options: StageOptions) -> None:
    """Claim amortization and concurrency scaling, then the async dispatch ratio."""
    recorded: list[dict[str, t.Any]] = []
    record_measurements("a", build_a1_measurements(), recorded, options)
    winner = pick_winning_worker(recorded)
    record_measurements("a", build_a2_measurements(winner), recorded, options)
    record_measurements("a", build_a3_measurements(winner), recorded, options)


def run_stage_b(options: StageOptions) -> None:
    """How throughput scales with worker processes at stage A's winning config."""
    worker = read_winning_worker(options)
    record_measurements("b", build_stage_b_measurements(worker), [], options)


def run_stage_c(options: StageOptions) -> None:
    """What poll_interval buys in latency and costs in idle transactions."""
    worker = read_winning_worker(options)
    recorded: list[dict[str, t.Any]] = []
    record_measurements("c", build_stage_c_measurements(worker), recorded, options)
    probes = measure_idle_probes(worker, options.duration_s or IDLE_PROBE_SECONDS)
    write_stage_file("c", recorded, options, {"idle_probes": probes})


def run_stage_f(options: StageOptions) -> None:
    """The producer's own ceiling: one connection, eight threads, batched commits."""
    recorded: list[dict[str, t.Any]] = []
    enqueues = options.tasks or PRODUCER_ENQUEUE_COUNT
    for mode in ("single", "threaded", "atomic"):
        reps = []
        for _ in range(options.reps or 3):
            truncate_queue_tables("bench")
            reps.append(measure_producer_rep(mode, enqueues))
        recorded.append(summarize_producer_reps(mode, reps))
        write_stage_file("f", recorded, options)
        median = recorded[-1]["median"]
        print(f"f_{mode}: {median.get('enqueues_per_s', 0.0):.1f} enqueues/s")


def measure_producer_rep(
    mode: t.Literal["single", "threaded", "atomic"],
    enqueues: int = PRODUCER_ENQUEUE_COUNT,
) -> dict[str, t.Any]:
    """One stage F rep, bracketed like every other measured phase.

    Not because a nap deflates the numbers — perf_counter stops with the host — but so
    stage F refuses a slept-through rep instead of being the one stage that keeps it.
    """
    try:
        with host.measure_phase():
            metrics = producer.run_producer_benchmark(mode, enqueues)
    except host.SuspendedPhaseError as exc:
        return {"valid": False, "error": str(exc)}
    else:
        return {"valid": True, **metrics}


def run_stage_g(options: StageOptions) -> None:
    """End-to-end latency at fractions of stage B's measured ceiling."""
    worker, workers, ceiling = read_ceiling(options)
    record_measurements(
        "g",
        [
            measurement.MeasurementSpec(
                name=f"g_rate_{int(fraction * 100)}pct",
                mode="rate",
                task_path=NOOP_SYNC,
                worker=worker,
                workers=workers,
                rate_per_s=ceiling * fraction,
                duration_s=RATE_OFFER_SECONDS,
                timeout_s=RATE_TIMEOUT_S,
                spread_floor=RATE_SPREAD_FLOOR_S,
            )
            for fraction in (0.25, 0.50, 0.75, 0.90)
        ],
        [],
        options,
    )


def build_a1_measurements() -> list[measurement.MeasurementSpec]:
    return [
        measurement.MeasurementSpec(
            name=f"a1_c{concurrency}",
            mode="saturation",
            task_path=NOOP_SYNC,
            worker=runner.WorkerSpec(concurrency=concurrency),
            tasks=SATURATION_TASKS,
            timeout_s=SATURATION_TIMEOUT_S,
        )
        for concurrency in (1, 2, 4, 8, 16)
    ]


def build_a2_measurements(
    winner: runner.WorkerSpec,
) -> list[measurement.MeasurementSpec]:
    return [
        measurement.MeasurementSpec(
            name=f"a2_batch_{batch_size}",
            mode="saturation",
            task_path=NOOP_SYNC,
            worker=dataclasses.replace(winner, batch_size=batch_size),
            tasks=SATURATION_TASKS,
            timeout_s=SATURATION_TIMEOUT_S,
        )
        for batch_size in (1, 2 * winner.concurrency)
    ]


def build_a3_measurements(
    winner: runner.WorkerSpec,
) -> list[measurement.MeasurementSpec]:
    return [
        measurement.MeasurementSpec(
            name="a3_async",
            mode="saturation",
            task_path=NOOP_ASYNC,
            worker=winner,
            tasks=SATURATION_TASKS,
            timeout_s=SATURATION_TIMEOUT_S,
        )
    ]


def build_stage_b_measurements(
    winner: runner.WorkerSpec,
) -> list[measurement.MeasurementSpec]:
    return [
        measurement.MeasurementSpec(
            name=f"b_workers_{count}",
            mode="saturation",
            task_path=NOOP_SYNC,
            worker=winner,
            workers=count,
            tasks=max(4000, 2000 * count),
            timeout_s=SATURATION_TIMEOUT_S,
        )
        for count in build_worker_ladder(HOST_CPUS)
    ]


def build_worker_ladder(cores: int) -> list[int]:
    """Worker counts stage B measures on a host with ``cores`` usable CPUs."""
    # 1 and 2 anchor the low end where per-worker efficiency is still readable; the
    # quarter/half/three-quarter steps track the host so the curve means the same thing
    # on any box, and nothing exceeds the core count.
    steps = {1, 2, cores // 4, cores // 2, cores * 3 // 4, cores}
    return sorted(step for step in steps if 1 <= step <= cores)


def build_stage_c_measurements(
    winner: runner.WorkerSpec,
) -> list[measurement.MeasurementSpec]:
    return [
        measurement.MeasurementSpec(
            name=f"c_poll_{poll_interval:g}",
            mode="rate",
            task_path=NOOP_SYNC,
            worker=dataclasses.replace(winner, poll_interval=poll_interval),
            rate_per_s=5.0,
            duration_s=RATE_OFFER_SECONDS,
            timeout_s=RATE_TIMEOUT_S,
            spread_floor=RATE_SPREAD_FLOOR_S,
        )
        for poll_interval in POLL_INTERVALS
    ]


def build_stage_d_measurements() -> list[measurement.MeasurementSpec]:
    return [
        measurement.MeasurementSpec(
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


def build_stage_e_measurements(
    winner: runner.WorkerSpec,
) -> list[measurement.MeasurementSpec]:
    return [
        measurement.MeasurementSpec(
            name=name,
            mode="saturation",
            task_path=task_path,
            worker=winner,
            tasks=WORKFLOW_TASKS,
            timeout_s=SATURATION_TIMEOUT_S,
        )
        for name, task_path in (("e_flat", NOOP_SYNC), ("e_workflow", RUN_STEPS))
    ]


def measure_idle_probes(
    winner: runner.WorkerSpec, seconds: float = IDLE_PROBE_SECONDS
) -> list[dict[str, t.Any]]:
    probes: list[dict[str, t.Any]] = []
    for poll_interval in POLL_INTERVALS:
        truncate_queue_tables(winner.queue)
        procs = runner.start_workers(
            dataclasses.replace(winner, poll_interval=poll_interval),
            IDLE_PROBE_WORKERS,
        )
        try:
            commits_per_s = analysis.measure_idle_commit_rate(seconds)
        finally:
            runner.stop_workers(procs)
        per_worker = commits_per_s / IDLE_PROBE_WORKERS
        probes.append(
            {
                "poll_interval": poll_interval,
                "workers": IDLE_PROBE_WORKERS,
                "seconds": seconds,
                "claims_per_s_per_worker": per_worker,
            }
        )
        print(f"idle poll={poll_interval:g}: {per_worker:.2f} claims/s/worker")
    return probes


def record_measurements(
    stage: str,
    specs: list[measurement.MeasurementSpec],
    recorded: list[dict[str, t.Any]],
    options: StageOptions,
) -> None:
    for spec in specs:
        recorded.append(
            measurement.run_measurement(apply_size_overrides(spec, options))
        )
        write_stage_file(stage, recorded, options)
        print(summarize_measurement(recorded[-1]))


def apply_size_overrides(
    spec: measurement.MeasurementSpec, options: StageOptions
) -> measurement.MeasurementSpec:
    """Shrink a production-sized spec to whatever the caller asked for.

    ``tasks`` only reaches a saturation spec and ``duration`` only a rate one: the two
    modes are sized in different units, and one flag scaling both would hide that.
    """
    replacements: dict[str, t.Any] = {}
    if options.reps is not None:
        replacements["reps"] = options.reps
    if options.tasks is not None and spec.mode == "saturation":
        replacements["tasks"] = options.tasks
    if options.duration_s is not None and spec.mode == "rate":
        replacements["duration_s"] = options.duration_s
    return dataclasses.replace(spec, **replacements) if replacements else spec


def write_stage_file(
    stage: str,
    recorded: list[dict[str, t.Any]],
    options: StageOptions,
    extra: dict[str, t.Any] | None = None,
) -> None:
    # Rewritten after every measurement so a run killed at hour two keeps everything.
    path = options.results_dir / f"stage_{stage}.json"
    staged = path.with_suffix(".json.tmp")
    staged.write_text(
        json.dumps(
            {"stage": stage, "measurements": recorded, **(extra or {})}, indent=2
        )
        + "\n"
    )
    staged.replace(path)


def summarize_measurement(result: dict[str, t.Any]) -> str:
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
    # The shared helper rather than a second copy of the arithmetic: this stage had
    # its own, so the "one rep has no spread" guard had to be fixed twice.
    spread = measurement.measure_spread(valid, median, "enqueues_per_s")
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


def read_winning_worker(options: StageOptions) -> runner.WorkerSpec:
    return pick_winning_worker(read_stage_measurements(options, "a"))


def read_ceiling(options: StageOptions) -> tuple[runner.WorkerSpec, int, float]:
    recorded = read_stage_measurements(options, "b")
    best = pick_rate_calibration_measurement(recorded)
    return (
        runner.WorkerSpec(**best["spec"]["worker"]),
        best["spec"]["workers"],
        best["median"]["throughput_per_s"],
    )


def read_stage_measurements(
    options: StageOptions, stage: str
) -> list[dict[str, t.Any]]:
    path = options.results_dir / f"stage_{stage}.json"
    if not path.exists():
        raise MissingStageError(path, stage.upper())
    return t.cast(
        "list[dict[str, t.Any]]", json.loads(path.read_text())["measurements"]
    )


def pick_winning_worker(recorded: list[dict[str, t.Any]]) -> runner.WorkerSpec:
    return runner.WorkerSpec(**pick_best_measurement(recorded)["spec"]["worker"])


def pick_best_measurement(recorded: list[dict[str, t.Any]]) -> dict[str, t.Any]:
    # Flagged measurements are unreliable, but calibrating on nothing is worse than
    # calibrating on a noisy best, so they are the fallback rather than an error.
    candidates = [entry for entry in recorded if not entry["flagged"]] or recorded
    best = max(
        candidates, key=lambda entry: entry["median"].get("throughput_per_s", 0.0)
    )
    # A zero-throughput winner would calibrate stages B/C/E/G on nothing at all.
    if best["median"].get("throughput_per_s", 0.0) <= 0:
        raise UncalibratableStageError(len(recorded))
    return best


def pick_rate_calibration_measurement(
    recorded: list[dict[str, t.Any]],
) -> dict[str, t.Any]:
    """Pick what a rate stage calibrates from, leaving the producer some cores."""
    # Falling back to the whole set keeps a stage B run that never went below the cap
    # calibratable, at the cost of an offer its producer may not reach.
    capped = [
        entry for entry in recorded if entry["spec"]["workers"] <= RATE_WORKER_CAP
    ]
    return pick_best_measurement(capped or recorded)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run the django-absurd benchmark stages."
    )
    parser.add_argument(
        "stages",
        nargs="*",
        choices=[*STAGE_NAMES, []],
        type=str.lower,
        help="Stages to run, in any order; omit to run all of them.",
    )
    parser.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR, type=Path)
    parser.add_argument("--reps", default=None, type=int)
    parser.add_argument("--tasks", default=None, type=int)
    parser.add_argument("--duration", default=None, type=float)
    args = parser.parse_args(argv)
    stages = args.stages or list(STAGE_NAMES)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "benchmarks.settings")
    django.setup()
    run_stages(
        stages,
        StageOptions(
            results_dir=args.results_dir,
            reps=args.reps,
            tasks=args.tasks,
            duration_s=args.duration,
        ),
    )


if __name__ == "__main__":
    main()
