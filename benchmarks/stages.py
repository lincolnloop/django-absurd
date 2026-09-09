import argparse
import contextlib
import dataclasses
import datetime as dt
import json
import os
import sys
import time
import typing as t
from pathlib import Path

import django
from django.conf import settings
from django.db import connections
from django.utils import timezone
from django.utils.module_loading import import_string

import analysis
import host
import measurement
import producer
import runner
import seed
from django_absurd import cleanup
from django_absurd.flush import truncate_queue_tables
from django_absurd.queues import resolve_absurd_database
from report import (
    DEFAULT_RESULTS_DIR,
    THROUGHPUT_KEY,
    describe_marks,
    format_dispersion,
)

DURABLE_STEPS = "tasks.run_durable_steps"
DURABLE_WORK = "tasks.run_durable_work"
NOOP_ASYNC = "tasks.noop_async"
NOOP_SYNC = "tasks.noop_sync"
RUN_STEPS = "tasks.run_steps"
SLEEP_ASYNC = "tasks.sleep_async"
SLEEP_SYNC = "tasks.sleep_sync"

# Dependency order, which is the order several named stages run in; the edges are
# written once each, at the `read_*` call site that needs one.
STAGE_NAMES = (
    "worker_knobs",
    "process_scaling",
    "pooled_vs_split",
    "size_vs_depth",
    "poll_interval",
    "sync_vs_async",
    "checkpoint_cost",
    "durable_checkpoints",
    "cleanup_vs_size",
    "batch_barrier",
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
        "each of that many processes, on a nano-task body and on a durable one"
    ),
    "size_vs_depth": (
        "one pending depth drained on three sizes of table: empty, ballasted with "
        "finished work, and ballasted then vacuumed"
    ),
    "poll_interval": ("latency under a paced offer, plus idle claim-rate probes"),
    "sync_vs_async": "async vs sync task bodies at the same 50 ms of simulated IO",
    "checkpoint_cost": "checkpoint cost: a 4-step workflow against a flat task",
    "durable_checkpoints": (
        "what a checkpoint costs inside a durable body: three step depths, each on a "
        "body held for --durable-seconds and again on one held fifteen times as long"
    ),
    "cleanup_vs_size": (
        "what one shipped cleanup call costs, at the default batch size on a table "
        "and on one four times longer"
    ),
    "batch_barrier": (
        "what a batch claim's barrier costs on a backlog of uneven task lengths, "
        "against a uniform control carrying the same total service time"
    ),
    "producer_ceiling": (
        "the producer's own ceiling: one connection, eight threads, batched commits"
    ),
    "latency_under_load": (
        "end-to-end latency at fractions of a measured sustainable offer rate"
    ),
}

