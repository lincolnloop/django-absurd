import argparse
import json
import typing as t
from pathlib import Path

DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent / "results"

TABLE_HEADER = (
    "| measurement | mode | workers | concurrency | batch | poll | tasks/s "
    "| e2e p50 s | e2e p90 s | e2e p99 s | rep range | spread | cv | notes |"
)
TABLE_RULE = "| " + " | ".join(["---"] * 14) + " |"
PRODUCER_TABLE_HEADER = (
    "| mode | enqueues | enqueues/s | enqueue p50 s | enqueue p99 s "
    "| rep range | spread | cv | notes |"
)
PRODUCER_TABLE_RULE = "| " + " | ".join(["---"] * 9) + " |"

# Printed once for the whole document rather than under each table: it is how to read
# every row here, and nothing in it varies by stage.
MARK_LEGEND = (
    (
        "Marks: `!` invalid — a rep measured something other than what was asked (a "
        "redelivery, a task that never completed, a window too short to divide by, "
        "an under-offered rate). `~` unstable — the reps measured the right thing "
        "and disagreed, beyond the measurement's CV limit. `?` — fewer than two "
        "valid reps, so dispersion was never measured. A marked measurement stays "
        "in every table and in every number derived below one."
    ),
    "",
    (
        "`rep range`, `spread` and `cv` are over the metric the reps were ranked on: "
        "tasks/s in saturation mode, end-to-end p50 s in rate mode, enqueues/s in "
        "producer mode. `spread` is `(max - min) / median`, shown because the "
        "endpoints are what a reader wants; `cv` is stdev/mean and is what `~` is "
        "thresholded on, because a range grows with `--reps` on unchanged data "
        "and a CV does not."
    ),
)

# The metric every saturation-mode derivation divides, named once: the throughput
# ratio, the scaling efficiency and the checkpoint multiplier all reach for this one.
THROUGHPUT_KEY = "throughput_per_s"

# Share of the measured commit ceiling above which a measurement is called fsync-bound
# rather than client-bound. The concurrency ladder measured here ran from 28% of the
# ceiling at concurrency 1 to 102% at concurrency 16, where concurrent backends share
# an fsync through group commit. Where "near the ceiling" starts is a judgement and
# not a measured boundary, which is why the share itself prints beside the verdict.
FSYNC_BOUND_SHARE = 0.70

# Named by the flag that sets each one, so the header reads back as the command that
# produced it. Alphabetical: no ordering of these means anything to a reader.
OPTION_FLAGS = (
    ("--duration", "duration_s"),
    ("--io-seconds", "io_seconds"),
    ("--max-workers", "max_workers"),
    ("--reps", "reps"),
    ("--tasks", "tasks"),
)


def render_report(results_dir: Path) -> str:
    """Render every ``stage_*.json`` under ``results_dir`` as one markdown document."""
    stages = [
        json.loads(path.read_text())
        for path in sorted(results_dir.glob("stage_*.json"))
    ]
    if not stages:
        return f"No stage_*.json result files under {results_dir}.\n"
    lines = describe_host(stages)
    for stage in stages:
        lines += render_stage(stage)
    return "\n".join(lines) + "\n"


def describe_host(stages: list[dict[str, t.Any]]) -> list[str]:
    # Every measurement carries its own provenance, and a benchmark run can span a
    # rebuild; reading only the first would print one SHA for a mixed report.
    contexts = [entry["host"] for stage in stages for entry in stage["measurements"]]
    context = contexts[0]
    shas = sorted({entry["git_sha"] for entry in contexts})
    stamps = sorted(entry["captured_at"] for entry in contexts)
    return [
        "# django-absurd benchmark results",
        "",
        f"- git sha: {describe_provenance(shas)}",
        f"- captured at: {describe_capture_window(stamps)}",
        f"- options: {describe_options(stages)}",
        (
            f"- cpu count: {context['cpu_count']}, "
            f"load average (1m): {context['load_avg_1m']:.2f}"
        ),
        f"- commit ceiling: {describe_commit_ceiling(stages)}",
        (
            f"- python {context['python']}, Django {context['django']}, "
            f"absurd-sdk {context['absurd_sdk']}"
        ),
        f"- postgres: {context['postgres']}",
        "",
        *MARK_LEGEND,
    ]


