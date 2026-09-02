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
# Ranking keys a SMALLER value is the better measurement of. Everything else here is
# a rate, where bigger is better; only an end-to-end latency runs the other way, and
# which way it runs is what says which of two middle reps is the unlucky one.
LOWER_IS_BETTER_RANKING_KEYS = frozenset({"end_to_end_p50_s"})


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
    # Sample stdev over the mean, and not a max-min range: a range grows with the rep
    # count on unchanged data, so asking for more repeats made good data look worse.
    cv_limit: float = 0.10
    # Absolute max-min, in the ranking key's own units, under which `cv_limit` no
    # longer applies: every relative dispersion RISES as a measurement gets faster.
    spread_floor: float = 0.0


def run_measurement(spec: MeasurementSpec) -> dict[str, t.Any]:
    """Run one measurement's reps from a clean queue, reduce to a median + marks."""
    return summarize_reps(spec, [run_clean_rep(spec) for _ in range(spec.reps)])


def run_clean_rep(spec: MeasurementSpec) -> dict[str, t.Any]:
    """One rep from a truncated queue, with the ambient load sampled either side.

    Separate from the loop above so a stage comparing two configurations can interleave
    their reps rather than letting one arm always go first.
    """
    truncate_queue_tables(spec.worker.queue)
    # Bracketing each rep rather than reading the host block's one figure, which is
    # collected afterwards and is mostly the harness's own load by then.
    load_before = host.read_load_average()
    rep = run_one_rep(spec)
    return {**rep, "load_before": load_before, "load_after": host.read_load_average()}


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
    # Ahead of the commit snapshot, because reading the statement view is itself a
    # statement and a commit: the other order bills that read to the tasks.
    statements_before = analysis.read_statement_stats()
    commits_before = analysis.read_xact_commit()
    try:
        with host.measure_phase() as phase:
            wait_until_drained(spec)
    finally:
        runner.stop_workers(procs)
    # Read once the workers have exited, since a live backend flushes its transaction
    # counters at most once a second. Before the analysis queries, which commit too.
    commits = analysis.read_xact_commit() - commits_before
    statements_after = analysis.read_statement_stats()
    metrics = analysis.analyze_saturation(spec.worker.queue)
    # A terminally failed task still satisfies the drain predicate, so without this
    # the measurement silently covers a smaller sample than it was asked to.
    metrics["missing_tasks"] = spec.tasks - metrics["n_tasks"]
    # `preload_s` and `phase_s` because every other number comes off a trimmed window
    # that excludes the ramp and the tail by construction.
    return {
        "preload_s": preload_s,
        "phase_s": phase.elapsed_s,
        # The execution side alone — the preload sits outside the phase — so throughput
        # times this is the commit rate the drain asked of the disk.
        "commits_per_task": commits / metrics["n_runs"] if metrics["n_runs"] else None,
        # The same exchange rate itemised, and `None` on any server that counted no
        # statements, which is every suite server: the extension needs a preload.
        "statement_stats": analysis.build_statement_stats(
            statements_before, statements_after, metrics["n_runs"], phase.elapsed_s
        ),
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
    # No commits per task and no statement stats: the completed-run count comes off the
    # trimmed middle of the offer while the phase spans the offer AND the drain.
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
    median = pick_median_rep(valid, ranking_key)
    absolute_spread = measure_absolute_spread(valid, ranking_key)
    cv = measure_cv(valid, ranking_key)
    low, high = measure_rep_range(valid, ranking_key)
    return {
        "spec": dataclasses.asdict(spec),
        "reps": reps,
        # Named rather than re-derived from the mode by every reader: the range, the
        # spread and the CV are all over this one metric and nothing else.
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


def pick_median_rep(
    valid: list[dict[str, t.Any]], ranking_key: str
) -> dict[str, t.Any]:
    """The middle rep of reps already sorted ascending, resolving an even count
    towards the WORSE of the two middles.

    Which middle that is depends on the metric, and taking the lower one both times
    summarized a rate measurement by its LUCKIEST rep: a throughput and an enqueue
    rate are better high, an end-to-end latency is better low.
    """
    if not valid:
        return {}
    if ranking_key in LOWER_IS_BETTER_RANKING_KEYS:
        return valid[len(valid) // 2]
    return valid[(len(valid) - 1) // 2]


def measure_spread(
    valid: list[dict[str, t.Any]], median: dict[str, t.Any], ranking_key: str
) -> float | None:
    """Relative spread, or ``None`` when there is nothing to spread.

    Never 0.0: a measurement that measured nothing, or measured once, would otherwise
    read as the most stable one in its stage.
    """
    if len(valid) < 2 or median.get(ranking_key, 0.0) <= 0:
        return None
    values = [rep[ranking_key] for rep in valid]
    return float((max(values) - min(values)) / median[ranking_key])


def measure_absolute_spread(
    valid: list[dict[str, t.Any]], ranking_key: str
) -> float | None:
    """``max - min`` in the ranking key's own units, or ``None`` with nothing valid.

    Recorded beside the relative spread because a fast measurement can be tight here
    and still read as noisy there.
    """
    if not valid:
        return None
    values = [rep[ranking_key] for rep in valid]
    return float(max(values) - min(values))


def measure_cv(valid: list[dict[str, t.Any]], ranking_key: str) -> float | None:
    """Sample stdev over the mean, or ``None`` with fewer than two reps to compare.

    What the instability flag is thresholded on: unlike the spread it does not grow
    with the rep count, so raising `--reps` sharpens the estimate.
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
    """The reps' own endpoints, so a report can print a range rather than a percent.

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
    """Whether any rep measured something OTHER than what the spec asked for.

    Every rep votes, not only the median one: a rep that under-offered has a LOWER
    latency, so it sorts away from the median and would never be looked at.
    """
    return not valid or any(is_rep_invalid(rep) for rep in reps)


def is_rep_invalid(rep: dict[str, t.Any]) -> bool:
    """One rep's own verdict, which the rate ramp reads a probe off directly.

    Split out rather than inlined above so a probe judging whether one offer was
    absorbed and a measurement marking its reps cannot drift apart.
    """
    return (
        not rep["valid"]
        or rep.get("extra_runs", 0) > 0
        or rep.get("missing_tasks", 0) != 0
        or rep.get("degenerate_window", False)
        or rep.get("offered_ok", True) is False
        # A paced rep whose queue was still growing when the offer stopped measured
        # the ramp towards a rate, not the rate.
        or rep.get("backlog_grew", False)
    )


def is_measurement_unstable(
    spec: MeasurementSpec, cv: float | None, absolute_spread: float | None
) -> bool:
    """Whether the reps measured the right thing and disagreed about the answer.

    Not invalidity and not the same remedy: an unstable measurement is a finding about
    the system. An unmeasured dispersion is neither, and reads as `n/a`.
    """
    if cv is None:
        return False
    return cv > spec.cv_limit and (absolute_spread or 0.0) > spec.spread_floor
