import json
import typing as t
from pathlib import Path

import pytest

import report

HOST = {
    "absurd_sdk": "0.5.0",
    "captured_at": "2026-08-27T12:00:00+00:00",
    "cpu_count": 8,
    "django": "6.1",
    "git_sha": "deadbeef",
    "load_avg_1m": 0.5,
    "postgres": "PostgreSQL 18.0 on x86_64-pc-linux-gnu",
    "python": "3.14.3",
}

OPTIONS = {
    "duration_s": None,
    "io_seconds": 0.05,
    "max_workers": 8,
    "reps": 3,
    "tasks": None,
}

RANKING_KEYS = {
    "producer": "enqueues_per_s",
    "rate": "end_to_end_p50_s",
    "saturation": "throughput_per_s",
}

HEADER = (
    "# django-absurd benchmark results\n"
    "\n"
    "- git sha: `deadbeef`\n"
    "- captured at: 2026-08-27T12:00:00+00:00\n"
    "- options: --duration stage default, --io-seconds 0.05, --max-workers 8, "
    "--reps 3, --tasks stage default\n"
    "- cpu count: 8, load average (1m): 0.50\n"
    "- python 3.14.3, Django 6.1, absurd-sdk 0.5.0\n"
    "- postgres: PostgreSQL 18.0 on x86_64-pc-linux-gnu\n"
    "\n"
    "Marks: `!` invalid — a rep measured something other than what was asked (a "
    "redelivery, a task that never completed, a window too short to divide by, an "
    "under-offered rate). `~` unstable — the reps measured the right thing and "
    "disagreed, beyond the measurement's CV limit. `?` — fewer than two valid reps, "
    "so dispersion was never measured. A marked measurement stays in every table and "
    "in every number derived below one.\n"
    "\n"
    "`rep range`, `spread` and `cv` are over the metric the reps were ranked on: "
    "tasks/s in saturation mode, end-to-end p50 s in rate mode, enqueues/s in "
    "producer mode. `spread` is `(max - min) / median`, shown because the endpoints "
    "are what a reader wants; `cv` is stdev/mean and is what `~` is thresholded on, "
    "because a range grows with `--reps` on unchanged data and a CV does not.\n"
)

MEASUREMENT_TABLE_HEAD = (
    "| measurement | mode | workers | concurrency | batch | poll | tasks/s "
    "| e2e p50 s | e2e p90 s | e2e p99 s | rep range | spread | cv | notes |\n"
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- "
    "| --- | --- |\n"
)


def build_measurement(
    name: str,
    spec: dict[str, t.Any],
    median: dict[str, t.Any],
    summary: dict[str, t.Any] | None = None,
    host: dict[str, t.Any] | None = None,
) -> dict[str, t.Any]:
    """One summarized measurement, the way `measurement.summarize_reps` writes it.

    `summary` overrides the dispersion block by its own key names, so a test states
    the shape it means in the file's own vocabulary. Left alone it describes reps that
    agreed exactly — endpoints on the median rep's own value — so a test that is not
    about dispersion renders one number and derives one number.
    """
    full_spec = {
        "name": name,
        "mode": "saturation",
        "workers": 1,
        "task_path": "tasks.noop_sync",
        "worker": {"concurrency": 1, "batch_size": None, "poll_interval": 0.25},
        **spec,
    }
    full_median = {
        "throughput_per_s": 100.0,
        "end_to_end_p50_s": 0.012,
        "end_to_end_p90_s": 0.03,
        "end_to_end_p99_s": 0.05,
        **median,
    }
    ranking_key = RANKING_KEYS[full_spec["mode"]]
    ranked = full_median.get(ranking_key, 0.0)
    return {
        "spec": full_spec,
        "ranking_key": ranking_key,
        "median": full_median,
        "spread": 0.04,
        "cv": 0.02,
        "range_low": ranked,
        "range_high": ranked,
        "invalid": False,
        "unstable": False,
        **(summary or {}),
        "host": host or HOST,
    }


