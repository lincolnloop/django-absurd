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

# Share of one connection's commit ceiling above which a measurement is called
# connection-bound rather than client-bound. The concurrency ladder measured here ran
# from 28% of the ceiling at concurrency 1 to 102% at concurrency 16 — sixteen threads
# on the same single connection, which is what it saturated. Where "near the ceiling"
# starts is a judgement and not a measured boundary, which is why the band the verdict
# was read from prints beside it.
CONNECTION_BOUND_SHARE = 0.70

# Statements printed under one measurement. A results file keeps
# `analysis.STATEMENT_STATS_LIMIT` of them so the fan-out can be re-read in full; a
# report is read top to bottom, and past the costliest few the entries are microsecond
# bookkeeping repeated under every row in the stage.
REPORT_STATEMENT_LIMIT = 5
# Normalised SQL arrives wrapped over several lines and runs to Postgres's
# `track_activity_query_size`, while a bullet has to stay one line of it.
STATEMENT_TEXT_LIMIT = 110

# The `cluster_name` compose gives the RAM-backed benchmark server. The warning is
# keyed off this name and not off a suspiciously large ceiling, because a tmpfs commit
# rate reads as a hundredfold better disk rather than as no disk at all — 56,659
# commits/s against 584 off the volume — and no threshold separates the two.
TMPFS_CLUSTER_NAME = "bench-tmpfs"

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
    # A stage can legitimately measure nothing — a pooled_vs_split whose every pair the
    # worker bound refused writes a file saying which — and the header is built out of
    # measurement provenance, so there would be no run to describe.
    if not any(stage["measurements"] for stage in stages):
        return f"No measurements in the stage_*.json files under {results_dir}.\n"
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
        (
            f"- commit ceiling (commits/s, one connection): "
            f"{describe_commit_ceiling(stages)}"
        ),
        *describe_storage_medium(contexts),
        (
            f"- python {context['python']}, Django {context['django']}, "
            f"absurd-sdk {context['absurd_sdk']}"
        ),
        f"- postgres: {context['postgres']}, cluster {describe_cluster(contexts)}",
        "",
        *MARK_LEGEND,
    ]


def describe_provenance(shas: list[str]) -> str:
    return describe_alternatives([f"`{sha}`" for sha in shas])


def describe_commit_ceiling(stages: list[dict[str, t.Any]]) -> str:
    """What one connection to this server could commit while the run was made.

    Every task rate below is a commit rate in disguise — 71% of the active backend time
    in a full run against a Docker volume was WAL durability — and the ceiling is what
    says whether it was the connection's number or Absurd's. Each probe prints its
    dispersion beside its median: the durable rate is a wide distribution however fast
    the medium, and a bare number would be read as a constant. Printed even when it is
    missing — a run nobody could calibrate is still a run, but it must not read like a
    calibrated one.
    """
    return describe_alternatives(sorted({format_commit_ceiling(s) for s in stages}))


def format_commit_ceiling(stage: dict[str, t.Any]) -> str:
    durable = stage.get("commit_ceiling_durable")
    if durable is None:
        return (
            "not measured, so nothing below says whether a throughput is this "
            "connection's number or Absurd's"
        )
    nondurable = stage.get("commit_ceiling_nondurable")
    after = stage.get("commit_ceiling_durable_after")
    return (
        f"durable {format_commit_rate(durable)}, "
        f"after the run {format_commit_rate(after)}, "
        f"non-durable {format_commit_rate(nondurable)}, "
        f"{describe_durability_cost(durable, nondurable)}"
    )


def format_commit_rate(ceiling: dict[str, float] | None) -> str:
    """A probe's median, with the spread that says how much of it to believe."""
    if ceiling is None:
        return "not measured"
    return (
        f"{ceiling['median_per_s']:.0f} "
        f"(cv {ceiling['cv']:.0%}, {ceiling['range_low']:.0f}-"
        f"{ceiling['range_high']:.0f})"
    )


def describe_durability_cost(
    durable: dict[str, float], nondurable: dict[str, float] | None
) -> str:
    """What fsync costs, as the multiple the same server reaches without it."""
    if nondurable is None:
        return "ratio not measured"
    return f"{nondurable['median_per_s'] / durable['median_per_s']:.0f}x without fsync"


