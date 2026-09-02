import argparse
import dataclasses
import json
import os
import sys
import typing as t
from pathlib import Path

import django

import analysis
import host
import measurement
import producer
import runner
from django_absurd.flush import truncate_queue_tables
from report import DEFAULT_RESULTS_DIR, describe_marks, format_dispersion

NOOP_ASYNC = "tasks.noop_async"
NOOP_SYNC = "tasks.noop_sync"
RUN_STEPS = "tasks.run_steps"
SLEEP_ASYNC = "tasks.sleep_async"
SLEEP_SYNC = "tasks.sleep_sync"

# Declared in dependency order: a stage that calibrates itself from another reads that
# one back off disk, so it is listed after it. Naming several stages runs them in this
# order whatever order they arrive in; a stage named alone whose prerequisite is missing
# still refuses rather than quietly running it. The dependency itself is written once,
# at the `read_*` call site that needs it — a second table naming the same edges is one
# more thing to keep in step, and this list is the only ordering anything reads.
STAGE_NAMES = (
    "worker_knobs",
    "process_scaling",
    "pooled_vs_split",
    "poll_interval",
    "sync_vs_async",
    "checkpoint_cost",
    "producer_ceiling",
    "latency_under_load",
)

STAGE_DESCRIPTIONS = {
    "worker_knobs": (
        "one worker's knobs: concurrency ladder, then batch size, then async dispatch"
    ),
    "process_scaling": (
        "throughput scaling across worker processes, at the winning worker config"
    ),
    "pooled_vs_split": (
        "one total concurrency reached two ways: slots in one process, or one slot in "
        "each of that many processes"
    ),
    "poll_interval": ("latency under a paced offer, plus idle claim-rate probes"),
    "sync_vs_async": "async vs sync task bodies at the same 50 ms of simulated IO",
    "checkpoint_cost": "checkpoint cost: a 4-step workflow against a flat task",
    "producer_ceiling": (
        "the producer's own ceiling: one connection, eight threads, batched commits"
    ),
    "latency_under_load": (
        "end-to-end latency at fractions of the measured process-scaling ceiling"
    ),
}