# The depth every rung sharing a table is measured at, so `--tasks` is a comparability
# key rather than a size: 20,000 was measured and REFUSED — it moves the medians
# 0.42-0.60x on depth alone, leaves the cross-rep CV inside the bracket 5,000 already
# spanned while making the within-rep profile noisier, and costs up to 10x the wall
# clock. See `benchmarks/CLAUDE.md`.
SATURATION_TASKS = 5000
SATURATION_TIMEOUT_S = 900.0
# Read back off the measurement default rather than restated, so a results file
# records one rep count that is true of the whole run.
DEFAULT_REP_COUNT = measurement.MeasurementSpec.reps
RATE_OFFER_SECONDS = 60.0
HOST_CPUS = os.cpu_count() or 1
# A rate measurement's producer runs on the same box as its workers, so calibrating
# off the fastest saturation result asks for an offer it has no cores left to give.
RATE_WORKER_CAP = max(1, HOST_CPUS // 2)
RATE_TIMEOUT_S = 300.0
# What `latency_under_load` offers, as fractions of the sustainable rate its own ramp
# measured — never of a drain rate, which is a different quantity entirely.
RATE_FRACTIONS = (0.25, 0.50, 0.75, 0.90)
# The ramp that finds that rate: climb from a tenth of the drain ceiling by half again
# a step, and stop at the first offer the fleet could not absorb. A drain rate is the
# most any fleet could ever complete, so it caps the climb.
RATE_RAMP_START_FRACTION = 0.10
RATE_RAMP_STEP = 1.5
# Long enough that a queue falling behind has visibly fallen behind by the midpoint,
# short enough that a whole ramp costs a couple of minutes. One rep each: a probe
# picks a working point, and every rung it sizes is measured at `--reps`.
RATE_RAMP_SECONDS = 20.0
# Reps this close are not called unstable however far apart they read relatively: a
# rate measurement ranks on a latency, which every relative dispersion divides by.
# 10 ms and not the 150 ms this started at: `latency_under_load`'s rungs measure p50s of
# 9-30 ms and `poll_0.05` 39-42 ms, so a floor above their whole healthy range put `~`
# out of reach of every one of them and an unmarked rate table said nothing. Under the
# smallest rung's own p50, so a rep that genuinely lurched still clears it.
RATE_SPREAD_FLOOR_S = 0.010
IDLE_PROBE_SECONDS = 30.0
# A rate is divided by its own window, so a zero-length one has nothing to report.
SMALLEST_MEASURABLE_DURATION_S = 0.001
IDLE_PROBE_WORKERS = 4
# The totals pooled_vs_split reaches both ways. Fixed rather than derived from the
# host: a shape that differs by machine cannot be compared across machines.
POOLED_VS_SPLIT_TOTALS = (4, 8)
POLL_INTERVALS = (0.05, 0.25, 1.0)
# Finished tasks a size_vs_depth arm leaves in the tables before its own preload, as a
# multiple of the depth it then measures. 3 makes the ballasted arms' tables four
# times the fresh one's — the same ratio as the 5,000-against-20,000 comparison that
# cost 2.45x and moved both size and depth at once, with depth now held still.
SIZE_BALLAST_MULTIPLE = 3
# What the sleep tasks simulate as IO — the experiment's independent variable, since
# its finding is only ever true AT a duration.
SLEEP_IO_SECONDS = 0.05
# How long a durable body holds its worker thread. The floor of the regime it stands
# for, not the middle of it: every durable rep costs its arm's tasks over its slots
# times this, so a full run's wall clock scales straight off it. Thirty seconds is the
# same experiment at an agent tool call's real duration, at fifteen times the bill.
DURABLE_SECONDS = 2.0
# Rounds of durable work each slot runs in a rep, which is what sizes those arms: a
# fixed task count would run for minutes at one shape and seconds at another.
DURABLE_ROUNDS_PER_SLOT = 8
# What durable_checkpoints multiplies `--durable-seconds` by for its long-body arms,
# so one flag sets both lengths and the pair always spans the same ratio.
LONG_DURABLE_MULTIPLE = 15
# Checkpoints per durable body, in the order the arms run; 0 is the control every
# per-step cost is subtracted from.
DURABLE_STEP_DEPTHS = (0, 4, 40)
# Rounds of durable work per slot in a durable_checkpoints rep. Fewer than
# `DURABLE_ROUNDS_PER_SLOT` because six arms at fifteen times the floor is this
# stage's whole wall clock; see `benchmarks/CLAUDE.md`.
DURABLE_CHECKPOINT_ROUNDS_PER_SLOT = 2
# How often the connection probe reads `pg_stat_activity` while a fleet works. Each
# read is a query on the harness's own connection, so this is a sampling rate rather
# than a cost the fleet pays.
BACKEND_SAMPLE_INTERVAL_S = 0.05
# ~25 s per rep at the slowest mode's rate: enough for stable percentiles while
# 3 reps x 3 modes still finish in a couple of minutes.
PRODUCER_ENQUEUE_COUNT = 5000
# The shared defaults rather than a third pair, so one rep count and one instability
# threshold describe every stage, whatever produced the reps.
PRODUCER_REP_COUNT = DEFAULT_REP_COUNT
PRODUCER_CV_LIMIT = measurement.MeasurementSpec.cv_limit
# The console has no legend under it, so the report's row marks are spelled out rather
# than printed as punctuation nobody watching a 75-minute run can look up.
MARK_WORDS = {"!": "INVALID", "~": "UNSTABLE", "?": "DISPERSION UNMEASURED"}
# A 4-checkpoint task runs ~4.5x slower than a flat one, so SATURATION_TASKS would push
# a rep past two minutes; this keeps checkpoint_cost on the same per-rep budget.
WORKFLOW_TASKS = 2000
# What cleanup_vs_size seeds, and the arms it then measures: a name, the multiple of
# that seed the arm's table holds, and the `cleanup_limit` its queue is set to. The 4x
# ratio is `SIZE_BALLAST_MULTIPLE`'s, so the two stages' size findings compare.
#
# 250,000 and not a million, because the server's data directory is a 4 GB tmpfs and a
# million tasks is already 1.09 GB of tables — 4x of that fills it and the seed dies
# mid-clone. The finding is the RATIO across the step, which travels where the
# milliseconds do not.
#
# One batch size only. A larger `cleanup_limit` cannot be measured honestly here: six
# calls at 100,000 delete 60% of a million-row table, so the arm's own per-call figure
# would average a table shrinking underneath it, where 1,000 erodes 0.6% and holds the
# size still. Measuring it needs a table this rig has no room for.
CLEANUP_SEED_ROWS = 250_000
CLEANUP_ARMS = (
    ("limit1k_1x", 1, 1000),
    ("limit1k_4x", 4, 1000),
)
# Timed calls per rep, after one warm-up call that is recorded but kept out of the
# rates: the first call of a session pays for plans and cache misses the rest do not,
# and this stage's finding is what a call costs in the steady state. Recorded rather
# than discarded so that a rep whose table was small enough for the warm-up to empty
# still shows the deletions that prove the clock shift reached the rows.
CLEANUP_TIMED_CALLS = 5
# How far past the queue's own `cleanup_ttl` the clock moves, which is what makes a
# freshly seeded row eligible at all.
CLEANUP_CLOCK_MARGIN = dt.timedelta(days=1)
# The backlog batch_barrier drains. Mostly fast tasks with a few slow ones, at
# `loadtest`'s own proportions: one slow task every 21 is rare enough that most batches
# are clean and frequent enough that the stalls accumulate. The uniform control carries
# the same task count and the same total service time at the mixed mean, so the two
# differ in variance alone.
BARRIER_FAST_TASKS = 400
BARRIER_SLOW_TASKS = 20
BARRIER_FAST_SECONDS = 0.01
BARRIER_SLOW_SECONDS = 1.0
# One worker at four slots: the barrier is a property of a POOLED worker, whose batch
# size defaults to its concurrency, so C slots wait on the slowest of C.
BARRIER_CONCURRENCY = 4
# The three probe blocks every stage file this invocation writes records beside its
# options; only the closing one says whether the ceiling still held at the end.
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


class MeasuringUnderDebugError(Exception):
    def __init__(self) -> None:
        super().__init__(
            "DEBUG is on, so every query would run through Django's debug cursor and "
            "no rate measured under it compares with one measured without it. Unset "
            "DEBUG — it is there to serve the seeded admin, not to measure."
        )


@dataclasses.dataclass(frozen=True)
class StageOptions:
    results_dir: Path
    reps: int | None = None
    # Override a stage's production size, so the suite can drive one end to end in
    # seconds. Two flags, because the two modes are sized in different units.
    tasks: int | None = None
    duration_s: float | None = None
    io_seconds: float | None = None
    durable_seconds: float | None = None
    # The size flags above bound the WORK; this one bounds the TOPOLOGY, which
    # otherwise tracks the host — a 128-core box spawns hundreds of workers a stage.
    max_workers: int | None = None
    # Not a flag: the machine's own commit ceiling, carried into every stage file this
    # run writes. Mutable because the closing probe lands after those files exist.
    commit_ceiling: dict[str, dict[str, t.Any]] = dataclasses.field(
        default_factory=dict
    )


def run_stages(stage_names: list[str], options: StageOptions) -> None:
    options.results_dir.mkdir(parents=True, exist_ok=True)
    ordered = order_by_dependency(stage_names)
    # Ahead of every measurement, because a rep can only itemise what something was
    # already counting, and a RAM data directory loses the extension on every restart.
    analysis.install_statement_stats()
    # Never between the reps of a measurement: the probe commits a few thousand times
    # of its own.
    options.commit_ceiling.update(
        {
            "commit_ceiling_durable": analysis.measure_commit_ceiling(durable=True),
            "commit_ceiling_nondurable": analysis.measure_commit_ceiling(durable=False),
            # Every file carries this from its first write, so a run killed at any
            # point says it never took a closing probe rather than reading as a run
            # whose server refused one.
            "commit_ceiling_durable_after": analysis.UNPROBED_COMMIT_CEILING,
        }
    )
    try:
        for name in ordered:
            run_stage(name, options)
    except Exception:
        # A stage that raised leaves its session intact, unlike an interrupted wait,
        # so the stages that did finish still get the ceiling they were read against.
        record_closing_commit_ceiling(ordered, options)
        raise
    record_closing_commit_ceiling(ordered, options)


def record_closing_commit_ceiling(
    stage_names: list[str], options: StageOptions
) -> None:
    """Probe the ceiling once more and write it into the files this invocation wrote.

    Last, because the closing probe does not exist until the stages are over — and it
    says whether the ceiling those stages were read against still held.
    """
    options.commit_ceiling["commit_ceiling_durable_after"] = (
        analysis.measure_commit_ceiling(durable=True)
    )
    for name in stage_names:
        path = options.results_dir / f"stage_{name}.json"
        # A stage that raised before writing anything has no file to update, and a
        # FileNotFoundError raised here would replace the failure that got us here.
        if path.exists():
            write_results_file(
                path, {**json.loads(path.read_text()), **options.commit_ceiling}
            )


def order_by_dependency(stage_names: list[str]) -> list[str]:
    """Sort the requested stages so a prerequisite runs before what calibrates on it.

    Only orders what was asked for, never adding a missing prerequisite: a stage that
    cannot calibrate says so rather than running a second stage nobody asked for.
    """
    requested = set(stage_names)
    return [name for name in STAGE_NAMES if name in requested]


def run_stage(name: str, options: StageOptions) -> None:
    print(f"stage {name.upper()}: {STAGE_DESCRIPTIONS[name]}")
    runners: dict[str, t.Callable[[StageOptions], None]] = {
        "worker_knobs": run_worker_knobs,
        "process_scaling": run_process_scaling,
        "pooled_vs_split": run_pooled_vs_split,
        "size_vs_depth": run_size_vs_depth,
        "poll_interval": run_poll_interval,
        "sync_vs_async": run_sync_vs_async,
        "checkpoint_cost": run_checkpoint_cost,
        "durable_checkpoints": run_durable_checkpoints,
        "cleanup_vs_size": run_cleanup_vs_size,
        "batch_barrier": run_batch_barrier,
        "producer_ceiling": run_producer_ceiling,
        "latency_under_load": run_latency_under_load,
    }
    runners[name](options)


def run_size_vs_depth(options: StageOptions) -> None:
    """One pending depth drained on three sizes of table."""
    record_measurements(
        "size_vs_depth", build_size_vs_depth_measurements(), [], options
    )


def run_sync_vs_async(options: StageOptions) -> None:
    """Async against sync bodies at the same simulated IO."""
    record_measurements(
        "sync_vs_async",
        build_sync_vs_async_measurements(resolve_io_seconds(options)),
        [],
        options,
    )


def run_checkpoint_cost(options: StageOptions) -> None:
    """A 4-step workflow against a flat task, at the winning worker config."""
    worker, calibration = read_winning_worker(options)
    record_measurements(
        "checkpoint_cost",
        build_checkpoint_cost_measurements(worker),
        [],
        options,
        {"calibration": calibration},
    )


def run_durable_checkpoints(options: StageOptions) -> None:
    """Three step depths on two body lengths, every arm's reps interleaved."""
    worker, calibration = read_winning_worker(options)
    record_interleaved_measurements(
        "durable_checkpoints",
        build_durable_checkpoint_measurements(worker, options),
        options,
        {"calibration": calibration},
    )


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

    Calibrated from nothing: configuring either arm from an earlier stage's winner
    would make the pair unequal in a second way.
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
    record_interleaved_measurements(
        "pooled_vs_split",
        specs,
        options,
        {
            "shape_connections": measure_shape_connections(
                specs, resolve_durable_seconds(options)
            ),
            "skipped_pairs": skipped,
        },
    )


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


def run_cleanup_vs_size(options: StageOptions) -> None:
    """What one shipped cleanup call costs, and whether the table it scans sets it.

    No fleet: `absurd.cleanup_tasks` scans the whole tasks table for terminal rows
    older than the cutoff, so what it costs is a property of the table rather than of
    anything a worker is doing. Whether it also costs a live fleet is a different
    experiment and not this one.
    """
    recorded: list[dict[str, t.Any]] = []
    base_rows = CLEANUP_SEED_ROWS if options.tasks is None else options.tasks
    rep_count = DEFAULT_REP_COUNT if options.reps is None else options.reps
    for name, multiple, limit in CLEANUP_ARMS:
        rows = base_rows * multiple
        reps = []
        for _ in range(rep_count):
            # Reseeded per REP, not per arm: a cleanup rep deletes what it measures,
            # so the second rep of an arm would read a table the first one shrank.
            seed.seed_queue_tables(rows, queue=seed.DEFAULT_QUEUE)
            set_cleanup_limit(seed.DEFAULT_QUEUE, limit)
            load_before = host.read_load_average()
            reps.append(
                {
                    **measure_cleanup_rep(seed.DEFAULT_QUEUE),
                    "load_before": load_before,
                    "load_after": host.read_load_average(),
                }
            )
        recorded.append(summarize_cleanup_reps(name, rows, limit, reps))
        write_stage_file("cleanup_vs_size", recorded, options)
        print(summarize_cleanup_arm(recorded[-1]))


def measure_cleanup_rep(queue: str) -> dict[str, t.Any]:
    """One rep: a recorded warm-up call, then `CLEANUP_TIMED_CALLS` timed ones.

    Bracketed like every other measured phase, so a rep the host slept through is
    refused rather than recorded as a fast one.
    """
    table = analysis.refresh_table_state(queue)
    try:
        with host.measure_phase(), shift_clock_past_cleanup_ttl(queue):
            warm_up = measure_one_cleanup_call(queue)
            calls = [
                measure_one_cleanup_call(queue) for _ in range(CLEANUP_TIMED_CALLS)
            ]
    except host.SuspendedPhaseError as exc:
        return {"valid": False, "error": str(exc)}
    else:
        return {
            "valid": True,
            "table": table,
            "warm_up": warm_up,
            "calls": calls,
            **read_call_rates(calls),
        }


def measure_one_cleanup_call(queue: str) -> dict[str, t.Any]:
    """One `cleanup_queues` call — the shipped path the command and the cron job take
    — and what it deleted."""
    started = time.perf_counter()
    rows = cleanup.cleanup_queues([queue])
    elapsed_ms = 1000.0 * (time.perf_counter() - started)
    return {
        "ms": elapsed_ms,
        "tasks_deleted": rows[0]["tasks_deleted"],
        "events_deleted": rows[0]["events_deleted"],
    }


def read_call_rates(calls: list[dict[str, t.Any]]) -> dict[str, t.Any]:
    """The timed calls as rates. `deletes_per_s` ranks the reps because it is the one
    figure here that is better high, which is what `pick_median_rep` assumes."""
    durations = [call["ms"] for call in calls]
    tasks_deleted = sum(call["tasks_deleted"] for call in calls)
    elapsed_s = sum(durations) / 1000.0
    return {
        "tasks_deleted": tasks_deleted,
        "events_deleted": sum(call["events_deleted"] for call in calls),
        "elapsed_s": elapsed_s,
        "deletes_per_s": tasks_deleted / elapsed_s if elapsed_s else 0.0,
        "ms_per_call_p50": producer.read_percentile(durations, 0.50),
        "ms_per_call_p99": producer.read_percentile(durations, 0.99),
    }


@contextlib.contextmanager
def shift_clock_past_cleanup_ttl(queue: str) -> t.Iterator[None]:
    """Move THIS session's `absurd.current_time()` past the queue's `cleanup_ttl`.

    A seeded row is seconds old against a TTL of thirty days, so nothing is eligible
    until the clock moves — and moving it costs nothing, where backdating a million
    rows would rewrite the table the arm is sized on. Session-scoped and reset on the
    way out: `alter database ... set absurd.fake_now` would outlive the stage and
    leave every later claim in the run unreachable.
    """
    ttl = read_cleanup_ttl(queue)
    write_fake_now((timezone.now() + ttl + CLEANUP_CLOCK_MARGIN).isoformat())
    try:
        yield
    finally:
        write_fake_now("")


def write_fake_now(instant: str) -> None:
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute("select set_config('absurd.fake_now', %s, false)", [instant])


def read_cleanup_ttl(queue: str) -> dt.timedelta:
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute(
            "select cleanup_ttl from absurd.queues where queue_name = %s", [queue]
        )
        return t.cast("dt.timedelta", cursor.fetchone()[0])


def set_cleanup_limit(queue: str, limit: int) -> None:
    """The arm's batch size, written where `absurd.cleanup_all_queues` reads it from.

    The queue policy rather than a direct `cleanup_tasks(queue, ttl, limit)` call, so
    every arm goes through the path the shipped command and the cron job take. Raw SQL
    rather than the `Queue` model: importing `django_absurd.models` here would ask for
    the app registry at import time, and this module is imported before `django.setup`.
    """
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute(
            "update absurd.queues set cleanup_limit = %s where queue_name = %s",
            [limit, queue],
        )


def summarize_cleanup_reps(
    name: str, rows: int, limit: int, reps: list[dict[str, t.Any]]
) -> dict[str, t.Any]:
    valid = sorted(
        (rep for rep in reps if rep["valid"]), key=lambda rep: rep["deletes_per_s"]
    )
    median = measurement.pick_median_rep(valid, "deletes_per_s")
    cv = measurement.measure_cv(valid, "deletes_per_s")
    low, high = measurement.measure_rep_range(valid, "deletes_per_s")
    return {
        "spec": {
            "name": name,
            "mode": "cleanup",
            "rows": rows,
            "cleanup_limit": limit,
        },
        "reps": reps,
        "ranking_key": "deletes_per_s",
        "median": median,
        "spread": measurement.measure_spread(valid, median, "deletes_per_s"),
        "cv": cv,
        "range_low": low,
        "range_high": high,
        "invalid": measurement.is_measurement_invalid(reps, valid),
        "unstable": cv is not None and cv > measurement.MeasurementSpec.cv_limit,
        "host": host.collect_host_context(),
    }


def summarize_cleanup_arm(entry: dict[str, t.Any]) -> str:
    median = entry["median"]
    return (
        f"{entry['spec']['name']}: {median.get('ms_per_call_p50', 0.0):.1f} ms/call, "
        f"{median.get('deletes_per_s', 0.0):.0f} deletes/s, "
        f"{entry['spec']['rows']} rows"
    )


def run_batch_barrier(options: StageOptions) -> None:
    """What a batch claim's barrier costs when a backlog's tasks differ in length.

    The async loop claims `batch_size` tasks — defaulting to concurrency — launches
    them all and gathers before claiming again, so C slots wait on the slowest of C.
    A uniform backlog hides that completely: waiting for the slowest of C costs nothing
    when every task is the same length. So the mixed arm is measured against a uniform
    control of the same task count and the same total service time.

    Its own rep rather than `measurement.run_saturation_rep`, because the backlog is
    the experiment: the slow tasks have to be SPREAD through the claim order, which a
    threaded single-path preload cannot express.
    """
    arms = build_barrier_arms(options)
    rep_count = DEFAULT_REP_COUNT if options.reps is None else options.reps
    reps: dict[str, list[dict[str, t.Any]]] = {arm["name"]: [] for arm in arms}
    run_order: list[str] = []
    extra = {"run_order": run_order}
    write_stage_file("batch_barrier", [], options, extra)
    for index in range(rep_count):
        # Reversed on the odd reps, so neither arm always meets the emptier tables.
        for arm in arms if index % 2 == 0 else list(reversed(arms)):
            reps[arm["name"]].append(measure_barrier_rep(arm))
            run_order.append(arm["name"])
            write_stage_file(
                "batch_barrier", summarize_barrier_reps(arms, reps), options, extra
            )
    for entry in summarize_barrier_reps(arms, reps):
        print(summarize_barrier_arm(entry))


def build_barrier_arms(options: StageOptions) -> list[dict[str, t.Any]]:
    """The uniform control and the mixed backlog, at equal total service time.

    Both scale with `--tasks` at the fixed proportion, so a smoke run measures the
    same experiment as a production one.
    """
    total = BARRIER_FAST_TASKS + BARRIER_SLOW_TASKS
    tasks = total if options.tasks is None else options.tasks
    slow_tasks = max(1, tasks * BARRIER_SLOW_TASKS // total)
    fast_tasks = tasks - slow_tasks
    service_seconds = (
        fast_tasks * BARRIER_FAST_SECONDS + slow_tasks * BARRIER_SLOW_SECONDS
    )
    return [
        {
            "name": "uniform",
            "mode": "barrier",
            "tasks": tasks,
            "slow_tasks": 0,
            "service_seconds": service_seconds,
            "concurrency": BARRIER_CONCURRENCY,
            # One length, the mixed backlog's mean, so the arms differ in variance and
            # in nothing else.
            "groups": [[SLEEP_SYNC, {"seconds": service_seconds / tasks}, tasks]],
        },
        {
            "name": "mixed",
            "mode": "barrier",
            "tasks": tasks,
            "slow_tasks": slow_tasks,
            "service_seconds": service_seconds,
            "concurrency": BARRIER_CONCURRENCY,
            "groups": [
                [SLEEP_SYNC, {"seconds": BARRIER_FAST_SECONDS}, fast_tasks],
                [SLEEP_SYNC, {"seconds": BARRIER_SLOW_SECONDS}, slow_tasks],
            ],
        },
    ]


def measure_barrier_rep(arm: dict[str, t.Any]) -> dict[str, t.Any]:
    """One drain of one backlog, and the idle slot-seconds it left behind."""
    worker = runner.WorkerSpec(concurrency=arm["concurrency"])
    truncate_queue_tables(worker.queue)
    preload_s = producer.preload_spread_tasks(
        [(path, kwargs, count) for path, kwargs, count in arm["groups"]]
    )
    window_start = analysis.capture_database_now()
    procs = runner.start_workers(worker, 1)
    try:
        with host.measure_phase() as phase:
            measurement.wait_until_drained(
                procs,
                name=arm["name"],
                queue=worker.queue,
                timeout_s=SATURATION_TIMEOUT_S,
            )
    except host.SuspendedPhaseError as exc:
        return {"valid": False, "error": str(exc)}
    finally:
        runner.stop_workers(procs)
    return {
        "valid": True,
        "preload_s": preload_s,
        "phase_s": phase.elapsed_s,
        # The finding: slots free while the backlog still held work.
        "idle_slot_s": analysis.read_idle_slot_seconds(
            worker.queue, None, arm["concurrency"]
        ),
        **analysis.analyze_saturation(worker.queue, None, window_start),
    }


def summarize_barrier_reps(
    arms: list[dict[str, t.Any]], reps: dict[str, list[dict[str, t.Any]]]
) -> list[dict[str, t.Any]]:
    """The arms that have a rep, in their canonical order however they ran.

    Ranked on throughput and not on `idle_slot_s`, which is better LOW: the shared
    median helper resolves an even rep count towards the worse of the two middles, and
    it decides which that is by the metric.
    """
    return [
        summarize_one_barrier_arm(arm, reps[arm["name"]])
        for arm in arms
        if reps[arm["name"]]
    ]


def summarize_one_barrier_arm(
    arm: dict[str, t.Any], reps: list[dict[str, t.Any]]
) -> dict[str, t.Any]:
    valid = sorted(
        (rep for rep in reps if rep["valid"]),
        key=lambda rep: rep[THROUGHPUT_KEY],
    )
    cv = measurement.measure_cv(valid, THROUGHPUT_KEY)
    low, high = measurement.measure_rep_range(valid, THROUGHPUT_KEY)
    median = measurement.pick_median_rep(valid, THROUGHPUT_KEY)
    return {
        "spec": {key: value for key, value in arm.items() if key != "groups"},
        "reps": reps,
        "ranking_key": THROUGHPUT_KEY,
        "median": median,
        "spread": measurement.measure_spread(valid, median, THROUGHPUT_KEY),
        "cv": cv,
        "range_low": low,
        "range_high": high,
        "invalid": measurement.is_measurement_invalid(reps, valid),
        "unstable": cv is not None and cv > measurement.MeasurementSpec.cv_limit,
        "host": host.collect_host_context(),
    }


def summarize_barrier_arm(entry: dict[str, t.Any]) -> str:
    median = entry["median"]
    return (
        f"{entry['spec']['name']}: {median.get('idle_slot_s', 0.0):.2f} idle slot-s, "
        f"{median.get(THROUGHPUT_KEY, 0.0):.1f} tasks/s, "
        f"{entry['spec']['slow_tasks']} slow of {entry['spec']['tasks']}"
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

    So this stage refuses a slept-through rep too, rather than being the one that keeps
    them; `perf_counter` alone would not notice.
    """
    try:
        with host.measure_phase():
            metrics = producer.run_producer_benchmark(mode, enqueues)
    except host.SuspendedPhaseError as exc:
        return {"valid": False, "error": str(exc)}
    else:
        return {"valid": True, **metrics}


def run_latency_under_load(options: StageOptions) -> None:
    """End-to-end latency at fractions of an offer rate this stage measured itself.

    A drain rate is not an arrival rate: a fleet draining a backlog has work waiting
    at every claim, while a paced one has to keep up in real time, polls that find
    nothing included. Taking fractions of the drain rate offered more than the fleet
    could absorb, so the upper rungs measured a diverging queue.
    """
    worker, workers, ceiling, calibration = read_ceiling(options)
    ramp = measure_sustainable_rate(worker, workers, ceiling, options)
    record_measurements(
        "latency_under_load",
        build_latency_measurements(worker, workers, ramp["rate_per_s"]),
        [],
        options,
        {"calibration": calibration, "sustainable_rate": ramp},
    )


def measure_sustainable_rate(
    worker: runner.WorkerSpec,
    workers: int,
    ceiling: float,
    options: StageOptions,
) -> dict[str, t.Any]:
    """Climb the offered rate until the fleet stops keeping up with it.

    The working point is the highest rate a probe absorbed — the fractions below it
    then mean something, where fractions of a drain rate did not. The rung above it is
    kept as the bracket: the knee is between the two and no probe here refines it.
    """
    probes: list[dict[str, t.Any]] = []
    rate = ceiling * RATE_RAMP_START_FRACTION
    absorbed = True
    while absorbed and rate <= ceiling:
        probes.append(measure_rate_probe(worker, workers, rate, options))
        absorbed = probes[-1]["sustained"]
        rate *= RATE_RAMP_STEP
    return summarize_rate_ramp(probes, ceiling, resolve_rate_ramp_seconds(options))


def summarize_rate_ramp(
    probes: list[dict[str, t.Any]], ceiling: float, offer_seconds: float
) -> dict[str, t.Any]:
    """Which rung of a finished climb the stage measures at, and what brackets it.

    Separate from the climbing above so the choice can be read — and asserted — over
    probes nobody had to make a machine produce: which rates a fleet absorbs is the
    machine's to decide, where which rung a set of verdicts selects is not.
    """
    sustained = [probe["rate_per_s"] for probe in probes if probe["sustained"]]
    refused = [probe["rate_per_s"] for probe in probes if not probe["sustained"]]
    return {
        "drain_ceiling_per_s": ceiling,
        "offer_seconds": offer_seconds,
        # Defaulting to the lowest rate probed, when even that one diverged: the stage
        # measures at a rate it can name rather than refusing, and says it is unproven.
        "rate_per_s": max(sustained, default=probes[0]["rate_per_s"]),
        "sustained": bool(sustained),
        # None where the ramp ran out of ceiling with everything absorbed, since the
        # knee is then above the top of the ramp rather than between two probes.
        "bracket_high_per_s": min(refused, default=None),
        "probes": probes,
    }


def measure_rate_probe(
    worker: runner.WorkerSpec,
    workers: int,
    rate_per_s: float,
    options: StageOptions,
) -> dict[str, t.Any]:
    """One offer at one rate, and whether the fleet absorbed it.

    Judged by the same rules that mark a measurement's reps, so a probe cannot call
    an offer absorbed that a rung at the same rate would then mark invalid.
    """
    spec = measurement.MeasurementSpec(
        name=f"ramp_{rate_per_s:.0f}_per_s",
        mode="rate",
        task_path=NOOP_SYNC,
        worker=worker,
        workers=workers,
        rate_per_s=rate_per_s,
        duration_s=resolve_rate_ramp_seconds(options),
        timeout_s=RATE_TIMEOUT_S,
        reps=1,
    )
    rep = measurement.run_clean_rep(spec)
    probe = {
        "rate_per_s": rate_per_s,
        "sustained": not measurement.is_rep_invalid(rep),
        "rep": rep,
    }
    print(summarize_rate_probe(probe))
    return probe


def summarize_rate_probe(probe: dict[str, t.Any]) -> str:
    """A probe on one line: the offer, the verdict, and the backlog behind it."""
    rep = probe["rep"]
    verdict = "sustained" if probe["sustained"] else "not sustained"
    return (
        f"ramp {probe['rate_per_s']:.1f}/s offered: {verdict}, "
        f"e2e p50 {rep.get('end_to_end_p50_s', 0.0) * 1000:.1f}ms, "
        f"backlog {rep.get('backlog_mid', 0)} -> {rep.get('backlog_end', 0)}"
    )


def build_latency_measurements(
    worker: runner.WorkerSpec, workers: int, rate_per_s: float
) -> list[measurement.MeasurementSpec]:
    return [
        measurement.MeasurementSpec(
            name=f"rate_{int(fraction * 100)}pct",
            mode="rate",
            task_path=NOOP_SYNC,
            worker=worker,
            workers=workers,
            rate_per_s=rate_per_s * fraction,
            duration_s=RATE_OFFER_SECONDS,
            timeout_s=RATE_TIMEOUT_S,
            spread_floor=RATE_SPREAD_FLOOR_S,
        )
        for fraction in RATE_FRACTIONS
    ]


def build_concurrency_measurements() -> list[measurement.MeasurementSpec]:
    # Fixed while the process_scaling ladder tracks the host, so a concurrency result
    # means the same thing on every machine — which is all the async ratio is for.
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
    """The worker ladder, each rung preloaded with 2,000 tasks per worker.

    Sized per rung so a ten-worker fleet still has a drain long enough to trim, which
    CONFOUNDS the ladder: its rungs run at 4,000 to 20,000 tasks and a deeper backlog
    is slower, so no rate here compares with the rate above it and the report marks
    the scaling efficiency it derives. Fixing it means one depth for every rung — see
    `benchmarks/CLAUDE.md`, which also records the direction the confound points.
    """
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

    The host's core count unless ``--max-workers`` asks for less. Derived from the
    ceiling rather than clamped to it, so a bounded ladder is still a ladder.
    """
    # 1 and 2 anchor the low end where per-worker efficiency is still readable; the
    # fractional steps track the ceiling so the curve means the same thing on any box.
    steps = {1, 2, ceiling // 4, ceiling // 2, ceiling * 3 // 4, ceiling}
    return sorted(step for step in steps if 1 <= step <= ceiling)


def build_pooled_vs_split_measurements(
    totals: list[int], options: StageOptions
) -> list[measurement.MeasurementSpec]:
    """Both shapes of each total on both workloads, pooled arm first, pairs adjacent.

    Two workloads because the shapes differ in what a body can hold. A nano-task body
    is over before its thread means anything, so the ranking of processes over threads
    was only ever measured where the answer costs least; a durable body holds a pool
    thread — and the Django connection its ORM work opens — for seconds, which is the
    regime django-absurd is for.

    Sized here rather than by `record_measurements`, which this stage cannot use: its
    reps interleave between the arms instead of running measurement by measurement. The
    durable arms are sized per SLOT, so every shape runs the same number of rounds
    rather than the same number of tasks.
    """
    durable_seconds = resolve_durable_seconds(options)
    return [
        apply_size_overrides(
            measurement.MeasurementSpec(
                name=f"{shape}{suffix}_{total}",
                mode="saturation",
                task_path=task_path,
                task_kwargs=task_kwargs,
                worker=runner.WorkerSpec(concurrency=concurrency),
                workers=workers,
                tasks=tasks,
                timeout_s=SATURATION_TIMEOUT_S,
            ),
            options,
        )
        for total in totals
        for suffix, task_path, task_kwargs, tasks in (
            ("", NOOP_SYNC, None, SATURATION_TASKS),
            (
                "_durable",
                DURABLE_WORK,
                {"seconds": durable_seconds},
                DURABLE_ROUNDS_PER_SLOT * total,
            ),
        )
        for shape, workers, concurrency in (
            ("pooled", 1, total),
            ("split", total, 1),
        )
    ]


def build_size_vs_depth_measurements() -> list[measurement.MeasurementSpec]:
    """One pending depth on three tables: empty, ballasted, and ballasted then vacuumed.

    Table SIZE and queue DEPTH move together in every other saturation measurement
    here, because a rep preloads what it drains — so the 0.42x that four times the
    `--tasks` cost cannot say which of the two it was. These arms hold the depth still
    and vary only what the table already holds under it, and the vacuumed arm splits
    that again into rows that are live and rows a drain left dead.

    At one process and one slot, where the claim path is the whole per-task cost and
    `statement_stats` is exact, so the report says WHICH statement a bigger table made
    more expensive rather than only that one did. Calibrated from nothing: an arm
    configured from an earlier stage's winner would differ from its own control in a
    second way.
    """
    ballast = SIZE_BALLAST_MULTIPLE * SATURATION_TASKS
    return [
        measurement.MeasurementSpec(
            name=name,
            mode="saturation",
            task_path=NOOP_SYNC,
            worker=runner.WorkerSpec(concurrency=1),
            tasks=SATURATION_TASKS,
            ballast_tasks=ballast_tasks,
            vacuum_ballast=vacuum_ballast,
            timeout_s=SATURATION_TIMEOUT_S,
        )
        for name, ballast_tasks, vacuum_ballast in (
            ("fresh_table", 0, False),
            ("aged_table", ballast, False),
            ("vacuumed_table", ballast, True),
        )
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


def build_durable_checkpoint_measurements(
    winner: runner.WorkerSpec, options: StageOptions
) -> list[measurement.MeasurementSpec]:
    """Three step depths on a brief durable body, then the same three on a long one.

    Sized per SLOT like pooled_vs_split's durable arms: a fixed task count runs for
    minutes at one concurrency and seconds at another, and the winning one is the
    machine's to choose. The depths run inner so a pair a per-step cost divides is
    measured back to back rather than a body length apart.
    """
    brief_seconds = resolve_durable_seconds(options)
    return [
        apply_size_overrides(
            measurement.MeasurementSpec(
                name=f"steps{depth}_{length}",
                mode="saturation",
                task_path=DURABLE_STEPS,
                task_kwargs={"seconds": seconds, "step_count": depth},
                worker=winner,
                tasks=DURABLE_CHECKPOINT_ROUNDS_PER_SLOT * winner.concurrency,
                timeout_s=SATURATION_TIMEOUT_S,
            ),
            options,
        )
        for length, seconds in (
            ("brief", brief_seconds),
            ("long", brief_seconds * LONG_DURABLE_MULTIPLE),
        )
        for depth in DURABLE_STEP_DEPTHS
    ]


def measure_shape_connections(
    specs: list[measurement.MeasurementSpec], durable_seconds: float
) -> list[dict[str, t.Any]]:
    """What each distinct SHAPE costs in Postgres backends, idle and working.

    Once per shape rather than once per measurement: connection cost is a property of
    the topology, and the arms of a pair share theirs whatever they run.
    """
    probed: dict[tuple[int, int], dict[str, t.Any]] = {}
    for spec in specs:
        shape = (spec.workers, spec.worker.concurrency)
        if shape not in probed:
            probed[shape] = probe_shape_backends(
                spec.worker, spec.workers, durable_seconds
            )
    return list(probed.values())


def probe_shape_backends(
    worker: runner.WorkerSpec, workers: int, durable_seconds: float
) -> dict[str, t.Any]:
    """One shape's backends: the delta across starting it, then the peak under work.

    A delta because the harness's own connections sit on the same database. Run before
    any rep, so the probe's own queries stay outside every measured phase.

    The busy sample runs the DURABLE workload whatever the arms it describes are
    measured on. A body only opens a connection of its own by touching the ORM, and
    only holds it while it runs, so a nano-task fleet has nothing for a sampler to see
    and this number would be the idle one repeated.
    """
    slots = workers * worker.concurrency
    truncate_queue_tables(worker.queue)
    before = analysis.count_client_backends()
    procs = runner.start_workers(worker, workers)
    try:
        idle = analysis.count_client_backends() - before
        # Two rounds, so every slot still has work in hand while the sample runs even
        # where one process claimed a whole round to itself.
        offer_durable_work(2 * slots, durable_seconds)
        busy = sample_peak_backends(before, worker.poll_interval, durable_seconds)
    finally:
        runner.stop_workers(procs)
    print(f"{workers}x{worker.concurrency}: {idle} backends idle, {busy} working")
    return {
        "shape": f"{workers}x{worker.concurrency}",
        "processes": workers,
        "concurrency": worker.concurrency,
        "connections_idle": idle,
        "connections_busy": busy,
    }


def offer_durable_work(count: int, durable_seconds: float) -> None:
    """Enqueue what the connection probe samples under, on the harness's OWN connection.

    Not `producer.preload_tasks`, whose pool threads each open a backend of their own: a
    closed one lingers in `pg_stat_activity` for long enough that the next sample counts
    it, and nothing in the view separates it from a worker's. Enqueueing here spends the
    connection the probe is already asking from, which `count_client_backends` excludes.
    """
    task_object = import_string(DURABLE_WORK)
    for _ in range(count):
        task_object.enqueue(seconds=durable_seconds)


def sample_peak_backends(
    baseline: int, poll_interval: float, durable_seconds: float
) -> int:
    """The most backends seen over one durable body's worth of sampling.

    A peak and not a reading: claims land a poll apart, so the instant every slot is
    working is not the instant the sampling starts.
    """
    deadline = time.monotonic() + 2 * poll_interval + 2 * durable_seconds
    peak = 0
    while time.monotonic() < deadline:
        peak = max(peak, analysis.count_client_backends() - baseline)
        time.sleep(BACKEND_SAMPLE_INTERVAL_S)
    return peak


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


def record_interleaved_measurements(
    stage: str,
    specs: list[measurement.MeasurementSpec],
    options: StageOptions,
    extra: dict[str, t.Any] | None = None,
) -> None:
    """Every arm's reps interleaved rather than each arm's run back to back.

    For a stage whose finding divides one arm by another: cumulative database state
    only grows, so an arm that always went first would carry an advantage no column
    records. `run_order` is the list the loop appends to, so every rewrite of the file
    records the order the arms have run in SO FAR rather than the order they were
    meant to.
    """
    run_order: list[str] = []
    recorded = {**(extra or {}), "run_order": run_order}
    reps: dict[str, list[dict[str, t.Any]]] = {spec.name: [] for spec in specs}
    # Written before the first rep, so a stage left with nothing to compare still
    # leaves a file naming what it refused and what refused it.
    write_stage_file(stage, [], options, recorded)
    for arm in build_interleaved_schedule(specs):
        reps[arm.name].append(measurement.run_clean_rep(arm))
        run_order.append(arm.name)
        write_stage_file(
            stage, summarize_interleaved_arms(specs, reps), options, recorded
        )
    for entry in summarize_interleaved_arms(specs, reps):
        print(summarize_measurement(entry))


def build_interleaved_schedule(
    specs: list[measurement.MeasurementSpec],
) -> list[measurement.MeasurementSpec]:
    """Every rep of every arm, in the order they run.

    Reversed on the odd reps, arms staying back to back: cumulative database state only
    grows, so a fixed order hands one arm of every pair the emptier tables.
    """
    rounds = specs[0].reps if specs else 0
    return [
        spec
        for index in range(rounds)
        for spec in (specs if index % 2 == 0 else specs[::-1])
    ]


def summarize_interleaved_arms(
    specs: list[measurement.MeasurementSpec],
    reps: dict[str, list[dict[str, t.Any]]],
) -> list[dict[str, t.Any]]:
    """The arms that have a rep, in their canonical order however they were run."""
    return [
        measurement.summarize_reps(spec, reps[spec.name])
        for spec in specs
        if reps[spec.name]
    ]


def apply_size_overrides(
    spec: measurement.MeasurementSpec, options: StageOptions
) -> measurement.MeasurementSpec:
    """Shrink a production-sized spec to whatever the caller asked for.

    ``tasks`` reaches only a saturation spec and ``duration`` only a rate one, the two
    modes being sized in different units.
    """
    replacements: dict[str, t.Any] = {}
    if options.reps is not None:
        replacements["reps"] = options.reps
    if options.tasks is not None and spec.mode == "saturation":
        replacements["tasks"] = options.tasks
        if spec.ballast_tasks:
            # Kept at the ratio to the measured depth the stage chose, so `--tasks`
            # shrinks the whole experiment rather than leaving a smoke run to drain a
            # production ballast.
            replacements["ballast_tasks"] = (
                spec.ballast_tasks * options.tasks // spec.tasks
            )
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
            # The three keys always: omitting one would read as a file written
            # before the ceiling was ever recorded.
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

    Beside the per-measurement `host` block rather than inside it, since one file is
    one run of one stage while load average and uptime vary within it.

    RESOLVED, so an unset flag records what it fell back to — `--max-workers` above
    all, whose default is the host's core count. `--tasks` and `--duration` stay null
    because a stage's production size is its own and every spec records the size it
    ran at.
    """
    return {
        "durable_seconds": resolve_durable_seconds(options),
        "duration_s": options.duration_s,
        "io_seconds": resolve_io_seconds(options),
        "max_workers": bound_fleet(HOST_CPUS, options),
        "reps": DEFAULT_REP_COUNT if options.reps is None else options.reps,
        "tasks": options.tasks,
    }


def resolve_io_seconds(options: StageOptions) -> float:
    """Seconds of simulated IO, read by the stage that sleeps and by the record of
    what it slept for, so the two cannot disagree about one run."""
    return SLEEP_IO_SECONDS if options.io_seconds is None else options.io_seconds


def resolve_durable_seconds(options: StageOptions) -> float:
    """How long a durable body holds its thread, read by the stages that run one, by
    the connection probe that samples under it, and by the record of all of them.

    `durable_checkpoints` measures this length AND `LONG_DURABLE_MULTIPLE` times it,
    so one flag sets both and the pair always spans the same ratio.
    """
    return (
        DURABLE_SECONDS if options.durable_seconds is None else options.durable_seconds
    )


def resolve_rate_ramp_seconds(options: StageOptions) -> float:
    """How long one ramp probe offers for, read by the probe and by its own record.

    `--duration` reaches it like any other rate window, so a suite can drive a whole
    ramp in seconds.
    """
    return RATE_RAMP_SECONDS if options.duration_s is None else options.duration_s


def summarize_measurement(result: dict[str, t.Any]) -> str:
    median = result["median"]
    # A saturation run starts with a full queue, so every task but the first waited
    # behind the backlog: its percentiles are drain time wearing latency's name.
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
    median = measurement.pick_median_rep(valid, "enqueues_per_s")
    # The shared helpers rather than a second copy of the arithmetic, which needs
    # every dispersion guard fixed in two places.
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

    When most rungs are marked, calibration lands on the slowest survivor and every
    later stage would otherwise read as though that were the machine's best.
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
    # A working point, not a report: nothing is dropped from a table by this, and
    # calibrating on a noisy best beats refusing, so the rest are the fallback.
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
    # Capping the SELECTION keeps the offered rate and the fleet asked to absorb it one
    # measurement; clamping afterwards aims a smaller fleet at a bigger one's ceiling.
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
    parser.add_argument(
        "--durable-seconds",
        default=None,
        type=float,
        help=(
            "Seconds a durable task body holds its worker thread for, in "
            f"pooled_vs_split's durable arms and its connection probe, and in "
            f"durable_checkpoints, whose long-body arms run at "
            f"{LONG_DURABLE_MULTIPLE} times this (default: {DURABLE_SECONDS:g}). A "
            "durable rep costs this times its rounds, so raising it to an agent tool "
            "call's real duration raises the bill with it."
        ),
    )
    args = parser.parse_args(argv)
    stages = args.stages or list(STAGE_NAMES)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
    django.setup()
    # All three are the caller's to fix, so they print as errors rather than crashing.
    try:
        refuse_measuring_under_debug()
        run_stages(stages, build_stage_options(args))
    except (
        InvalidSizeError,
        MeasuringUnderDebugError,
        MissingStageError,
        UncalibratableStageError,
    ) as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)


def build_stage_options(args: argparse.Namespace) -> StageOptions:
    """The parsed flags as one stage's options, refusing a size nothing could measure.

    Refused here because each goes wrong silently downstream: an empty ladder writes no
    file while reporting success, a zero-length window still gets divided by, and a
    durable body of no duration is the nano-task arm again under a durable name.
    """
    for flag, value, floor in (
        ("--durable-seconds", args.durable_seconds, SMALLEST_MEASURABLE_DURATION_S),
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
        durable_seconds=args.durable_seconds,
        max_workers=args.max_workers,
    )


def refuse_measuring_under_debug() -> None:
    """Refuse rather than record it: the host block reads config off the SERVER.

    A debug cursor is the harness's own process, so nothing in a results file could
    speak for it, and every rate in the run would be incomparable rather than merely
    annotated.
    """
    if settings.DEBUG:
        raise MeasuringUnderDebugError


if __name__ == "__main__":
    main()