def describe_storage_medium(contexts: list[dict[str, t.Any]]) -> list[str]:
    """Whether the server kept its data in RAM, and what that costs the reader.

    Printed directly under the commit ceiling rather than beside the postgres version
    where the cluster name itself goes: the ceiling is the number this line is about,
    and the one a reader would otherwise carry off as a property of django-absurd.
    `any`, not all — one stage measured on RAM is enough to make the absolute rates in
    the file it sits in incomparable with a durable one.
    """
    if not any(entry["cluster_name"] == TMPFS_CLUSTER_NAME for entry in contexts):
        return []
    return [
        (
            "- storage: RAM. The data directory is a tmpfs, so fsync costs a memcpy "
            "and every rate here is for COMPARING configurations. None of them is a "
            "durable-storage figure, and none may be published as a property of "
            "django-absurd."
        )
    ]


def describe_cluster(contexts: list[dict[str, t.Any]]) -> str:
    """The server's `cluster_name`, which is how a run says which server it got.

    An ordinary server declares none and reads back as the empty string. That prints
    as `unnamed` rather than as nothing at all, so the field is always there to read.
    """
    return describe_alternatives(
        sorted(
            {
                f"`{entry['cluster_name']}`" if entry["cluster_name"] else "unnamed"
                for entry in contexts
            }
        )
    )


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
    lines += render_skipped_pairs(stage)
    lines += render_shape_connections(stage)
    lines += render_run_order(stage)
    lines += build_commit_budget_lines(stage)
    lines += build_statement_cost_lines(stage)
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


def render_skipped_pairs(stage: dict[str, t.Any]) -> list[str]:
    """The comparisons a stage refused to make, said where their rows would be.

    A pair whose split arm the worker bound cannot spawn is dropped whole rather than
    run at the bound — half a pair is not a comparison — and a table two rows short
    with nothing said about it reads like a run that crashed.
    """
    skipped = stage.get("skipped_pairs")
    if not skipped:
        return []
    return [
        "",
        "Pairs not run:",
        "",
        *[
            f"- total {pair['total']}: `--max-workers {pair['max_workers']}` cannot "
            f"spawn the {pair['total']} processes its split arm needs, so neither arm "
            f"of the pair was measured"
            for pair in skipped
        ],
    ]


def render_shape_connections(stage: dict[str, t.Any]) -> list[str]:
    """What each shape cost in Postgres backends, and what that does to the ratios."""
    shapes = stage.get("shape_connections")
    if not shapes:
        return []
    return [
        "",
        "Postgres backends each shape opened (measured across starting its fleet):",
        "",
        "| measurement | processes | concurrency | connections |",
        "| --- | --- | --- | --- |",
        *[
            render_row(
                [
                    shape["measurement"],
                    str(shape["processes"]),
                    str(shape["concurrency"]),
                    str(shape["connections"]),
                ]
            )
            for shape in shapes
        ],
        "",
        describe_connection_confound(shapes),
    ]


def describe_connection_confound(shapes: list[dict[str, t.Any]]) -> str:
    """Whether a pair's two shapes reached one total on the same connection count.

    They differ in claim path by design. If they also differ in how many backends they
    open, every ratio below is both differences at once and no line here can separate
    them — which is a thing to say out loud rather than a reason to drop the stage.
    """
    opened_by_total: dict[int, set[int]] = {}
    for shape in shapes:
        total = shape["processes"] * shape["concurrency"]
        opened_by_total.setdefault(total, set()).add(shape["connections"])
    unequal = sorted(
        total for total, opened in opened_by_total.items() if len(opened) > 1
    )
    if not unequal:
        return (
            "Both shapes of every pair opened the same number of backends, so a ratio "
            "below is the claim path and nothing else."
        )
    return (
        f"At total {', '.join(str(total) for total in unequal)} the two shapes opened "
        "different numbers of backends — a pooled arm's slots share the connections of "
        "the one process holding them, a split arm gets a set per process. Every ratio "
        "below is the claim path AND the connection count, and nothing here separates "
        "the two."
    )


def render_run_order(stage: dict[str, t.Any]) -> list[str]:
    """The order the arms ran in, which is a property of the measurement itself.

    Cumulative database state only grows across a stage, so an arm that always ran
    first would carry an advantage no column in the table records.
    """
    run_order = stage.get("run_order")
    if not run_order:
        return []
    return [
        "",
        "Arms ran in this order, reversed each rep so neither always went first: "
        + ", ".join(run_order)
        + ".",
    ]