# Sized so the slowest worker_knobs measurement (concurrency 1, ~67 tasks/s) drains
# in about 75 s; throughput divides a p10-p90 window; a small backlog is mostly ramp.
SATURATION_TASKS = 5000
SATURATION_TIMEOUT_S = 900.0
# The rep count every stage runs at unless `--reps` says otherwise. Read back off the
# measurement default rather than restated, so a results file can record one rep count
# that is true of the whole run.
DEFAULT_REP_COUNT = measurement.MeasurementSpec.reps
RATE_OFFER_SECONDS = 60.0
HOST_CPUS = os.cpu_count() or 1
# A rate measurement's producer runs on the same box as its workers, so calibrating
# off the fastest saturation result asks for an offer it has no cores left to give.
RATE_WORKER_CAP = max(1, HOST_CPUS // 2)
RATE_TIMEOUT_S = 300.0
# Reps within 150 ms of each other are not called unstable however far apart they read
# relatively. A rate measurement ranks on `end_to_end_p50_s`, and every relative
# dispersion divides by that middle, so it RISES as the measurement gets faster.
RATE_SPREAD_FLOOR_S = 0.15
IDLE_PROBE_SECONDS = 30.0
# A rate is divided by its own window, so a zero-length one has nothing to report.
SMALLEST_MEASURABLE_DURATION_S = 0.001
IDLE_PROBE_WORKERS = 4
# The totals pooled_vs_split reaches both ways. Fixed rather than derived from the
# host, for the same reason the concurrency ladder's rungs are: a shape that means
# something different on every machine cannot be compared across machines.
POOLED_VS_SPLIT_TOTALS = (4, 8)
POLL_INTERVALS = (0.05, 0.25, 1.0)
# What the sleep tasks simulate as IO. The stage's finding is only true AT a duration,
# so it is the experiment's independent variable rather than a fixed property of it.
SLEEP_IO_SECONDS = 0.05
# ~25 s per rep at the slowest mode's ~200 enqueues/s: enough for stable percentiles
# while 3 reps x 3 modes still finish in a couple of minutes. The shared default rather
# than a third one, so one recorded rep count describes every stage.
PRODUCER_ENQUEUE_COUNT = 5000
PRODUCER_REP_COUNT = DEFAULT_REP_COUNT
# Read back off the measurement default rather than restated: one threshold decides
# what "unstable" means across the whole harness, whatever produced the reps.
PRODUCER_CV_LIMIT = measurement.MeasurementSpec.cv_limit
# The console has no legend under it, so the report's row marks are spelled out here
# rather than reprinted as punctuation nobody watching a 75-minute run can look up.
MARK_WORDS = {"!": "INVALID", "~": "UNSTABLE", "?": "DISPERSION UNMEASURED"}
# The 4-checkpoint task runs ~4.5x slower than a flat one, so SATURATION_TASKS would
# push a rep past two minutes; 2000 keeps checkpoint_cost on the same per-rep budget.
WORKFLOW_TASKS = 2000
# What every stage file this invocation writes records beside its options: a block per
# probe, each a median with its own CV and endpoints. The third is not the first
# repeated — a run leaves behind the bloat and the WAL churn it made, so only a closing
# probe says whether the ceiling its throughputs were read against still held at the
# end. All three are warmed probes: the 719/s once read here straight after a run was a
# cold session, not a depressed server.
COMMIT_CEILING_KEYS = (
    "commit_ceiling_durable",
    "commit_ceiling_nondurable",
    "commit_ceiling_durable_after",
)


class MissingStageError(Exception):
    def __init__(self, path: Path, required_stage: str) -> None:
        super().__init__(
            f"{path} is missing, and this stage is calibrated from it. "
            f"Run `python -m stages {required_stage}` first."
        )


class UncalibratableStageError(Exception):
    def __init__(self, stage_size: int) -> None:
        super().__init__(
            f"None of the {stage_size} recorded measurement(s) measured any "
            f"throughput, so there is no winning configuration to calibrate the next "
            f"stage from. "
            f"Re-run the earlier stage on a quiet machine and check its flags."
        )


class InvalidSizeError(Exception):
    def __init__(self, flag: str, value: float, floor: float) -> None:
        super().__init__(
            f"{flag} {value:g} is below {floor:g}, which leaves a stage nothing to "
            f"measure — no worker to spawn, no task to drain, or no window to divide "
            f"by. Every number it recorded would describe work that never happened."
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
    io_seconds: float | None = None
    # The size flags above bound the WORK; this one bounds the TOPOLOGY. Fleet size
    # otherwise tracks the host, so a 128-core box spawns hundreds of worker processes
    # across a stage with no way to ask for fewer.
    max_workers: int | None = None
    # Not a flag: the machine's own commit ceiling, measured once per invocation and
    # carried into every stage file this run writes. Mutable because the after-the-run
    # probe only exists once the stages are done, and it belongs in files already on
    # disk by then.
    commit_ceiling: dict[str, dict[str, float] | None] = dataclasses.field(
        default_factory=dict
    )


def run_stages(stage_names: list[str], options: StageOptions) -> None:
    options.results_dir.mkdir(parents=True, exist_ok=True)
    ordered = order_by_dependency(stage_names)
    # Before anything measures, and never between the reps of a measurement: the probe
    # commits a few thousand times of its own.
    options.commit_ceiling.update(
        {
            "commit_ceiling_durable": analysis.measure_commit_ceiling(durable=True),
            "commit_ceiling_nondurable": analysis.measure_commit_ceiling(durable=False),
        }
    )
    for name in ordered:
        run_stage(name, options)
    options.commit_ceiling["commit_ceiling_durable_after"] = (
        analysis.measure_commit_ceiling(durable=True)
    )
    record_commit_ceiling(ordered, options)


def record_commit_ceiling(stage_names: list[str], options: StageOptions) -> None:
    """Write the after-the-run ceiling into the files this invocation already wrote.

    A stage's file is rewritten after every measurement so a run killed at hour two
    keeps its work, and the closing probe does not exist until the last stage is done
    — so it lands last, into whatever those writes left on disk. A run that never got
    there leaves the field null, which is what happened.
    """
    for name in stage_names:
        path = options.results_dir / f"stage_{name}.json"
        write_results_file(
            path, {**json.loads(path.read_text()), **options.commit_ceiling}
        )


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
    if name == "worker_knobs":
        run_worker_knobs(options)
    elif name == "process_scaling":
        run_process_scaling(options)
    elif name == "pooled_vs_split":
        run_pooled_vs_split(options)
    elif name == "poll_interval":
        run_poll_interval(options)
    elif name == "sync_vs_async":
        record_measurements(
            "sync_vs_async",
            build_sync_vs_async_measurements(resolve_io_seconds(options)),
            [],
            options,
        )
    elif name == "checkpoint_cost":
        worker, calibration = read_winning_worker(options)
        record_measurements(
            "checkpoint_cost",
            build_checkpoint_cost_measurements(worker),
            [],
            options,
            {"calibration": calibration},
        )
    elif name == "producer_ceiling":
        run_producer_ceiling(options)
    else:
        run_latency_under_load(options)


def run_worker_knobs(options: StageOptions) -> None:
    """Claim amortization and concurrency scaling, then the async dispatch ratio."""
    recorded: list[dict[str, t.Any]] = []
    record_measurements(
        "worker_knobs", build_concurrency_measurements(), recorded, options
    )
    best = pick_best_measurement(recorded)
    calibration = {"calibration": describe_calibration("worker_knobs", best)}
    winner = runner.WorkerSpec(**best["spec"]["worker"])
    record_measurements(
        "worker_knobs",
        build_batch_size_measurements(winner),
        recorded,
        options,
        calibration,
    )
    record_measurements(
        "worker_knobs",
        build_async_dispatch_measurements(winner),
        recorded,
        options,
        calibration,
    )


def run_process_scaling(options: StageOptions) -> None:
    """How throughput scales with worker processes at the winning worker config."""
    worker, calibration = read_winning_worker(options)
    record_measurements(
        "process_scaling",
        build_process_scaling_measurements(worker, bound_fleet(HOST_CPUS, options)),
        [],
        options,
        {"calibration": calibration},
    )


def run_pooled_vs_split(options: StageOptions) -> None:
    """The same total concurrency reached two ways, arm against arm.

    Calibrated from nothing: both arms of a pair are the experiment's own fixed
    shapes, and configuring either from an earlier stage's winner would make them
    unequal in a second way.
    """
    runnable = [
        total
        for total in POOLED_VS_SPLIT_TOTALS
        if bound_fleet(total, options) >= total
    ]
    skipped = [
        {"total": total, "max_workers": bound_fleet(total, options)}
        for total in POOLED_VS_SPLIT_TOTALS
        if total not in runnable
    ]
    for pair in skipped:
        print(
            f"total {pair['total']}: not run, --max-workers {pair['max_workers']} "
            f"cannot spawn its {pair['total']}-process split arm"
        )
    specs = build_pooled_vs_split_measurements(runnable, options)
    reps: dict[str, list[dict[str, t.Any]]] = {spec.name: [] for spec in specs}
    # The list the loop below appends to, so every rewrite of the file records the
    # order the arms have run in so far rather than the order they were meant to.
    run_order: list[str] = []
    extra = {
        "shape_connections": [measure_shape_connections(spec) for spec in specs],
        "run_order": run_order,
        "skipped_pairs": skipped,
    }
    # Written before the first rep, so a bound that leaves nothing to compare still
    # leaves a file saying which pairs it refused and what refused them.
    write_stage_file("pooled_vs_split", [], options, extra)
    for arm in build_pooled_vs_split_schedule(specs):
        reps[arm.name].append(measurement.run_clean_rep(arm))
        run_order.append(arm.name)
        write_stage_file(
            "pooled_vs_split", summarize_pooled_vs_split(specs, reps), options, extra
        )
    for entry in summarize_pooled_vs_split(specs, reps):
        print(summarize_measurement(entry))


def run_poll_interval(options: StageOptions) -> None:
    """What poll_interval buys in latency and costs in idle transactions."""
    worker, calibration = read_winning_worker(options)
    recorded: list[dict[str, t.Any]] = []
    record_measurements(
        "poll_interval",
        build_poll_interval_measurements(worker),
        recorded,
        options,
        {"calibration": calibration},
    )
    probes = measure_idle_probes(
        worker,
        IDLE_PROBE_SECONDS if options.duration_s is None else options.duration_s,
        bound_fleet(IDLE_PROBE_WORKERS, options),
    )
    write_stage_file(
        "poll_interval",
        recorded,
        options,
        {"calibration": calibration, "idle_probes": probes},
    )


def run_producer_ceiling(options: StageOptions) -> None:
    """The producer's own ceiling: one connection, eight threads, batched commits."""
    recorded: list[dict[str, t.Any]] = []
    enqueues = PRODUCER_ENQUEUE_COUNT if options.tasks is None else options.tasks
    rep_count = PRODUCER_REP_COUNT if options.reps is None else options.reps
    for mode in ("single", "threaded", "atomic"):
        reps = []
        for _ in range(rep_count):
            truncate_queue_tables("bench")
            # On each side of the rep, for the reason in `host.read_load_average`.
            load_before = host.read_load_average()
            reps.append(
                {
                    **measure_producer_rep(mode, enqueues),
                    "load_before": load_before,
                    "load_after": host.read_load_average(),
                }
            )
        recorded.append(summarize_producer_reps(mode, reps))
        write_stage_file("producer_ceiling", recorded, options)
        median = recorded[-1]["median"]
        print(f"{mode}: {median.get('enqueues_per_s', 0.0):.1f} enqueues/s")


def measure_producer_rep(
    mode: t.Literal["single", "threaded", "atomic"],
    enqueues: int = PRODUCER_ENQUEUE_COUNT,
) -> dict[str, t.Any]:
    """One producer rep, bracketed like every other measured phase.

    Not because a nap deflates the numbers — perf_counter stops with the host — but so
    the producer stage refuses a slept-through rep rather than being the one that
    keeps it.
    """
    try:
        with host.measure_phase():
            metrics = producer.run_producer_benchmark(mode, enqueues)
    except host.SuspendedPhaseError as exc:
        return {"valid": False, "error": str(exc)}
    else:
        return {"valid": True, **metrics}


def run_latency_under_load(options: StageOptions) -> None:
    """End-to-end latency at fractions of the measured process-scaling ceiling."""
    worker, workers, ceiling, calibration = read_ceiling(options)
    record_measurements(
        "latency_under_load",
        [
            measurement.MeasurementSpec(
                name=f"rate_{int(fraction * 100)}pct",
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
        {"calibration": calibration},
    )


def build_concurrency_measurements() -> list[measurement.MeasurementSpec]:
    # The rungs are fixed while the worker ladder in process_scaling tracks the host,
    # so that a concurrency result means the same thing on every machine. Scale these
    # and the async-vs-sync ratio stops being comparable across hosts, which is the
    # only thing that ratio is for.
    return [
        measurement.MeasurementSpec(
            name=f"concurrency_{concurrency}",
            mode="saturation",
            task_path=NOOP_SYNC,
            worker=runner.WorkerSpec(concurrency=concurrency),
            tasks=SATURATION_TASKS,
            timeout_s=SATURATION_TIMEOUT_S,
        )
        for concurrency in (1, 2, 4, 8, 16)
    ]


def build_batch_size_measurements(
    winner: runner.WorkerSpec,
) -> list[measurement.MeasurementSpec]:
    return [
        measurement.MeasurementSpec(
            name=f"batch_{batch_size}",
            mode="saturation",
            task_path=NOOP_SYNC,
            worker=dataclasses.replace(winner, batch_size=batch_size),
            tasks=SATURATION_TASKS,
            timeout_s=SATURATION_TIMEOUT_S,
        )
        for batch_size in (1, 2 * winner.concurrency)
    ]


def build_async_dispatch_measurements(
    winner: runner.WorkerSpec,
) -> list[measurement.MeasurementSpec]:
    return [
        measurement.MeasurementSpec(
            name="async_dispatch",
            mode="saturation",
            task_path=NOOP_ASYNC,
            worker=winner,
            tasks=SATURATION_TASKS,
            timeout_s=SATURATION_TIMEOUT_S,
        )
    ]


def build_process_scaling_measurements(
    winner: runner.WorkerSpec, max_workers: int
) -> list[measurement.MeasurementSpec]:
    return [
        measurement.MeasurementSpec(
            name=f"workers_{count}",
            mode="saturation",
            task_path=NOOP_SYNC,
            worker=winner,
            workers=count,
            tasks=max(4000, 2000 * count),
            timeout_s=SATURATION_TIMEOUT_S,
        )
        for count in build_worker_ladder(max_workers)
    ]


def build_worker_ladder(ceiling: int) -> list[int]:
    """Worker counts process_scaling measures, topping out at ``ceiling``.

    The host's core count unless ``--max-workers`` asks for less. The steps are
    derived from the ceiling rather than clamped to it, so a bounded ladder is still a
    ladder instead of the same rung repeated.
    """
    # 1 and 2 anchor the low end where per-worker efficiency is still readable; the
    # quarter/half/three-quarter steps track the ceiling so the curve means the same
    # thing on any box, and nothing exceeds it.
    steps = {1, 2, ceiling // 4, ceiling // 2, ceiling * 3 // 4, ceiling}
    return sorted(step for step in steps if 1 <= step <= ceiling)


def build_pooled_vs_split_measurements(
    totals: list[int], options: StageOptions
) -> list[measurement.MeasurementSpec]:
    """Both shapes of each total, pooled arm first, at the same task count.

    Sized here rather than by `record_measurements`, which this stage cannot use: its
    reps are interleaved between the arms instead of run measurement by measurement.
    """
    return [
        apply_size_overrides(
            measurement.MeasurementSpec(
                name=f"{shape}_{total}",
                mode="saturation",
                task_path=NOOP_SYNC,
                worker=runner.WorkerSpec(concurrency=concurrency),
                workers=workers,
                tasks=SATURATION_TASKS,
                timeout_s=SATURATION_TIMEOUT_S,
            ),
            options,
        )
        for total in totals
        for shape, workers, concurrency in (
            ("pooled", 1, total),
            ("split", total, 1),
        )
    ]


def build_pooled_vs_split_schedule(
    specs: list[measurement.MeasurementSpec],
) -> list[measurement.MeasurementSpec]:
    """Every rep of every arm, in the order they run.

    Reversed on the odd reps: a pair's two arms stay back to back, while neither arm
    and neither pair ever always runs first. Cumulative database state only grows
    across a stage, so a fixed order would hand one arm of every pair the emptier
    tables — which is how an earlier control in this repo came to be invalidated.
    """
    rounds = specs[0].reps if specs else 0
    return [
        spec
        for index in range(rounds)
        for spec in (specs if index % 2 == 0 else specs[::-1])
    ]


def summarize_pooled_vs_split(
    specs: list[measurement.MeasurementSpec],
    reps: dict[str, list[dict[str, t.Any]]],
) -> list[dict[str, t.Any]]:
    """The arms that have a rep, in their canonical order however they were run."""
    return [
        measurement.summarize_reps(spec, reps[spec.name])
        for spec in specs
        if reps[spec.name]
    ]


def build_poll_interval_measurements(
    winner: runner.WorkerSpec,
) -> list[measurement.MeasurementSpec]:
    return [
        measurement.MeasurementSpec(
            name=f"poll_{poll_interval:g}",
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


def build_sync_vs_async_measurements(
    io_seconds: float,
) -> list[measurement.MeasurementSpec]:
    return [
        measurement.MeasurementSpec(
            name=f"{flavour}_c{concurrency}",
            mode="saturation",
            task_path=task_path,
            task_kwargs={"seconds": io_seconds},
            worker=runner.WorkerSpec(concurrency=concurrency),
            tasks=250 * concurrency,
            timeout_s=SATURATION_TIMEOUT_S,
        )
        for concurrency in (4, 16, 32)
        for flavour, task_path in (("async", SLEEP_ASYNC), ("sync", SLEEP_SYNC))
    ]


def build_checkpoint_cost_measurements(
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
        for name, task_path in (("flat", NOOP_SYNC), ("workflow", RUN_STEPS))
    ]


def measure_shape_connections(
    spec: measurement.MeasurementSpec,
) -> dict[str, t.Any]:
    """How many Postgres backends one shape opens, as a delta across starting it.

    A delta because the harness's own connections sit on the same database, and a
    count would credit them to whichever shape was measured. Sampled once the fleet
    reports ready and before any rep, so the probe's own queries stay outside every
    measured phase; a fleet already draining opens the same backends.
    """
    before = analysis.count_client_backends()
    procs = runner.start_workers(spec.worker, spec.workers)
    try:
        opened = analysis.count_client_backends() - before
    finally:
        runner.stop_workers(procs)
    print(
        f"{spec.name}: {opened} backends for {spec.workers} x {spec.worker.concurrency}"
    )
    return {
        "measurement": spec.name,
        "processes": spec.workers,
        "concurrency": spec.worker.concurrency,
        "connections": opened,
    }


def measure_idle_probes(
    winner: runner.WorkerSpec,
    seconds: float = IDLE_PROBE_SECONDS,
    workers: int = IDLE_PROBE_WORKERS,
) -> list[dict[str, t.Any]]:
    probes: list[dict[str, t.Any]] = []
    for poll_interval in POLL_INTERVALS:
        truncate_queue_tables(winner.queue)
        procs = runner.start_workers(
            dataclasses.replace(winner, poll_interval=poll_interval), workers
        )
        try:
            commits_per_s = analysis.measure_idle_commit_rate(seconds)
        finally:
            runner.stop_workers(procs)
        per_worker = commits_per_s / workers
        probes.append(
            {
                "poll_interval": poll_interval,
                "workers": workers,
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
    extra: dict[str, t.Any] | None = None,
) -> None:
    for spec in specs:
        recorded.append(
            measurement.run_measurement(apply_size_overrides(spec, options))
        )
        write_stage_file(stage, recorded, options, extra)
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


def bound_fleet(count: int, options: StageOptions) -> int:
    """How many worker processes a stage may spawn, after ``--max-workers``."""
    return count if options.max_workers is None else min(count, options.max_workers)


def write_stage_file(
    stage: str,
    recorded: list[dict[str, t.Any]],
    options: StageOptions,
    extra: dict[str, t.Any] | None = None,
) -> None:
    # Rewritten after every measurement so a run killed at hour two keeps everything.
    write_results_file(
        options.results_dir / f"stage_{stage}.json",
        {
            "stage": stage,
            "options": resolve_options(options),
            # The three keys always, at null until a probe fills them: a file that
            # simply omitted them would read as one written before the ceiling was
            # recorded rather than as a run whose probe was refused.
            **dict.fromkeys(COMMIT_CEILING_KEYS),
            **options.commit_ceiling,
            "measurements": recorded,
            **(extra or {}),
        },
    )


def write_results_file(path: Path, payload: dict[str, t.Any]) -> None:
    staged = path.with_suffix(".json.tmp")
    staged.write_text(json.dumps(payload, indent=2) + "\n")
    staged.replace(path)


def resolve_options(options: StageOptions) -> dict[str, t.Any]:
    """What the flags came out as, so a results file says which run produced it.

    Beside the per-measurement `host` block rather than inside it: one results file is
    one run of one stage, so its flags are the same for every measurement in it, while
    load average and uptime are not.

    Resolved, so an unset flag records what it fell back to instead of a null the
    reader has to go look up — `--max-workers` most of all, since its default is the
    host's core count and a bounded run on a big box otherwise reads exactly like an
    unbounded run on a small one. `--tasks` and `--duration` are the two with no single
    default to resolve to: a stage's production size is its own (5000 no-ops here, 2000
    workflows there; a 60 s offer, a 30 s idle probe), and every measurement records
    the size it actually ran at in its own spec. `results_dir` is where the file went,
    which the file's own location already says.
    """
    return {
        "duration_s": options.duration_s,
        "io_seconds": resolve_io_seconds(options),
        "max_workers": bound_fleet(HOST_CPUS, options),
        "reps": DEFAULT_REP_COUNT if options.reps is None else options.reps,
        "tasks": options.tasks,
    }


def resolve_io_seconds(options: StageOptions) -> float:
    """Read by the stage that sleeps and by the record of what it slept for, so the
    two cannot drift into disagreeing about the same run."""
    return SLEEP_IO_SECONDS if options.io_seconds is None else options.io_seconds


def summarize_measurement(result: dict[str, t.Any]) -> str:
    median = result["median"]
    # A saturation run starts with a full queue, so every task but the first waited
    # behind the whole backlog: its percentiles are drain time wearing latency's name.
    # The report's table drops them for the same reason.
    latency = (
        f"e2e p50 {median.get('end_to_end_p50_s', 0.0) * 1000:.1f}ms, "
        if result["spec"]["mode"] == "rate"
        else ""
    )
    line = (
        f"{result['spec']['name']}: "
        f"{median.get('throughput_per_s', 0.0):.1f} tasks/s, "
        f"{latency}"
        f"spread {format_dispersion(result['spread'])}, "
        f"cv {format_dispersion(result['cv'])}"
    )
    marks = " ".join(MARK_WORDS[mark] for mark in describe_marks(result).split())
    return f"{line} [{marks}]" if marks else line


def summarize_producer_reps(
    mode: str, reps: list[dict[str, t.Any]]
) -> dict[str, t.Any]:
    valid = sorted(
        (rep for rep in reps if rep["valid"]), key=lambda rep: rep["enqueues_per_s"]
    )
    median: dict[str, t.Any] = valid[(len(valid) - 1) // 2] if valid else {}
    # The shared helper rather than a second copy of the arithmetic: this stage had
    # its own, so the "one rep has no spread" guard had to be fixed twice.
    cv = measurement.measure_cv(valid, "enqueues_per_s")
    low, high = measurement.measure_rep_range(valid, "enqueues_per_s")
    return {
        "spec": {"name": mode, "mode": "producer"},
        "reps": reps,
        "ranking_key": "enqueues_per_s",
        "median": median,
        "spread": measurement.measure_spread(valid, median, "enqueues_per_s"),
        "cv": cv,
        "range_low": low,
        "range_high": high,
        # No absolute floor here: the producer measures one number in one unit at one
        # size, so there is no fast-measurement case for a floor to rescue.
        "invalid": measurement.is_measurement_invalid(reps, valid),
        "unstable": cv is not None and cv > PRODUCER_CV_LIMIT,
        "host": host.collect_host_context(),
    }


def read_winning_worker(
    options: StageOptions,
) -> tuple[runner.WorkerSpec, dict[str, t.Any]]:
    best = pick_best_measurement(read_stage_measurements(options, "worker_knobs"))
    return (
        runner.WorkerSpec(**best["spec"]["worker"]),
        describe_calibration("worker_knobs", best),
    )


def read_ceiling(
    options: StageOptions,
) -> tuple[runner.WorkerSpec, int, float, dict[str, t.Any]]:
    recorded = read_stage_measurements(options, "process_scaling")
    best = pick_rate_calibration_measurement(
        recorded, bound_fleet(RATE_WORKER_CAP, options)
    )
    return (
        runner.WorkerSpec(**best["spec"]["worker"]),
        best["spec"]["workers"],
        best["median"]["throughput_per_s"],
        describe_calibration("process_scaling", best),
    )


def describe_calibration(stage: str, best: dict[str, t.Any]) -> dict[str, t.Any]:
    """Which measurement became the working point, recorded where it is inherited.

    The choice itself is right — a valid, stable rung is what a later stage should be
    configured at — but nothing downstream could see it had been made. When most rungs
    are marked, calibration lands on the slowest survivor and every stage after it
    reads as though that were the machine's best, so what was chosen and how well it
    repeated travel with the stage that inherited it.
    """
    return {
        "stage": stage,
        "measurement": best["spec"]["name"],
        "throughput_per_s": best["median"].get("throughput_per_s", 0.0),
        "cv": best["cv"],
        "invalid": best["invalid"],
        "unstable": best["unstable"],
    }


def read_stage_measurements(
    options: StageOptions, stage: str
) -> list[dict[str, t.Any]]:
    path = options.results_dir / f"stage_{stage}.json"
    if not path.exists():
        raise MissingStageError(path, stage)
    return t.cast(
        "list[dict[str, t.Any]]", json.loads(path.read_text())["measurements"]
    )


def pick_best_measurement(recorded: list[dict[str, t.Any]]) -> dict[str, t.Any]:
    # A working point, not a report: preferring a measurement that measured what was
    # asked and repeated is right here, and nothing is dropped from any table by it.
    # Calibrating on nothing is worse than calibrating on a noisy best, so the rest are
    # the fallback rather than an error.
    candidates = [
        entry for entry in recorded if not (entry["invalid"] or entry["unstable"])
    ] or recorded
    best = max(
        candidates, key=lambda entry: entry["median"].get("throughput_per_s", 0.0)
    )
    # A zero-throughput winner calibrates every stage reading it back on nothing at all.
    if best["median"].get("throughput_per_s", 0.0) <= 0:
        raise UncalibratableStageError(len(recorded))
    return best


def pick_rate_calibration_measurement(
    recorded: list[dict[str, t.Any]], worker_cap: int
) -> dict[str, t.Any]:
    """Pick what a rate stage calibrates from, leaving the producer some cores."""
    # Capping the SELECTION rather than the chosen fleet keeps the offered rate and the
    # workers asked to absorb it the same measurement; clamping afterwards would aim a
    # smaller fleet at a bigger fleet's ceiling. No fallback to the uncapped set: every
    # ladder anchors at one worker and every cap is at least one, so a rung under the
    # cap always exists.
    capped = [entry for entry in recorded if entry["spec"]["workers"] <= worker_cap]
    return pick_best_measurement(capped)


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
    parser.add_argument(
        "--max-workers",
        default=None,
        type=int,
        help=(
            "Most worker processes any one stage may spawn at once, at least 1: "
            "bounds the process_scaling ladder, the poll_interval idle probes, and the "
            "fleet latency_under_load calibrates from, and drops whole any "
            "pooled_vs_split pair whose split arm it cannot spawn. Unset, fleet size "
            f"tracks the host's core count ({HOST_CPUS} here)."
        ),
    )
    parser.add_argument(
        "--io-seconds",
        default=None,
        type=float,
        help=(
            "Seconds of simulated IO the sync_vs_async workloads sleep for "
            f"(default: {SLEEP_IO_SECONDS:g}); no other stage sleeps."
        ),
    )
    args = parser.parse_args(argv)
    stages = args.stages or list(STAGE_NAMES)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
    django.setup()
    # All three are the caller's to fix — a size nothing could measure, a stage named
    # before its prerequisite, or one whose measurements came back empty — so they
    # print as errors, not as crashes.
    try:
        run_stages(stages, build_stage_options(args))
    except (
        InvalidSizeError,
        MissingStageError,
        UncalibratableStageError,
    ) as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)


def build_stage_options(args: argparse.Namespace) -> StageOptions:
    """The parsed flags as one stage's options, refusing a size nothing could measure.

    Each of these goes wrong in its own way downstream and none of them announces it:
    an empty ladder writes no results file while reporting success, a negative fleet
    divides an idle probe's claim rate into a rate no worker produced, no tasks leaves
    a percentile with nothing to sort, and a zero-second probe divides by its own
    duration.
    """
    for flag, value, floor in (
        ("--max-workers", args.max_workers, 1),
        ("--tasks", args.tasks, 1),
        ("--duration", args.duration, SMALLEST_MEASURABLE_DURATION_S),
    ):
        if value is not None and value < floor:
            raise InvalidSizeError(flag, value, floor)
    return StageOptions(
        results_dir=args.results_dir,
        reps=args.reps,
        tasks=args.tasks,
        duration_s=args.duration,
        io_seconds=args.io_seconds,
        max_workers=args.max_workers,
    )


if __name__ == "__main__":
    main()
