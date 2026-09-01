import argparse
import json
import typing as t
from pathlib import Path

DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent / "results"

TABLE_HEADER = (
    "| measurement | mode | workers | concurrency | batch | poll | tasks/s "
    "| e2e p50 s | e2e p90 s | e2e p99 s | spread | notes |"
)
TABLE_RULE = "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"


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
        (
            f"- cpu count: {context['cpu_count']}, "
            f"load average (1m): {context['load_avg_1m']:.2f}"
        ),
        (
            f"- python {context['python']}, Django {context['django']}, "
            f"absurd-sdk {context['absurd_sdk']}"
        ),
        f"- postgres: {context['postgres']}",
    ]


def describe_provenance(shas: list[str]) -> str:
    if len(shas) == 1:
        return f"`{shas[0]}`"
    return "mixed (" + ", ".join(f"`{sha}`" for sha in shas) + ")"


def describe_capture_window(stamps: list[str]) -> str:
    if stamps[0] == stamps[-1]:
        return stamps[0]
    return f"{stamps[0]} .. {stamps[-1]}"


def render_stage(stage: dict[str, t.Any]) -> list[str]:
    if stage["stage"] == "f":
        return render_producer_stage(stage)
    measurements = stage["measurements"]
    lines = ["", f"## Stage {stage['stage'].upper()}", "", TABLE_HEADER, TABLE_RULE]
    lines += [render_measurement_row(entry) for entry in measurements]
    lines += render_idle_probes(stage)
    lines += build_derived_lines(stage["stage"], measurements)
    return lines