def build_commit_budget_lines(stage: dict[str, t.Any]) -> list[str]:
    """What bound each saturation measurement: its own client, or its connection.

    Throughput times the commits a task cost is the commit rate the run asked of the
    database, and dividing that by the worker count is what one worker's connection
    carried — the comparable quantity, since a worker opens the SDK's one connection
    whatever its concurrency and the ceiling is one connection's. Near the ceiling that
    rate belongs to the connection and moves on another machine; a fraction of it is
    the case where the measurement is about Absurd.

    Saturation rows only. A paced row's rate is set by the offer, so its share of the
    ceiling says how big the offer was and nothing about what bound it.
    """
    measurements = select_saturation_measurements(stage)
    if not any(entry["median"].get("commits_per_task") for entry in measurements):
        return []
    opening = stage.get("commit_ceiling_durable")
    closing = stage.get("commit_ceiling_durable_after")
    return [
        "",
        (
            "Commit budget (`tasks/s x commits/task / workers`, against one "
            "connection's durable ceiling):"
        ),
        "",
        *[describe_commit_budget(entry, opening, closing) for entry in measurements],
    ]


def describe_commit_budget(
    entry: dict[str, t.Any],
    opening: dict[str, float] | None,
    closing: dict[str, float] | None,
) -> str:
    """One row's commit rate, as a band across every durable probe the run recorded.

    The band spans the union of the opening and closing probes' endpoints, because a
    ceiling that MOVED during the run calibrates nothing measured under it: one run
    here opened at 615 commits/s and closed at 2,100 against the same server, and its
    opening probe alone called 15 of that run's 25 rows connection-bound — a confident
    verdict on a run that crossed between storage regimes halfway through and is not
    internally comparable at all. The union refuses that call, which is the correct one
    to refuse, and costs a run that did not drift nothing: run A's verdicts are the
    same either way.

    No single share of a median prints beside it. Two probes are two calibrations and
    not two draws of one, so when they disagree there is no rate a share could honestly
    be taken against, and a headline percentage is exactly what a reader would take the
    verdict from instead of the band.
    """
    name = entry["spec"]["name"]
    commits_per_task = entry["median"].get("commits_per_task")
    if not commits_per_task:
        return (
            f"- `{name}`: commits per task not recorded, so nothing says what bound it"
        )
    throughput = entry["median"].get(THROUGHPUT_KEY, 0.0)
    workers = entry["spec"]["workers"]
    per_connection = throughput * commits_per_task / workers
    demanded = (
        f"- `{name}`: {per_connection:.0f} commits/s per worker connection "
        f"({throughput:.1f} x {commits_per_task:.2f} / {workers})"
    )
    # The opening probe is what the run's rows were measured against, so a run without
    # one has no calibration for a closing probe to widen — the same asymmetry the
    # header's `format_commit_ceiling` already reports the whole line as missing on.
    if opening is None:
        return f"{demanded}, against no ceiling — nothing here says what bound it"
    probes = [opening] if closing is None else [opening, closing]
    band_low = per_connection / max(probe["range_high"] for probe in probes)
    band_high = per_connection / min(probe["range_low"] for probe in probes)
    return (
        f"{demanded}, {band_low:.0%}-{band_high:.0%} of the durable ceiling across "
        f"{describe_band_probes(closing)} — "
        f"{describe_what_bound_it(band_low, band_high)}"
    )


def describe_band_probes(closing: dict[str, float] | None) -> str:
    """Which probes the band came from, since a wide band has two different causes.

    A reader comparing two reports has to tell a band that widened because the machine
    drifted mid-run from one that widened because the disk is worse than it was.
    """
    if closing is None:
        return "the opening probe alone"
    return "both probes"


def describe_what_bound_it(band_low: float, band_high: float) -> str:
    """Which side of the line the whole band falls on, or that it lies across it.

    Read off the band and not off any median: the calibration's own spread is wide
    enough that a verdict taken from one draw of it would flip between two identical
    runs, and its two probes can disagree by more again, which is the false precision
    this block exists to refuse.
    """
    if band_low >= CONNECTION_BOUND_SHARE:
        return "connection-bound"
    if band_high < CONNECTION_BOUND_SHARE:
        return "client-bound"
    return "unresolved: the ceiling's spread straddles the line"