def describe_provenance(shas: list[str]) -> str:
    return describe_alternatives([f"`{sha}`" for sha in shas])


def describe_commit_ceiling(stages: list[dict[str, t.Any]]) -> str:
    """What the machine's disk could commit while this run was made.

    71% of the active backend time in a full run is WAL durability, so every task rate
    below is a commit rate in disguise and the ceiling is what says whether it was the
    disk's number or Absurd's. Printed even when it is missing: a run nobody could
    calibrate is still a run, but it must not read like a calibrated one.
    """
    return describe_alternatives(sorted({format_commit_ceiling(s) for s in stages}))


def format_commit_ceiling(stage: dict[str, t.Any]) -> str:
    durable = stage.get("commit_ceiling_durable_per_s")
    if durable is None:
        return (
            "not measured, so nothing below says whether a throughput is this disk's "
            "number or Absurd's"
        )
    nondurable = stage.get("commit_ceiling_nondurable_per_s")
    after = stage.get("commit_ceiling_durable_after_per_s")
    return (
        f"{durable:.0f} commits/s durable, "
        f"{format_commit_rate(after)} after the run, "
        f"{format_commit_rate(nondurable)} non-durable "
        f"({describe_durability_cost(durable, nondurable)})"
    )


def format_commit_rate(value: float | None) -> str:
    return "not measured" if value is None else f"{value:.0f}/s"


def describe_durability_cost(durable: float, nondurable: float | None) -> str:
    """What fsync costs, as the multiple the same server reaches without it."""
    if nondurable is None:
        return "ratio not measured"
    return f"{nondurable / durable:.0f}x without fsync"


def describe_options(stages: list[dict[str, t.Any]]) -> str:
    """The configuration behind the numbers: every flag, at the value it resolved to.

    All of them, not only the ones that were passed — an omitted flag would be
    indistinguishable from one this report does not know about, and the whole line is
    one line either way.

    Stages are run separately and a partial re-run is a documented workflow, so a
    directory can hold one stage measured at a different size from the rest. A flag
    they disagree about reads as mixed, the same way a git SHA does.
    """
    recorded = [stage["options"] for stage in stages]
    described: list[str] = []
    for flag, key in OPTION_FLAGS:
        # Sorted as numbers rather than as the text they render to, so a mix reads
        # `8, 60`; an unset flag has no number to sort by and goes last.
        values = sorted(
            {entry[key] for entry in recorded},
            key=lambda value: (value is None, value or 0),
        )
        described.append(
            f"{flag} {describe_alternatives([format_option(v) for v in values])}"
        )
    return ", ".join(described)


def describe_alternatives(values: list[str]) -> str:
    """One value plainly; several as a mix, because a results directory can hold
    stages run from different checkouts or at different sizes."""
    if len(values) == 1:
        return values[0]
    return "mixed (" + ", ".join(values) + ")"


def format_option(value: float | None) -> str:
    # None where a flag has no single default to resolve to: `--tasks` and `--duration`
    # are each a per-stage production size, recorded per measurement in its own spec.
    return "stage default" if value is None else f"{value:g}"


def describe_capture_window(stamps: list[str]) -> str:
    if stamps[0] == stamps[-1]:
        return stamps[0]
    return f"{stamps[0]} .. {stamps[-1]}"


def render_stage(stage: dict[str, t.Any]) -> list[str]:
    if stage["stage"] == "producer_ceiling":
        return render_producer_stage(stage)
    measurements = stage["measurements"]
    lines = [
        "",
        f"## {render_heading(stage['stage'])}",
        "",
        *build_calibration_lines(stage),
        TABLE_HEADER,
        TABLE_RULE,
    ]
    lines += [render_measurement_row(entry) for entry in measurements]
    lines += render_idle_probes(stage)
    lines += build_commit_budget_lines(stage)
    lines += build_derived_lines(stage["stage"], measurements)
    return lines