def render_producer_stage(stage: dict[str, t.Any]) -> list[str]:
    """Stage F measures the ENQUEUE side, so it gets its own columns rather than
    borrowing an execution-throughput table it would have to fake."""
    return [
        "",
        "## Stage F",
        "",
        (
            "| mode | enqueues | enqueues/s | enqueue p50 s | enqueue p99 s "
            "| spread | notes |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- |",
        *[
            render_row(
                [
                    entry["spec"]["name"],
                    str(entry["median"].get("count", 0)),
                    f"{entry['median'].get('enqueues_per_s', 0.0):.1f}",
                    f"{entry['median'].get('enqueue_p50_s', 0.0):.5f}",
                    f"{entry['median'].get('enqueue_p99_s', 0.0):.5f}",
                    format_spread(entry["spread"]),
                    "⚠ flagged" if entry["flagged"] else "",
                ]
            )
            for entry in stage["measurements"]
        ],
    ]


def render_measurement_row(entry: dict[str, t.Any]) -> str:
    spec = entry["spec"]
    worker = spec["worker"]
    median = entry["median"]
    return render_row(
        [
            spec["name"],
            spec["mode"],
            str(spec["workers"]),
            str(worker["concurrency"]),
            "default" if worker["batch_size"] is None else str(worker["batch_size"]),
            f"{worker['poll_interval']:g}",
            f"{median.get('throughput_per_s', 0.0):.1f}",
            f"{median.get('end_to_end_p50_s', 0.0):.4f}",
            f"{median.get('end_to_end_p90_s', 0.0):.4f}",
            f"{median.get('end_to_end_p99_s', 0.0):.4f}",
            format_spread(entry["spread"]),
            "⚠ flagged" if entry["flagged"] else "",
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


def build_derived_lines(stage: str, measurements: list[dict[str, t.Any]]) -> list[str]:
    unflagged = [entry for entry in measurements if not entry["flagged"]]
    if not unflagged:
        return ["", "No unflagged measurements; nothing derived."]
    if stage == "b":
        return build_scaling_efficiency_lines(unflagged)
    if stage == "d":
        return build_async_ratio_lines(unflagged)
    if stage == "e":
        return build_checkpoint_multiplier_lines(unflagged)
    if all(entry["spec"]["mode"] == "rate" for entry in unflagged):
        # In rate mode throughput is set by the OFFER, so a throughput ratio there
        # only restates the configured rate; latency is what those vary.
        return build_ratio_lines(
            unflagged, measurements, "end_to_end_p50_s", "End-to-end p50"
        )
    return build_ratio_lines(unflagged, measurements, "throughput_per_s", "Throughput")


def build_scaling_efficiency_lines(measurements: list[dict[str, t.Any]]) -> list[str]:
    single = next(
        (
            entry["median"]["throughput_per_s"]
            for entry in measurements
            if entry["spec"]["workers"] == 1
        ),
        None,
    )
    if not single:
        return build_ratio_lines(
            measurements, measurements, "throughput_per_s", "Throughput"
        )
    return [
        "",
        "Scaling efficiency `T(N) / (N x T(1))` (flagged measurements excluded):",
        "",
        *[
            f"- {entry['spec']['workers']} worker(s): "
            f"{read_efficiency(entry, single):.2f}"
            for entry in measurements
        ],
    ]


def read_efficiency(entry: dict[str, t.Any], single_worker_throughput: float) -> float:
    workers = entry["spec"]["workers"]
    throughput = entry["median"]["throughput_per_s"]
    return float(throughput / (workers * single_worker_throughput))


def build_async_ratio_lines(measurements: list[dict[str, t.Any]]) -> list[str]:
    paired: dict[int, dict[str, float]] = {}
    for entry in measurements:
        flavour = "async" if entry["spec"]["task_path"].endswith("_async") else "sync"
        concurrency = entry["spec"]["worker"]["concurrency"]
        paired.setdefault(concurrency, {})[flavour] = entry["median"][
            "throughput_per_s"
        ]
    return [
        "",
        (
            "Async / sync throughput ratio at the same IO wait "
            "(flagged measurements excluded):"
        ),
        "",
        *[
            f"- concurrency {concurrency}: {pair['async'] / pair['sync']:.2f}x"
            for concurrency, pair in sorted(paired.items())
            if {"async", "sync"} <= pair.keys() and pair["sync"]
        ],
    ]


def build_checkpoint_multiplier_lines(
    measurements: list[dict[str, t.Any]],
) -> list[str]:
    throughput = {
        entry["spec"]["task_path"]: entry["median"]["throughput_per_s"]
        for entry in measurements
    }
    flat = throughput.get("benchmarks.tasks.noop_sync")
    workflow = throughput.get("benchmarks.tasks.run_steps")
    if not flat or not workflow:
        return build_ratio_lines(
            measurements, measurements, "throughput_per_s", "Throughput"
        )
    return [
        "",
        "Checkpoint cost (flagged measurements excluded):",
        "",
        f"- one `run_steps` task costs {flat / workflow:.2f}x a flat no-op task",
    ]


def build_ratio_lines(
    measurements: list[dict[str, t.Any]],
    all_measurements: list[dict[str, t.Any]],
    metric_key: str,
    label: str,
) -> list[str]:
    reference = measurements[0]
    base = reference["median"].get(metric_key, 0.0)
    if not base:
        return ["", "Reference measurement measured nothing; nothing derived."]
    return [
        "",
        (
            f"{label} relative to `{reference['spec']['name']}` "
            f"({describe_exclusions(measurements, all_measurements)}):"
        ),
        "",
        *[
            f"- `{entry['spec']['name']}`: {entry['median'][metric_key] / base:.2f}x"
            for entry in measurements
        ],
    ]


def describe_exclusions(
    measurements: list[dict[str, t.Any]], all_measurements: list[dict[str, t.Any]]
) -> str:
    """Name a baseline that moved: dropping a flagged first entry silently rebases
    every ratio in the block, which would otherwise be invisible between runs."""
    first = all_measurements[0]["spec"]["name"]
    if first == measurements[0]["spec"]["name"]:
        return "flagged measurements excluded"
    return (
        f"flagged measurements excluded; the stage's first measurement "
        f"`{first}` is flagged, "
        f"so the baseline moved"
    )


def format_spread(spread: float | None) -> str:
    # None, not 0.0, when a measurement had no positive median to divide by.
    return "n/a" if spread is None else f"{spread:.1%}"


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
