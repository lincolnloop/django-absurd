import dataclasses
import time
import typing as t

from benchmarks import analysis, host, producer, runner
from django_absurd.flush import truncate_queue_tables

DRAIN_POLL_INTERVAL_S = 0.5


class MeasurementTimeoutError(Exception):
    def __init__(self, name: str, timeout_s: float) -> None:
        super().__init__(
            f"Measurement '{name}' still had unfinished tasks after {timeout_s:.0f}s. "
            f"A measurement that never drains is refused rather than recorded: raise "
            f"timeout_s, cut the task count, or find out why the workers stalled."
        )


@dataclasses.dataclass(frozen=True)
class MeasurementSpec:
    name: str
    mode: t.Literal["saturation", "rate"]
    task_path: str
    worker: runner.WorkerSpec
    workers: int = 1
    tasks: int = 0
    rate_per_s: float = 0.0
    duration_s: float = 0.0
    task_kwargs: dict[str, t.Any] | None = None
    reps: int = 3
    timeout_s: float = 300.0
    spread_limit: float = 0.15
    # Absolute max-min, in the ranking key's own units, under which `spread_limit` no
    # longer applies. Relative spread divides by the median, so it RISES as a
    # measurement gets faster: reps of 67/89/173ms read as 119%. Zero keeps the pure
    # relative test, which is the historical behaviour.
    spread_floor: float = 0.0


def run_measurement(spec: MeasurementSpec) -> dict[str, t.Any]:
    """Run one measurement's reps from a clean queue, reduce to a median + flags."""
    reps = []
    for _ in range(spec.reps):
        truncate_queue_tables(spec.worker.queue)
        reps.append(run_one_rep(spec))
    return summarize_reps(spec, reps)


def run_one_rep(spec: MeasurementSpec) -> dict[str, t.Any]:
    try:
        if spec.mode == "saturation":
            metrics = run_saturation_rep(spec)
        else:
            metrics = run_rate_rep(spec)
    except host.SuspendedPhaseError as exc:
        return {"valid": False, "error": str(exc)}
    else:
        return {"valid": True, **metrics}


def run_saturation_rep(spec: MeasurementSpec) -> dict[str, t.Any]:
    preload_s = producer.preload_tasks(
        spec.task_path, spec.tasks, kwargs=spec.task_kwargs
    )
    procs = runner.start_workers(spec.worker, spec.workers)
    try:
        with host.measure_phase():
            wait_until_drained(spec)
    finally:
        runner.stop_workers(procs)
    metrics = analysis.analyze_saturation(spec.worker.queue)
    # A terminally failed task still satisfies the drain predicate, so without this
    # the measurement silently covers a smaller sample than it was asked to.
    metrics["missing_tasks"] = spec.tasks - metrics["n_tasks"]
    return {"preload_s": preload_s, **metrics}


def run_rate_rep(spec: MeasurementSpec) -> dict[str, t.Any]:
    procs = runner.start_workers(spec.worker, spec.workers)
    try:
        with host.measure_phase():
            window_start = analysis.capture_database_now()
            offer = producer.run_rate_producer(
                spec.task_path,
                spec.rate_per_s,
                spec.duration_s,
                kwargs=spec.task_kwargs,
            )
            window_end = analysis.capture_database_now()
            wait_until_drained(spec)
    finally:
        runner.stop_workers(procs)
    return {
        **analysis.analyze_rate(spec.worker.queue, window_start, window_end),
        **dataclasses.asdict(offer),
    }


def wait_until_drained(spec: MeasurementSpec) -> None:
    deadline = time.monotonic() + spec.timeout_s
    while analysis.count_unfinished_tasks(spec.worker.queue) > 0:
        if time.monotonic() > deadline:
            raise MeasurementTimeoutError(spec.name, spec.timeout_s)
        time.sleep(DRAIN_POLL_INTERVAL_S)


def summarize_reps(
    spec: MeasurementSpec, reps: list[dict[str, t.Any]]
) -> dict[str, t.Any]:
    ranking_key = (
        "throughput_per_s" if spec.mode == "saturation" else "end_to_end_p50_s"
    )
    valid = sorted(
        (rep for rep in reps if rep["valid"]), key=lambda rep: rep[ranking_key]
    )
    # Lower of the two middles at an even rep count: the upper one is the BEST rep,
    # and a measurement must not be summarized by its luckiest rep.
    median: dict[str, t.Any] = valid[(len(valid) - 1) // 2] if valid else {}
    spread = measure_spread(valid, median, ranking_key)
    absolute_spread = measure_absolute_spread(valid, ranking_key)
    return {
        "spec": dataclasses.asdict(spec),
        "reps": reps,
        "median": median,
        "spread": spread,
        "absolute_spread": absolute_spread,
        "flagged": is_measurement_unreliable(
            spec, reps, valid, spread, absolute_spread
        ),
        "host": host.collect_host_context(),
    }


def measure_spread(
    valid: list[dict[str, t.Any]], median: dict[str, t.Any], ranking_key: str
) -> float | None:
    """Relative spread, or ``None`` when there is nothing to spread.

    Never 0.0 for those cases: a measurement that measured nothing, or measured once,
    would otherwise read as the most stable one in its stage. One rep has no spread —
    it has an unknown one, and `--reps 1` is documented as a dry run.
    """
    if len(valid) < 2 or median.get(ranking_key, 0.0) <= 0:
        return None
    values = [rep[ranking_key] for rep in valid]
    return float((max(values) - min(values)) / median[ranking_key])


def measure_absolute_spread(
    valid: list[dict[str, t.Any]], ranking_key: str
) -> float | None:
    """``max - min`` in the ranking key's own units, or ``None`` with nothing valid.

    Recorded alongside the relative spread because the two disagree exactly where it
    matters: a fast measurement can be tight here and still read as noisy there.
    """
    if not valid:
        return None
    values = [rep[ranking_key] for rep in valid]
    return float(max(values) - min(values))


def is_measurement_unreliable(
    spec: MeasurementSpec,
    reps: list[dict[str, t.Any]],
    valid: list[dict[str, t.Any]],
    spread: float | None,
    absolute_spread: float | None,
) -> bool:
    """Every rep votes, not only the median one: a rep that under-offered has a LOWER
    latency, so it sorts away from the median and would never be looked at.
    """
    if not valid or len(valid) != len(reps) or spread is None:
        return True
    return (
        (spread > spec.spread_limit and (absolute_spread or 0.0) > spec.spread_floor)
        or any(rep.get("extra_runs", 0) > 0 for rep in valid)
        or any(rep.get("missing_tasks", 0) != 0 for rep in valid)
        or any(rep.get("degenerate_window", False) for rep in valid)
        or any(rep.get("offered_ok", True) is False for rep in valid)
    )