def build_calibration_lines(stage: dict[str, t.Any]) -> list[str]:
    """Which measurement this stage was configured from, said where it was inherited.

    A stage calibrated from another runs at one rung of it and nothing in its own
    table says which: when most rungs of the earlier stage are marked, the working
    point lands on the slowest survivor and every number here is measured at it.
    """
    calibration = stage.get("calibration")
    if calibration is None:
        return []
    return [
        (
            f"Calibrated from `{calibration['stage']}` "
            f"`{calibration['measurement']}`: "
            f"{calibration['throughput_per_s']:.1f} tasks/s, "
            f"cv {format_dispersion(calibration['cv'])}, "
            f"{describe_calibration_standing(calibration)}."
        ),
        "",
    ]


def describe_calibration_standing(calibration: dict[str, t.Any]) -> str:
    """What was wrong with the rung that became the working point, if anything."""
    faults = [
        word
        for word, wrong in (
            ("invalid", calibration["invalid"]),
            ("unstable", calibration["unstable"]),
            ("dispersion unmeasured", calibration["cv"] is None),
        )
        if wrong
    ]
    return ", ".join(faults) or "valid and stable"


def render_heading(stage: str) -> str:
    """A stage name is already words, so it reads as a heading rather than shouting."""
    return stage.replace("_", " ").capitalize()


def render_producer_stage(stage: dict[str, t.Any]) -> list[str]:
    """The producer stage measures the ENQUEUE side, so it gets its own columns rather
    than borrowing an execution-throughput table it would have to fake."""
    return [
        "",
        f"## {render_heading(stage['stage'])}",
        "",
        PRODUCER_TABLE_HEADER,
        PRODUCER_TABLE_RULE,
        *[
            render_row(
                [
                    entry["spec"]["name"],
                    str(entry["median"].get("count", 0)),
                    f"{entry['median'].get('enqueues_per_s', 0.0):.1f}",
                    f"{entry['median'].get('enqueue_p50_s', 0.0):.5f}",
                    f"{entry['median'].get('enqueue_p99_s', 0.0):.5f}",
                    format_rep_range(entry),
                    format_dispersion(entry["spread"]),
                    format_dispersion(entry["cv"]),
                    describe_marks(entry),
                ]
            )
            for entry in stage["measurements"]
        ],
    ]


def render_measurement_row(entry: dict[str, t.Any]) -> str:
    spec = entry["spec"]
    worker = spec["worker"]
    median = entry["median"]
    # A saturation run starts with a full queue, so every task but the first waited
    # behind the whole backlog: its percentiles are drain time wearing latency's name.
    # Blank rather than omitted, so the column still lines up with the rate rows.
    paced = spec["mode"] == "rate"
    return render_row(
        [
            spec["name"],
            spec["mode"],
            str(spec["workers"]),
            str(worker["concurrency"]),
            "default" if worker["batch_size"] is None else str(worker["batch_size"]),
            f"{worker['poll_interval']:g}",
            f"{median.get('throughput_per_s', 0.0):.1f}",
            f"{median.get('end_to_end_p50_s', 0.0):.4f}" if paced else "",
            f"{median.get('end_to_end_p90_s', 0.0):.4f}" if paced else "",
            f"{median.get('end_to_end_p99_s', 0.0):.4f}" if paced else "",
            format_rep_range(entry),
            format_dispersion(entry["spread"]),
            format_dispersion(entry["cv"]),
            describe_marks(entry),
        ]
    )


def render_idle_probes(stage: dict[str, t.Any]) -> list[str]:
    probes = stage.get("idle_probes")
    if not probes:
        return []
    lines = [
        "",
        "Idle polling tax (workers parked on an empty queue):",
        "",
        "| poll interval s | workers | claims/s/worker | 1/poll_interval |",
        "| --- | --- | --- | --- |",
    ]
    lines += [
        render_row(
            [
                f"{probe['poll_interval']:g}",
                str(probe["workers"]),
                f"{probe['claims_per_s_per_worker']:.2f}",
                f"{1 / probe['poll_interval']:.2f}",
            ]
        )
        for probe in probes
    ]
    return lines