def select_saturation_measurements(stage: dict[str, t.Any]) -> list[dict[str, t.Any]]:
    """The rows whose rate the run discovered rather than imposed.

    A paced row's rate is the offer's, so anything divided into it — a share of the
    commit ceiling, a millisecond per task — says how big the offer was and nothing
    about what the work cost.
    """
    return [
        entry
        for entry in stage["measurements"]
        if entry["spec"]["mode"] == "saturation"
    ]


def build_statement_cost_lines(stage: dict[str, t.Any]) -> list[str]:
    """Where a task's time went, statement by statement, and what was left for Python.

    Under the commit budget because it decomposes the same quotient: the budget says
    what a task asked of the disk, this says which statements asked it, how often each
    ran and what the wall clock spent nowhere near the server. A block rather than
    columns — one row per statement per measurement is not a table anybody can scan,
    and the fan-out is the finding.
    """
    measurements = select_saturation_measurements(stage)
    if not any(entry["median"].get("statement_stats") for entry in measurements):
        return []
    return [
        "",
        (
            "Per-task cost (median rep, ms/task; server time is summed over every "
            "backend the phase used, so above one worker it counts concurrent work "
            "against one wall clock):"
        ),
        "",
        *[line for entry in measurements for line in describe_statement_cost(entry)],
    ]


def describe_statement_cost(entry: dict[str, t.Any]) -> list[str]:
    """One measurement's per-task split, then the statements that made up its server
    side, costliest first."""
    name = entry["spec"]["name"]
    stats = entry["median"].get("statement_stats")
    if not stats:
        return [f"- `{name}`: no statement stats recorded, so nothing itemises it"]
    return [
        (
            f"- `{name}`: {stats['wall_ms_per_task']:.2f} wall = "
            f"{stats['server_exec_ms_per_task']:.2f} server + "
            f"{stats['client_ms_per_task']:.2f} client"
        ),
        *[
            format_statement_cost(statement)
            for statement in stats["statements"][:REPORT_STATEMENT_LIMIT]
        ],
    ]


def format_statement_cost(statement: dict[str, t.Any]) -> str:
    """One statement's calls and server time per task.

    Nesting is marked because it is what the counts have to be read against: a nested
    row is a statement Absurd's PL/pgSQL ran inside a top-level call that was already
    charged for its time, so only the top-level rows sum to the server side.
    """
    nesting = "" if statement["toplevel"] else " nested"
    return (
        f"  - {statement['calls_per_task']:.2f} calls x "
        f"{statement['total_exec_ms_per_task']:.3f} ms{nesting}: "
        f"`{summarize_statement_text(statement['query'])}`"
    )


def summarize_statement_text(query: str) -> str:
    """Normalised SQL as one line, cut where a bullet stops being readable."""
    collapsed = " ".join(query.split())
    if len(collapsed) <= STATEMENT_TEXT_LIMIT:
        return collapsed
    return collapsed[:STATEMENT_TEXT_LIMIT] + "…"


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
    if stage == "pooled_vs_split":
        return build_pooled_vs_split_lines(measurements)
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


def build_pooled_vs_split_lines(measurements: list[dict[str, t.Any]]) -> list[str]:
    """`split / pooled` at every total both shapes of the pair were measured at.

    Paired on the shapes themselves — processes times concurrency is the total, and
    the pooled arm is the one-process one — rather than on the measurement names, so
    the pairing cannot come apart from what was actually run.
    """
    paired: dict[int, dict[str, dict[str, t.Any]]] = {}
    for entry in measurements:
        spec = entry["spec"]
        shape = "pooled" if spec["workers"] == 1 else "split"
        total = spec["workers"] * spec["worker"]["concurrency"]
        paired.setdefault(total, {})[shape] = entry
    complete = [
        (total, pair)
        for total, pair in sorted(paired.items())
        if {"pooled", "split"} <= pair.keys()
        and pair["pooled"]["median"].get(THROUGHPUT_KEY, 0.0)
    ]
    if not complete:
        # A run killed between a pair's two arms, or a pooled arm no rep survived:
        # a stage with rows in it still deserves a number under them.
        return build_ratio_lines(measurements, THROUGHPUT_KEY, "Throughput")
    return [
        "",
        "Split / pooled throughput at the same total concurrency:",
        "",
        *[
            f"- total {total}: "
            + describe_quotient(pair["split"], pair["pooled"], THROUGHPUT_KEY, 1, "x")
            for total, pair in complete
        ],
    ]


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
    # A stage whose every measurement was refused before it ran has no reference to
    # divide by and nothing to say about one; its own block says which pairs and why.
    if not measurements:
        return []
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