def write_stage(
    tmp_path: Path, stage: str, entries: list[dict[str, t.Any]], **extra: t.Any
) -> None:
    """The results file a stage run leaves behind, which is the report's real input."""
    (tmp_path / f"stage_{stage}.json").write_text(
        json.dumps(
            {"stage": stage, "options": OPTIONS, "measurements": entries, **extra}
        )
    )


def render(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    stage: str,
    entries: list[dict[str, t.Any]],
    **extra: t.Any,
) -> str:
    """Drive the report the way a reader does, and hand back what it printed.

    A results file on disk is the real input here — it is the interface between a stage
    run and the report — so the only thing worth faking is the measurement, never the
    rendering.
    """
    write_stage(tmp_path, stage, entries, **extra)
    report.main(["--results-dir", str(tmp_path)])
    return capsys.readouterr().out


def test_says_so_when_the_results_directory_holds_no_stage_files(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """An empty directory is the report's most likely first input, so it names it."""
    report.main(["--results-dir", str(tmp_path)])

    assert capsys.readouterr().out == (
        f"No stage_*.json result files under {tmp_path}.\n"
    )


def test_renders_stage_tables_from_result_files(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    entries = [
        build_measurement("concurrency_1", {}, {"throughput_per_s": 412.5}),
        build_measurement(
            "concurrency_2",
            {"worker": {"concurrency": 2, "batch_size": 4, "poll_interval": 0.25}},
            {"throughput_per_s": 800.0},
            {
                "cv": 0.31,
                "range_high": 990.0,
                "range_low": 614.0,
                "spread": 0.47,
                "unstable": True,
            },
        ),
    ]

    assert render(capsys, tmp_path, "worker_knobs", entries) == (
        HEADER + "\n"
        "## Worker knobs\n"
        "\n"
        + MEASUREMENT_TABLE_HEAD
        + "| concurrency_1 | saturation | 1 | 1 | default | 0.25 "
        "| 412.5 |  |  |  | 412.5-412.5 | 4.0% | 2.0% |  |\n"
        "| concurrency_2 | saturation | 1 | 2 | 4 | 0.25 "
        "| 800.0 |  |  |  | 614-990 | 47.0% | 31.0% | ~ |\n"
        "\n"
        "Throughput relative to `concurrency_1`:\n"
        "\n"
        "- `concurrency_1`: 1.00x\n"
        "- `concurrency_2`: 1.94x (reps 1.49-2.40x)\n"
    )


def test_carries_an_unstable_measurements_range_into_what_it_derives(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A ratio against a base whose reps ran 209-501 is a range, not a number.

    Both the baseline's spread and the entry's own are carried through the division,
    so the interval printed is the one the measurements actually support rather than
    the middle of it wearing two decimal places.
    """
    entries = [
        build_measurement(
            "concurrency_1",
            {},
            {"throughput_per_s": 300.0},
            {
                "cv": 0.42,
                "range_high": 501.0,
                "range_low": 209.0,
                "spread": 0.97,
                "unstable": True,
            },
        ),
        build_measurement("concurrency_2", {}, {"throughput_per_s": 600.0}),
    ]

    assert (
        "Throughput relative to `concurrency_1`:\n"
        "\n"
        "- `concurrency_1`: 1.00x (reps 0.42-2.40x)\n"
        "- `concurrency_2`: 2.00x (reps 1.20-2.87x)\n"
    ) in render(capsys, tmp_path, "worker_knobs", entries)


def test_derives_from_an_invalid_measurement_rather_than_dropping_it(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A measurement that measured the wrong thing is marked, never deleted.

    It stays the baseline every ratio in the block divides by, too: dropping it would
    silently rebase the whole block against a different measurement between one run
    and the next. Its worst rep measured no throughput at all, which is the endpoint a
    ratio cannot be divided by — so that one prints as a point rather than as an
    interval reaching to infinity.
    """
    entries = [
        build_measurement(
            "concurrency_1",
            {},
            {"throughput_per_s": 412.5},
            {"invalid": True, "range_high": 500.0, "range_low": 0.0},
        ),
        build_measurement("concurrency_2", {}, {"throughput_per_s": 825.0}),
    ]

    assert render(capsys, tmp_path, "worker_knobs", entries) == (
        HEADER + "\n"
        "## Worker knobs\n"
        "\n"
        + MEASUREMENT_TABLE_HEAD
        + "| concurrency_1 | saturation | 1 | 1 | default | 0.25 "
        "| 412.5 |  |  |  | 0-500 | 4.0% | 2.0% | ! |\n"
        "| concurrency_2 | saturation | 1 | 1 | default | 0.25 "
        "| 825.0 |  |  |  | 825-825 | 4.0% | 2.0% |  |\n"
        "\n"
        "Throughput relative to `concurrency_1`:\n"
        "\n"
        "- `concurrency_1`: 1.00x\n"
        "- `concurrency_2`: 2.00x\n"
    )


def test_marks_a_measurement_whose_dispersion_was_never_measured(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """One rep has an unknown dispersion, not a zero and not a disagreement.

    `--reps 1` is a documented dry run, so this is the shape of every measurement in
    one: an endpoint pair with nothing between them and nothing to compare, which the
    row says outright instead of reading as the steadiest measurement in the stage.
    """
    entries = [
        build_measurement(
            "concurrency_1",
            {},
            {"throughput_per_s": 412.5},
            {"cv": None, "spread": None},
        )
    ]

    assert render(capsys, tmp_path, "worker_knobs", entries) == (
        HEADER + "\n"
        "## Worker knobs\n"
        "\n"
        + MEASUREMENT_TABLE_HEAD
        + "| concurrency_1 | saturation | 1 | 1 | default | 0.25 "
        "| 412.5 |  |  |  | 412.5-412.5 | n/a | n/a | ? |\n"
        "\n"
        "Throughput relative to `concurrency_1`:\n"
        "\n"
        "- `concurrency_1`: 1.00x\n"
    )


def test_leaves_latency_columns_empty_for_a_saturation_measurement(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A saturation run's latency is drain time, not latency.

    Its queue is full by construction, so every task but the first waited behind the
    whole backlog. Publishing percentiles for it invites the comparison the harness
    tells the reader not to make.

    The rate row's own range is over the latency it was ranked on, so a throughput
    block cannot borrow it: that row derives a point while the saturation rows carry
    their endpoints through.
    """
    entries = [
        build_measurement("concurrency_1", {}, {"throughput_per_s": 412.5}),
        build_measurement("rate_25pct", {"mode": "rate"}, {"throughput_per_s": 90.0}),
    ]

    assert render(capsys, tmp_path, "worker_knobs", entries) == (
        HEADER + "\n"
        "## Worker knobs\n"
        "\n"
        + MEASUREMENT_TABLE_HEAD
        + "| concurrency_1 | saturation | 1 | 1 | default | 0.25 "
        "| 412.5 |  |  |  | 412.5-412.5 | 4.0% | 2.0% |  |\n"
        "| rate_25pct | rate | 1 | 1 | default | 0.25 "
        "| 90.0 | 0.0120 | 0.0300 | 0.0500 | 0.012-0.012 | 4.0% | 2.0% |  |\n"
        "\n"
        "Throughput relative to `concurrency_1`:\n"
        "\n"
        "- `concurrency_1`: 1.00x\n"
        "- `rate_25pct`: 0.22x\n"
    )


def test_renders_a_measurement_whose_every_rep_was_refused(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A measurement the host slept through has no median, no endpoints, no rates.

    The row is still printed, marked for both the invalidity and the dispersion
    nobody got to measure. What stops there is the arithmetic below the table: every
    ratio in a block divides by the first measurement, so a zero there would make the
    whole block infinities rather than one honest row.
    """
    entries = [
        build_measurement(
            "concurrency_1",
            {},
            {"throughput_per_s": 0.0},
            {
                "cv": None,
                "invalid": True,
                "range_high": None,
                "range_low": None,
                "spread": None,
            },
        ),
        build_measurement("concurrency_2", {}, {"throughput_per_s": 800.0}),
    ]

    assert render(capsys, tmp_path, "worker_knobs", entries) == (
        HEADER + "\n"
        "## Worker knobs\n"
        "\n"
        + MEASUREMENT_TABLE_HEAD
        + "| concurrency_1 | saturation | 1 | 1 | default | 0.25 "
        "| 0.0 |  |  |  | n/a | n/a | n/a | ! ? |\n"
        "| concurrency_2 | saturation | 1 | 1 | default | 0.25 "
        "| 800.0 |  |  |  | 800-800 | 4.0% | 2.0% |  |\n"
        "\n"
        "Reference measurement measured nothing; nothing derived.\n"
    )


def test_reports_mixed_provenance_when_measurements_disagree(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    entries = [
        build_measurement("concurrency_1", {}, {}, host={**HOST, "git_sha": "bbb222"}),
        build_measurement(
            "concurrency_2",
            {},
            {},
            host={
                **HOST,
                "git_sha": "aaa111",
                "captured_at": "2026-08-27T15:00:00+00:00",
            },
        ),
    ]

    rendered = render(capsys, tmp_path, "worker_knobs", entries)

    assert "- git sha: mixed (`aaa111`, `bbb222`)\n" in rendered
    assert (
        "- captured at: 2026-08-27T12:00:00+00:00 .. 2026-08-27T15:00:00+00:00\n"
    ) in rendered


def test_reports_mixed_options_when_stages_were_run_at_different_sizes(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Stages are run separately and a partial re-run is a documented workflow, so a
    directory holding one stage measured at another size is a real case rather than a
    corruption. The flags they disagree about read as mixed, the way a git SHA does;
    the ones they agree on still read as one value."""
    entries = [build_measurement("flat", {}, {})]
    write_stage(
        tmp_path,
        "checkpoint_cost",
        entries,
        options={**OPTIONS, "reps": 10, "tasks": 60},
    )

    rendered = render(capsys, tmp_path, "worker_knobs", entries)

    assert (
        "- options: --duration stage default, --io-seconds 0.05, --max-workers 8, "
        "--reps mixed (3, 10), --tasks mixed (60, stage default)\n"
    ) in rendered


def test_renders_scaling_efficiency_for_process_scaling(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The single-worker rung is the divisor, so its own spread widens every rung."""
    entries = [
        build_measurement(
            "workers_1",
            {"workers": 1},
            {"throughput_per_s": 100.0},
            {"range_high": 110.0, "range_low": 90.0},
        ),
        build_measurement("workers_2", {"workers": 2}, {"throughput_per_s": 180.0}),
    ]

    assert (
        "Scaling efficiency `T(N) / (N x T(1))`:\n"
        "\n"
        "- 1 worker(s): 1.00 (reps 0.82-1.22)\n"
        "- 2 worker(s): 0.90 (reps 0.82-1.00)\n"
    ) in render(capsys, tmp_path, "process_scaling", entries)


def test_falls_back_to_ratios_when_process_scaling_never_measured_one_worker(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Efficiency is measured against the single-worker rung, and a run killed partway
    keeps whichever rungs it finished — so the stage still reports something."""
    entries = [
        build_measurement("workers_2", {"workers": 2}, {"throughput_per_s": 180.0}),
        build_measurement("workers_4", {"workers": 4}, {"throughput_per_s": 320.0}),
    ]

    assert (
        "Throughput relative to `workers_2`:\n"
        "\n"
        "- `workers_2`: 1.00x\n"
        "- `workers_4`: 1.78x\n"
    ) in render(capsys, tmp_path, "process_scaling", entries)


def test_renders_async_over_sync_ratio_for_sync_vs_async(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    entries = [
        build_measurement(
            "async_c4",
            {
                "task_path": "tasks.sleep_async",
                "worker": {
                    "concurrency": 4,
                    "batch_size": None,
                    "poll_interval": 0.25,
                },
            },
            {"throughput_per_s": 200.0},
        ),
        build_measurement(
            "sync_c4",
            {
                "task_path": "tasks.sleep_sync",
                "worker": {
                    "concurrency": 4,
                    "batch_size": None,
                    "poll_interval": 0.25,
                },
            },
            {"throughput_per_s": 100.0},
        ),
    ]

    assert (
        "Async / sync throughput ratio at the same IO wait:\n\n- concurrency 4: 2.00x\n"
    ) in render(capsys, tmp_path, "sync_vs_async", entries)


def test_renders_checkpoint_multiplier_for_checkpoint_cost(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    entries = [
        build_measurement("flat", {}, {"throughput_per_s": 400.0}),
        build_measurement(
            "workflow",
            {"task_path": "tasks.run_steps"},
            {"throughput_per_s": 100.0},
        ),
    ]

    assert (
        "Checkpoint cost:\n\n- one `run_steps` task costs 4.00x a flat no-op task\n"
    ) in render(capsys, tmp_path, "checkpoint_cost", entries)


def test_falls_back_to_ratios_when_checkpoint_cost_lost_half_its_pair(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The multiplier needs both task paths, and the results file is rewritten after
    every measurement — so a run killed between the two leaves one on disk, and a
    stage with one row still deserves a table with a number under it."""
    entries = [build_measurement("flat", {}, {"throughput_per_s": 400.0})]

    assert ("Throughput relative to `flat`:\n\n- `flat`: 1.00x\n") in render(
        capsys, tmp_path, "checkpoint_cost", entries
    )


def test_renders_producer_columns_for_producer_ceiling(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    entries = [
        build_measurement(
            "single",
            {"mode": "producer"},
            {
                "count": 5000,
                "enqueues_per_s": 250.0,
                "enqueue_p50_s": 0.004,
                "enqueue_p99_s": 0.009,
            },
            {"range_high": 254.0, "range_low": 246.0, "spread": 0.03},
        )
    ]

    assert render(capsys, tmp_path, "producer_ceiling", entries) == (
        HEADER + "\n"
        "## Producer ceiling\n"
        "\n"
        "| mode | enqueues | enqueues/s | enqueue p50 s | enqueue p99 s "
        "| rep range | spread | cv | notes |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| single | 5000 | 250.0 | 0.00400 | 0.00900 | 246-254 | 3.0% | 2.0% |  |\n"
    )


def test_renders_idle_polling_tax_and_latency_ratios_for_poll_interval(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    entries = [
        build_measurement("poll_0.25", {"mode": "rate"}, {"end_to_end_p50_s": 0.02}),
        build_measurement(
            "poll_1",
            {
                "mode": "rate",
                "worker": {
                    "concurrency": 1,
                    "batch_size": None,
                    "poll_interval": 1.0,
                },
            },
            {"end_to_end_p50_s": 0.06},
        ),
    ]
    probes = [
        {"poll_interval": 0.05, "workers": 4, "claims_per_s_per_worker": 19.8},
        {"poll_interval": 1.0, "workers": 4, "claims_per_s_per_worker": 0.99},
    ]

    rendered = render(capsys, tmp_path, "poll_interval", entries, idle_probes=probes)

    assert (
        "Idle polling tax (workers parked on an empty queue):\n"
        "\n"
        "| poll interval s | workers | claims/s/worker | 1/poll_interval |\n"
        "| --- | --- | --- | --- |\n"
        "| 0.05 | 4 | 19.80 | 20.00 |\n"
        "| 1 | 4 | 0.99 | 1.00 |\n"
    ) in rendered
    assert (
        "End-to-end p50 relative to `poll_0.25`:\n"
        "\n"
        "- `poll_0.25`: 1.00x\n"
        "- `poll_1`: 3.00x\n"
    ) in rendered