def build_commit_budget_lines(stage: dict[str, t.Any]) -> list[str]:
    """What bound each saturation measurement: its own client, or the disk's fsync.

    Throughput times the commits a task cost is the commit rate the run asked of the
    disk. Near the ceiling that rate is a property of the disk and moves to a
    different number on another machine; a fraction of it is the case where the
    measurement is about Absurd.

    Saturation rows only. A paced row's rate is set by the offer, so its share of the
    ceiling says how big the offer was and nothing about what bound it.
    """
    measurements = [
        entry
        for entry in stage["measurements"]
        if entry["spec"]["mode"] == "saturation"
    ]
    if not any(entry["median"].get("commits_per_task") for entry in measurements):
        return []
    ceiling = stage.get("commit_ceiling_durable_per_s")
    return [
        "",
        "Commit budget (`tasks/s x commits/task`, against the durable commit ceiling):",
        "",
        *[describe_commit_budget(entry, ceiling) for entry in measurements],
    ]


def describe_commit_budget(entry: dict[str, t.Any], ceiling: float | None) -> str:
    name = entry["spec"]["name"]
    commits_per_task = entry["median"].get("commits_per_task")
    if not commits_per_task:
        return (
            f"- `{name}`: commits per task not recorded, so nothing says what bound it"
        )
    throughput = entry["median"].get(THROUGHPUT_KEY, 0.0)
    demanded = (
        f"- `{name}`: {throughput * commits_per_task:.0f} commits/s "
        f"({throughput:.1f} x {commits_per_task:.2f})"
    )
    if ceiling is None:
        return f"{demanded}, against no ceiling — nothing here says what bound it"
    share = throughput * commits_per_task / ceiling
    bound = "fsync-bound" if share >= FSYNC_BOUND_SHARE else "client-bound"
    return f"{demanded}, {share:.0%} of {ceiling:.0f}/s — {bound}"


def build_derived_lines(stage: str, measurements: list[dict[str, t.Any]]) -> list[str]:
    """Every measurement derives, marked or not.

    Nothing is filtered out here: a measurement excluded from the arithmetic is a
    finding deleted, and an unstable one is a finding about the system rather than a
    broken rep. What a marked measurement changes is the SHAPE of what it derives —
    see `describe_quotient`, which carries the reps' own endpoints through the
    division instead of publishing a point estimate the reps never agreed on.
    """
    if stage == "process_scaling":
        return build_scaling_efficiency_lines(measurements)
    if stage == "sync_vs_async":
        return build_async_ratio_lines(measurements)
    if stage == "checkpoint_cost":
        return build_checkpoint_multiplier_lines(measurements)
    if all(entry["spec"]["mode"] == "rate" for entry in measurements):
        # In rate mode throughput is set by the OFFER, so a throughput ratio there
        # only restates the configured rate; latency is what those vary.
        return build_ratio_lines(measurements, "end_to_end_p50_s", "End-to-end p50")
    return build_ratio_lines(measurements, THROUGHPUT_KEY, "Throughput")


def build_scaling_efficiency_lines(measurements: list[dict[str, t.Any]]) -> list[str]:
    single = next(
        (entry for entry in measurements if entry["spec"]["workers"] == 1), None
    )
    if single is None or not single["median"].get(THROUGHPUT_KEY, 0.0):
        return build_ratio_lines(measurements, THROUGHPUT_KEY, "Throughput")
    lines = ["", "Scaling efficiency `T(N) / (N x T(1))`:", ""]
    for entry in measurements:
        workers = entry["spec"]["workers"]
        efficiency = describe_quotient(entry, single, THROUGHPUT_KEY, workers, "")
        lines.append(f"- {workers} worker(s): {efficiency}")
    return lines


def build_async_ratio_lines(measurements: list[dict[str, t.Any]]) -> list[str]:
    paired: dict[int, dict[str, dict[str, t.Any]]] = {}
    for entry in measurements:
        flavour = "async" if entry["spec"]["task_path"].endswith("_async") else "sync"
        concurrency = entry["spec"]["worker"]["concurrency"]
        paired.setdefault(concurrency, {})[flavour] = entry
    return [
        "",
        "Async / sync throughput ratio at the same IO wait:",
        "",
        *[
            f"- concurrency {concurrency}: "
            f"{describe_quotient(pair['async'], pair['sync'], THROUGHPUT_KEY, 1, 'x')}"
            for concurrency, pair in sorted(paired.items())
            if {"async", "sync"} <= pair.keys()
            and pair["sync"]["median"].get(THROUGHPUT_KEY, 0.0)
        ],
    ]


