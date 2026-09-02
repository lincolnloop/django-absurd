import dataclasses
import statistics
import time
import typing as t

import analysis
import host
import producer
import runner
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
    # Sample stdev over the mean, and not the max-min range this used to threshold: a
    # range grows with the rep count on unchanged data (5.3% at n=3, 14.1% at n=50 on a
    # fixed process, flagging 0.2% to 30.3% of the time) and `--reps` is a live flag,
    # so asking for more repeats made good data look worse. A CV is flat over the same
    # sweep (2.8% to 3.1%), and 0.10 sits in the measured gap between settings that
    # repeat (1.0-3.4%) and ones that genuinely lurch (25-50%).
    cv_limit: float = 0.10
    # Absolute max-min, in the ranking key's own units, under which `cv_limit` no
    # longer applies. Both dispersion statistics divide by a middle, so they RISE as a
    # measurement gets faster: reps of 67/89/173ms read as 119% spread. Zero keeps the
    # pure relative test, which is the historical behaviour.
    spread_floor: float = 0.0


def run_measurement(spec: MeasurementSpec) -> dict[str, t.Any]:
    """Run one measurement's reps from a clean queue, reduce to a median + flags."""
    reps = []
    for _ in range(spec.reps):
        truncate_queue_tables(spec.worker.queue)
        # Bracketing each rep rather than reading the host block's one figure, which
        # is collected after every rep has run: a 1-minute average sampled there is
        # mostly the harness's own load, so it cannot say what it was asked to.
        load_before = host.read_load_average()
        rep = run_one_rep(spec)
        reps.append(
            {**rep, "load_before": load_before, "load_after": host.read_load_average()}
        )
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
    commits_before = analysis.read_xact_commit()
    try:
        with host.measure_phase() as phase:
            wait_until_drained(spec)
    finally:
        runner.stop_workers(procs)
    # Read once the workers have exited: a live backend flushes its transaction
    # counters at most once a second, so a rep that ends inside that window would
    # lose the commits it just made. Before the analysis queries, which are commits
    # of the harness's own and no part of what a task cost.
    commits = analysis.read_xact_commit() - commits_before
    metrics = analysis.analyze_saturation(spec.worker.queue)
    # A terminally failed task still satisfies the drain predicate, so without this
    # the measurement silently covers a smaller sample than it was asked to.
    metrics["missing_tasks"] = spec.tasks - metrics["n_tasks"]
    # Every other number here comes off a trimmed window that excludes the ramp and the
    # tail by construction, so without this a rep cannot say where its own time went.
    return {
        "preload_s": preload_s,
        "phase_s": phase.elapsed_s,
        # What the drain asked of the disk, per task: throughput times this is the
        # commit rate the run demanded, which is only comparable with the commit
        # ceiling beside it. The preload is outside the phase, so this is the
        # execution side alone — no enqueue commits in it.
        "commits_per_task": commits / metrics["n_runs"] if metrics["n_runs"] else None,
        **metrics,
    }


def run_rate_rep(spec: MeasurementSpec) -> dict[str, t.Any]:
    procs = runner.start_workers(spec.worker, spec.workers)
    try:
        with host.measure_phase() as phase:
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
    # The offer and the drain that followed it, which the trimmed window excludes.
    # No commits per task: this rep's completed-run count comes off the trimmed middle
    # of the offer window while the phase spans the whole offer and drain, so the
    # quotient would divide two different windows into each other.
    return {
        "phase_s": phase.elapsed_s,
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
    absolute_spread = measure_absolute_spread(valid, ranking_key)
    cv = measure_cv(valid, ranking_key)
    low, high = measure_rep_range(valid, ranking_key)
    return {
        "spec": dataclasses.asdict(spec),
        "reps": reps,
        # Named here rather than re-derived from the mode by every reader: the range,
        # the spread and the CV are all over this one metric and nothing else.
        "ranking_key": ranking_key,
        "median": median,
        "spread": measure_spread(valid, median, ranking_key),
        "absolute_spread": absolute_spread,
        "cv": cv,
        "range_low": low,
        "range_high": high,
        "invalid": is_measurement_invalid(reps, valid),
        "unstable": is_measurement_unstable(spec, cv, absolute_spread),
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


def measure_cv(valid: list[dict[str, t.Any]], ranking_key: str) -> float | None:
    """Sample stdev over the mean, or ``None`` with fewer than two reps to compare.

    The dispersion statistic the instability flag is thresholded on. Unlike the spread
    it does not grow with the rep count, so raising `--reps` sharpens the estimate
    instead of moving the goalposts.
    """
    if len(valid) < 2:
        return None
    values = [rep[ranking_key] for rep in valid]
    mean = statistics.fmean(values)
    if mean <= 0:
        return None
    return float(statistics.stdev(values) / mean)


def measure_rep_range(
    valid: list[dict[str, t.Any]], ranking_key: str
) -> tuple[float | None, float | None]:
    """The reps' own endpoints, so a report can print `209-501` rather than a percent.

    The percentage a spread reduces to is what a reader has to un-reduce; these are
    the two numbers the measurement actually produced.
    """
    if not valid:
        return (None, None)
    values = [rep[ranking_key] for rep in valid]
    return (float(min(values)), float(max(values)))


def is_measurement_invalid(
    reps: list[dict[str, t.Any]], valid: list[dict[str, t.Any]]
) -> bool:
    """Whether a rep measured something OTHER than what the spec asked for.

    Every rep votes, not only the median one: a rep that under-offered has a LOWER
    latency, so it sorts away from the median and would never be looked at.
    """
    return (
        not valid
        or len(valid) != len(reps)
        or any(rep.get("extra_runs", 0) > 0 for rep in valid)
        or any(rep.get("missing_tasks", 0) != 0 for rep in valid)
        or any(rep.get("degenerate_window", False) for rep in valid)
        or any(rep.get("offered_ok", True) is False for rep in valid)
    )


def is_measurement_unstable(
    spec: MeasurementSpec, cv: float | None, absolute_spread: float | None
) -> bool:
    """Whether the reps measured the right thing and disagreed about the answer.

    Not the same condition as invalidity and not the same remedy: an unstable
    measurement is a finding about the system, an invalid one is a broken rep. An
    unmeasured dispersion (one valid rep) is neither, and reads as `n/a` in the report
    rather than as a disagreement nobody observed.
    """
    if cv is None:
        return False
    return cv > spec.cv_limit and (absolute_spread or 0.0) > spec.spread_floor