def build_checkpoint_multiplier_lines(
    measurements: list[dict[str, t.Any]],
) -> list[str]:
    by_task_path = {entry["spec"]["task_path"]: entry for entry in measurements}
    flat = by_task_path.get("tasks.noop_sync")
    workflow = by_task_path.get("tasks.run_steps")
    if (
        flat is None
        or workflow is None
        or not workflow["median"].get(THROUGHPUT_KEY, 0.0)
    ):
        return build_ratio_lines(measurements, THROUGHPUT_KEY, "Throughput")
    return [
        "",
        "Checkpoint cost:",
        "",
        (
            f"- one `run_steps` task costs "
            f"{describe_quotient(flat, workflow, THROUGHPUT_KEY, 1, 'x')} "
            f"a flat no-op task"
        ),
    ]


def build_ratio_lines(
    measurements: list[dict[str, t.Any]], metric_key: str, label: str
) -> list[str]:
    reference = measurements[0]
    if not reference["median"].get(metric_key, 0.0):
        return ["", "Reference measurement measured nothing; nothing derived."]
    return [
        "",
        f"{label} relative to `{reference['spec']['name']}`:",
        "",
        *[
            f"- `{entry['spec']['name']}`: "
            f"{describe_quotient(entry, reference, metric_key, 1, 'x')}"
            for entry in measurements
        ],
    ]


def describe_quotient(
    entry: dict[str, t.Any],
    base_entry: dict[str, t.Any],
    metric_key: str,
    scale: float,
    suffix: str,
) -> str:
    """`entry / (scale x base_entry)`, plus the interval its reps span.

    A quotient of two medians is a point estimate whichever way its inputs wobbled: a
    scaling efficiency measured against a base whose reps ran 209-501 is a range, and
    printing only the middle of it publishes a precision nobody measured.
    """
    base = base_entry["median"][metric_key] * scale
    point = f"{entry['median'].get(metric_key, 0.0) / base:.2f}{suffix}"
    low, high = read_rep_range(entry, metric_key)
    base_low, base_high = read_rep_range(base_entry, metric_key)
    if base_low <= 0 or (low == high and base_low == base_high):
        return point
    return (
        f"{point} (reps {low / (base_high * scale):.2f}-"
        f"{high / (base_low * scale):.2f}{suffix})"
    )


def read_rep_range(entry: dict[str, t.Any], metric_key: str) -> tuple[float, float]:
    """The reps' endpoints for `metric_key`, or the median rep's own value twice.

    Endpoints are recorded for the metric the reps were RANKED on and no other, so a
    rate measurement appearing in a throughput block has none to carry — and neither
    does a measurement no rep survived.
    """
    point = float(entry["median"].get(metric_key, 0.0))
    if entry["ranking_key"] != metric_key or entry["range_low"] is None:
        return (point, point)
    return (float(entry["range_low"]), float(entry["range_high"]))


def describe_marks(entry: dict[str, t.Any]) -> str:
    """What is wrong with a row, said on the row rather than by deleting it."""
    marks = []
    if entry["invalid"]:
        marks.append("!")
    if entry["unstable"]:
        marks.append("~")
    if entry["cv"] is None:
        marks.append("?")
    return " ".join(marks)


def format_rep_range(entry: dict[str, t.Any]) -> str:
    """The endpoints the reps actually produced, at whatever magnitude they are.

    One format for both units: a throughput reads 209.3-501.2 and a latency 0.012-0.05,
    where a fixed number of decimals would round one of the two into a single number.
    """
    if entry["range_low"] is None:
        return "n/a"
    return f"{entry['range_low']:.4g}-{entry['range_high']:.4g}"


def format_dispersion(value: float | None) -> str:
    # None, not 0.0, when there were fewer than two reps to compare, or no positive
    # middle to divide by: a measurement that measured nothing must not read as the
    # steadiest one in its stage.
    return "n/a" if value is None else f"{value:.1%}"


def render_row(fields: list[str]) -> str:
    return "| " + " | ".join(fields) + " |"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Render a benchmark results directory as markdown."
    )
    parser.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR, type=Path)
    print(render_report(parser.parse_args(argv).results_dir), end="")


if __name__ == "__main__":
    main()
