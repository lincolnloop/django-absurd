import json
import typing as t
from pathlib import Path

import pytest

import report

HOST = {
    "absurd_sdk": "0.5.0",
    "captured_at": "2026-08-27T12:00:00+00:00",
    # An ordinary server declares no `cluster_name` and reads back as the empty
    # string, which is what `db` under the suites gives too.
    "cluster_name": "",
    "cpu_count": 8,
    "django": "6.1",
    "git_sha": "deadbeef",
    "load_avg_1m": 0.5,
    "max_connections": "100",
    "postgres": "PostgreSQL 18.0 on x86_64-pc-linux-gnu",
    "python": "3.14.3",
    # A container limit is invisible to the server, and `db` under the suites runs
    # under none, so an unset variable is what an ordinary run records.
    "requested_container_cpus": None,
    "requested_container_memory": None,
    "shared_buffers": "128MB",
}

OPTIONS = {
    "duration_s": None,
    "io_seconds": 0.05,
    "max_workers": 8,
    "reps": 3,
    "tasks": None,
}

# What a probe's timed rounds reduce to, the way `analysis.summarize_commit_rates`
# records them. A median with its own spread, because the durable rate is a wide
# distribution rather than a constant, and every share the report prints against it
# has to carry that.
COMMIT_CEILING = {
    "commit_ceiling_durable": {
        "median_per_s": 2016.0,
        "cv": 0.279,
        "range_low": 1600.0,
        "range_high": 2262.0,
    },
    "commit_ceiling_nondurable": {
        "median_per_s": 367723.0,
        "cv": 0.052,
        "range_low": 324000.0,
        "range_high": 380000.0,
    },
    "commit_ceiling_durable_after": {
        "median_per_s": 1904.0,
        "cv": 0.114,
        "range_low": 1700.0,
        "range_high": 2100.0,
    },
}

# One rep's itemised per-task cost, the way `analysis.build_statement_stats` records
# it: the statements the phase issued, costliest server time first, with the nested
# ones Absurd's PL/pgSQL ran inside the call above them. More of them than a report
# prints, and one wrapped the way Postgres normalises long SQL.
STATEMENT_STATS = {
    "statements": [
        {
            "query": (
                "update absurd.t_bench\n  set state = $1,\n      updated_at = now()\n"
                "  where task_id = $2 and state = $3 and attempt = $4\n"
                "  returning task_id, state, attempt"
            ),
            "toplevel": False,
            "calls_per_task": 3.0,
            "total_exec_ms_per_task": 0.91,
            "rows_per_task": 3.0,
        },
        {
            "query": "select * from absurd.claim_task($1, $2)",
            "toplevel": True,
            "calls_per_task": 1.0,
            "total_exec_ms_per_task": 0.62,
            "rows_per_task": 1.0,
        },
        {
            "query": "insert into absurd.r_bench (task_id, attempt) values ($1, $2)",
            "toplevel": True,
            "calls_per_task": 1.0,
            "total_exec_ms_per_task": 0.41,
            "rows_per_task": 1.0,
        },
        {
            "query": "commit",
            "toplevel": True,
            "calls_per_task": 1.0,
            "total_exec_ms_per_task": 0.3,
            "rows_per_task": 0.0,
        },
        {
            "query": "select count(*) from absurd.t_bench where state <> $1",
            "toplevel": True,
            "calls_per_task": 0.02,
            "total_exec_ms_per_task": 0.11,
            "rows_per_task": 0.02,
        },
        {
            "query": "select pg_stat_force_next_flush()",
            "toplevel": True,
            "calls_per_task": 0.01,
            "total_exec_ms_per_task": 0.02,
            "rows_per_task": 0.01,
        },
        {
            "query": "select now()",
            "toplevel": True,
            "calls_per_task": 0.01,
            "total_exec_ms_per_task": 0.01,
            "rows_per_task": 0.01,
        },
    ],
    "wall_ms_per_task": 7.89,
    "server_exec_ms_per_task": 2.1,
    "client_ms_per_task": 5.79,
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
    "- host cpu count: 8, load average (1m): 0.50\n"
    "- server: shared_buffers 128MB, max_connections 100, requested container "
    "limits: cpus unknown, memory unknown\n"
    "- commit ceiling (commits/s, one connection): durable 2016 (cv 28%, "
    "1600-2262), after the run 1904 (cv 11%, 1700-2100), non-durable 367723 "
    "(cv 5%, 324000-380000), 182x without fsync\n"
    "- python 3.14.3, Django 6.1, absurd-sdk 0.5.0\n"
    "- postgres: PostgreSQL 18.0 on x86_64-pc-linux-gnu, cluster unnamed\n"
    "\n"
    "Marks: `!` invalid — a rep measured something other than what was asked (a "
    "redelivery, a task that never completed, a window too short to divide by, an "
    "under-offered rate, a paced offer whose backlog was still growing when it "
    "stopped). `~` unstable — the reps measured the right thing and "
    "disagreed, beyond the measurement's CV limit. `?` — fewer than two valid reps, "
    "so dispersion was never measured. A marked measurement stays in every table and "
    "in every number derived below one.\n"
    "\n"
    "`backlog` is the `--tasks` depth a saturation rep preloaded and then drained, "
    "blank in rate mode, which preloads nothing. Throughput RISES as a backlog "
    "drains, so a saturation rate averages a curve whose starting depth is in this "
    "column: two rows with different backlogs are two different experiments, and "
    "anything derived across them carries a depth penalty as well as whatever it "
    "meant to measure.\n"
    "\n"
    "`rep range`, `spread` and `cv` are over the metric the reps were ranked on: "
    "tasks/s in saturation mode, end-to-end p50 s in rate mode, enqueues/s in "
    "producer mode. `spread` is `(max - min) / median`, shown because the endpoints "
    "are what a reader wants; `cv` is stdev/mean and is what `~` is thresholded on, "
    "because a range grows with `--reps` on unchanged data and a CV does not.\n"
)

MEASUREMENT_TABLE_HEAD = (
    "| measurement | mode | backlog | workers | concurrency | batch | poll | tasks/s "
    "| e2e p50 s | e2e p90 s | e2e p99 s | rep range | spread | cv | notes |\n"
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- "
    "| --- | --- | --- |\n"
)


# The two shapes of one pooled_vs_split pair: four slots in one process, against one
# slot in each of four processes.
POOLED_WORKER = {"concurrency": 4, "batch_size": None, "poll_interval": 0.25}
SPLIT_WORKER = {"concurrency": 1, "batch_size": None, "poll_interval": 0.25}


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
        # The depth a rung preloaded, which the table prints because a saturation rate
        # averages the drain of it and two different ones are two experiments.
        "tasks": 5000,
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
            {
                "stage": stage,
                "options": OPTIONS,
                **COMMIT_CEILING,
                "measurements": entries,
                **extra,
            }
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


def build_rate_ramp(**overrides: t.Any) -> dict[str, t.Any]:
    """The ramp a rate stage records, the way `stages.measure_sustainable_rate` does.

    Three probes climbing towards the drain rate: two the fleet absorbed and the one it
    did not, whose backlog doubled across its own offer window. The refused one has
    the producer delivering its whole target, which is what makes the refusal the
    fleet's — the other case is `build_producer_bound_ramp`.
    """
    return {
        "drain_ceiling_per_s": 4200.0,
        "offer_seconds": 20.0,
        "rate_per_s": 1400.0,
        "sustained": True,
        "bracket_high_per_s": 2100.0,
        "probes": [
            {
                "rate_per_s": 933.3,
                "sustained": True,
                "rep": {
                    "achieved_rate_per_s": 933.2,
                    "offered_ok": True,
                    "throughput_per_s": 933.0,
                    "end_to_end_p50_s": 0.014,
                    "backlog_mid": 30,
                    "backlog_end": 26,
                },
            },
            {
                "rate_per_s": 1400.0,
                "sustained": True,
                "rep": {
                    "achieved_rate_per_s": 1399.8,
                    "offered_ok": True,
                    "throughput_per_s": 1399.1,
                    "end_to_end_p50_s": 0.018,
                    "backlog_mid": 42,
                    "backlog_end": 39,
                },
            },
            {
                "rate_per_s": 2100.0,
                "sustained": False,
                "rep": {
                    "achieved_rate_per_s": 2099.6,
                    "offered_ok": True,
                    "throughput_per_s": 1704.0,
                    "end_to_end_p50_s": 8.4,
                    "backlog_mid": 24000,
                    "backlog_end": 51000,
                },
            },
        ],
        **overrides,
    }


def build_producer_bound_ramp() -> dict[str, t.Any]:
    """The same ramp, refused by an offer the PRODUCER never delivered.

    Both sides run on one box, so the enqueue loop misses its own deadlines before the
    fleet runs out — which is what the recorded ramps did. The rep is otherwise the
    refused probe's: a backlog that grew, on an offer nobody made in full.
    """
    ramp = build_rate_ramp()
    ramp["probes"][-1]["rep"] |= {
        "achieved_rate_per_s": 1890.0,
        "offered_ok": False,
        "throughput_per_s": 1804.0,
    }
    return ramp


def build_pooled_vs_split_entries() -> list[dict[str, t.Any]]:
    """One pair, measured both ways round, the way the stage records it."""
    return [
        build_measurement(
            "pooled_4",
            {"workers": 1, "worker": POOLED_WORKER},
            {"throughput_per_s": 400.0},
            {"range_high": 440.0, "range_low": 360.0},
        ),
        build_measurement(
            "split_4",
            {"workers": 4, "worker": SPLIT_WORKER},
            {"throughput_per_s": 600.0},
            {"range_high": 660.0, "range_low": 540.0},
        ),
    ]


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
        + "| concurrency_1 | saturation | 5000 | 1 | 1 | default | 0.25 "
        "| 412.5 |  |  |  | 412.5-412.5 | 4.0% | 2.0% |  |\n"
        "| concurrency_2 | saturation | 5000 | 1 | 2 | 4 | 0.25 "
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
        + "| concurrency_1 | saturation | 5000 | 1 | 1 | default | 0.25 "
        "| 412.5 |  |  |  | 0-500 | 4.0% | 2.0% | ! |\n"
        "| concurrency_2 | saturation | 5000 | 1 | 1 | default | 0.25 "
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
        + "| concurrency_1 | saturation | 5000 | 1 | 1 | default | 0.25 "
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
        + "| concurrency_1 | saturation | 5000 | 1 | 1 | default | 0.25 "
        "| 412.5 |  |  |  | 412.5-412.5 | 4.0% | 2.0% |  |\n"
        "| rate_25pct | rate |  | 1 | 1 | default | 0.25 "
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
        + "| concurrency_1 | saturation | 5000 | 1 | 1 | default | 0.25 "
        "| 0.0 |  |  |  | n/a | n/a | n/a | ! ? |\n"
        "| concurrency_2 | saturation | 5000 | 1 | 1 | default | 0.25 "
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


def test_reports_a_container_limit_as_requested_rather_than_as_measured(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A container CPU or memory limit reaches no SQL, so what a results file carries
    is what was asked for and nothing better. It prints under that label and on the
    server's own line, away from the host core count, which counts the cores the
    worker fleet had and would otherwise be read as the server's allowance."""
    entries = [
        build_measurement(
            "concurrency_1",
            {},
            {},
            host={
                **HOST,
                "requested_container_cpus": "4",
                "requested_container_memory": "8g",
            },
        )
    ]

    rendered = render(capsys, tmp_path, "worker_knobs", entries)

    assert (
        "- server: shared_buffers 128MB, max_connections 100, requested container "
        "limits: cpus 4, memory 8g\n"
    ) in rendered


def test_reports_mixed_server_resources_when_stages_measured_different_servers(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """These settings are env-overridable per machine and a partial re-run is a
    documented workflow, so a directory can hold one stage measured at 1GB of shared
    buffers and one at 256MB. That reads as mixed the way a git SHA does: the rates in
    the file no longer compare, and the line has to be the thing that says so."""
    entries = [
        build_measurement("concurrency_1", {}, {}),
        build_measurement(
            "concurrency_2",
            {},
            {},
            host={**HOST, "max_connections": "50", "shared_buffers": "1GB"},
        ),
    ]

    rendered = render(capsys, tmp_path, "worker_knobs", entries)

    assert (
        "- server: shared_buffers mixed (128MB, 1GB), "
        "max_connections mixed (100, 50), "
        "requested container limits: cpus unknown, memory unknown\n"
    ) in rendered


def test_says_a_run_on_ram_is_for_comparing_and_not_for_publishing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The RAM-backed server's whole point is that its commit ceiling stops reaching
    the measurement — 56,659 commits/s against 584 off a Docker volume — and that is
    also how its numbers become unpublishable. A reader looking at a five-figure
    ceiling has to be told which of those it is looking at, so the line sits directly
    under it and names the consequence rather than the medium alone."""
    entries = [
        build_measurement(
            "concurrency_1", {}, {}, host={**HOST, "cluster_name": "bench-tmpfs"}
        )
    ]

    rendered = render(capsys, tmp_path, "worker_knobs", entries)

    assert (
        "- storage: RAM. The data directory is a tmpfs, so fsync costs a memcpy and "
        "every rate here is for COMPARING configurations. None of them is a "
        "durable-storage figure, and none may be published as a property of "
        "django-absurd.\n"
        "- python 3.14.3, Django 6.1, absurd-sdk 0.5.0\n"
        "- postgres: PostgreSQL 18.0 on x86_64-pc-linux-gnu, cluster `bench-tmpfs`\n"
    ) in rendered


def test_warns_about_ram_when_only_some_measurements_came_off_it(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A partial re-run against another server is a documented workflow, so a
    directory can hold one stage measured on RAM and one on a disk. One is enough:
    the absolute rates in that file no longer compare with each other, which is the
    warning, and the cluster names read as mixed the way a git SHA does."""
    entries = [
        build_measurement(
            "concurrency_1", {}, {}, host={**HOST, "cluster_name": "bench-tmpfs"}
        ),
        build_measurement("concurrency_2", {}, {}),
    ]

    rendered = render(capsys, tmp_path, "worker_knobs", entries)

    assert (
        "- storage: RAM. The data directory is a tmpfs, so fsync costs a memcpy and "
        "every rate here is for COMPARING configurations. None of them is a "
        "durable-storage figure, and none may be published as a property of "
        "django-absurd.\n"
    ) in rendered
    assert (
        "- postgres: PostgreSQL 18.0 on x86_64-pc-linux-gnu, "
        "cluster mixed (`bench-tmpfs`, unnamed)\n"
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
    """The single-worker rung is the divisor, so its own spread widens every rung.

    Each rung's backlog prints with its efficiency: the ladder sizes its preload from
    the worker count, so the divisor is usually the shallowest rung in the stage.
    """
    entries = [
        build_measurement(
            "workers_1",
            {"tasks": 4000, "workers": 1},
            {"throughput_per_s": 100.0},
            {"range_high": 110.0, "range_low": 90.0},
        ),
        build_measurement(
            "workers_2", {"tasks": 4000, "workers": 2}, {"throughput_per_s": 180.0}
        ),
    ]

    assert (
        "Scaling efficiency `T(N) / (N x T(1))`:\n"
        "\n"
        "- 1 worker(s): 1.00 (reps 0.82-1.22), backlog 4000\n"
        "- 2 worker(s): 0.90 (reps 0.82-1.00), backlog 4000\n"
    ) in render(capsys, tmp_path, "process_scaling", entries)


def test_says_a_scaling_ladder_measured_at_two_depths_confounded_itself(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The ladder sizes its preload from the worker count, so its rungs drain
    different depths and the efficiency divides one depth by another.

    A deeper backlog is slower and the deeper rung is always the numerator, so the
    direction is known even though the size is not — which makes every figure a lower
    bound rather than a measurement, and it has to say so where it is read.
    """
    entries = [
        build_measurement(
            "workers_1", {"tasks": 4000, "workers": 1}, {"throughput_per_s": 100.0}
        ),
        build_measurement(
            "workers_5", {"tasks": 10000, "workers": 5}, {"throughput_per_s": 300.0}
        ),
    ]

    assert (
        "Scaling efficiency `T(N) / (N x T(1))`:\n"
        "\n"
        "- 1 worker(s): 1.00, backlog 4000\n"
        "- 5 worker(s): 0.60, backlog 10000\n"
        "\n"
        "CONFOUNDED: these rungs drained different backlogs (4000 to 10000, against "
        "4000 at one worker) and a deeper backlog is slower, so each efficiency "
        "divides a deeper measurement by a shallower one. Read them as lower bounds "
        "on the scaling, not as measurements of it; only a ladder run at one depth "
        "would separate the two.\n"
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


def test_says_what_bound_each_saturation_measurement(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A task rate near one connection's commit ceiling is a measurement of Postgres.

    Throughput times the commits a task cost is the commit rate the run asked of the
    database; over the worker count it is what ONE worker's connection carried, which
    is the comparable quantity — a worker opens the SDK's single connection whatever
    its concurrency, and the ceiling is one connection's. So `split_4` demands more of
    the database than `concurrency_16` and is still the freer of the two.

    The verdict is read off the band the durable probes' own spread puts around that
    rate, and off no median: a row whose band lies across the line is reported as
    unresolved rather than assigned to whichever side one draw of the calibration
    happened to favour. Both probes agree here, so the band is what either would have
    drawn on its own — the widening a disagreement earns is the neighbouring test. The
    paced row is left out — its rate was set by the offer, so
    its share says how big the offer was and nothing about what bound it — and a row
    whose reps left no median says so rather than reading as free.
    """
    entries = [
        build_measurement(
            "concurrency_1",
            {},
            {"throughput_per_s": 150.0, "commits_per_task": 3.55},
        ),
        build_measurement(
            "concurrency_16",
            {},
            {"throughput_per_s": 548.0, "commits_per_task": 3.55},
        ),
        build_measurement(
            "concurrency_8",
            {},
            {"throughput_per_s": 380.0, "commits_per_task": 3.55},
        ),
        build_measurement(
            "split_4",
            {"workers": 4, "worker": SPLIT_WORKER},
            {"throughput_per_s": 1200.0, "commits_per_task": 3.55},
        ),
        build_measurement("concurrency_32", {}, {"throughput_per_s": 0.0}),
        build_measurement("rate_25pct", {"mode": "rate"}, {"throughput_per_s": 90.0}),
    ]

    assert (
        "Commit budget (`tasks/s x commits/task / workers`, against one connection's "
        "durable ceiling):\n"
        "\n"
        "- `concurrency_1`: 532 commits/s per worker connection (150.0 x 3.55 / 1), "
        "24%-33% of the durable ceiling across both probes — client-bound\n"
        "- `concurrency_16`: 1945 commits/s per worker connection (548.0 x 3.55 / 1), "
        "86%-122% of the durable ceiling across both probes — connection-bound\n"
        "- `concurrency_8`: 1349 commits/s per worker connection (380.0 x 3.55 / 1), "
        "60%-84% of the durable ceiling across both probes — unresolved: the "
        "ceiling's spread straddles the line\n"
        "- `split_4`: 1065 commits/s per worker connection (1200.0 x 3.55 / 4), "
        "47%-67% of the durable ceiling across both probes — client-bound\n"
        "- `concurrency_32`: commits per task not recorded, so nothing says what "
        "bound it\n"
    ) in render(capsys, tmp_path, "worker_knobs", entries)


def test_softens_the_bound_verdict_when_the_calibration_disagrees_with_itself(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A ceiling that repeated badly cannot support a crisp verdict, and gets none.

    One measurement, one probe, two recorded spreads: the endpoints and the CV come off
    the same probe rounds, so a probe whose CV is high has already recorded a wide
    `range_low`-`range_high` and the band the verdict is read off widens with it. That
    is why nothing here reads the CV a second time — a row that was called client-bound
    against a ceiling that repeated to 1% reads `unresolved` against one that repeated
    to 14%, which is the whole of the widening one probe's own dispersion earns. No
    closing probe here, so that widening is the only one in play.
    """
    entries = [
        build_measurement(
            "concurrency_4",
            {},
            {"throughput_per_s": 400.0, "commits_per_task": 3.0},
        )
    ]

    assert (
        "- `concurrency_4`: 1200 commits/s per worker connection (400.0 x 3.00 / 1), "
        "59%-62% of the durable ceiling across the opening probe alone — client-bound\n"
    ) in render(
        capsys,
        tmp_path,
        "worker_knobs",
        entries,
        commit_ceiling_durable={
            "median_per_s": 2000.0,
            "cv": 0.012,
            "range_low": 1950.0,
            "range_high": 2050.0,
        },
        commit_ceiling_durable_after=None,
    )
    assert (
        "- `concurrency_4`: 1200 commits/s per worker connection (400.0 x 3.00 / 1), "
        "46%-80% of the durable ceiling across the opening probe alone — unresolved: "
        "the ceiling's spread straddles the line\n"
    ) in render(
        capsys,
        tmp_path,
        "worker_knobs",
        entries,
        commit_ceiling_durable={
            "median_per_s": 2000.0,
            "cv": 0.136,
            "range_low": 1500.0,
            "range_high": 2600.0,
        },
        commit_ceiling_durable_after=None,
    )


def test_bands_the_bound_verdict_across_both_durable_probes(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A ceiling that moved DURING the run calibrates nothing, and must not pretend to.

    Run B's real probes: 615/s opening, 2,100/s closing, against the same server. Its
    opening probe alone would call this row connection-bound and print that verdict
    against every row in the file, when the machine in fact crossed between storage
    regimes mid-run and nothing in the run is internally comparable. The band taken
    across both probes straddles the line, and `unresolved` is the honest answer.
    """
    entries = [
        build_measurement(
            "concurrency_8",
            {},
            {"throughput_per_s": 250.0, "commits_per_task": 2.0},
        )
    ]
    drifted = {
        "commit_ceiling_durable": {
            "median_per_s": 615.0,
            "cv": 0.136,
            "range_low": 465.0,
            "range_high": 673.0,
        },
        "commit_ceiling_durable_after": {
            "median_per_s": 2100.0,
            "cv": 0.024,
            "range_low": 2030.0,
            "range_high": 2139.0,
        },
    }

    assert (
        "- `concurrency_8`: 500 commits/s per worker connection (250.0 x 2.00 / 1), "
        "23%-108% of the durable ceiling across both probes — unresolved: the "
        "ceiling's spread straddles the line\n"
    ) in render(capsys, tmp_path, "worker_knobs", entries, **drifted)


def test_bands_against_the_opening_probe_when_the_run_recorded_no_closing_one(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The closing probe is absent until a run finishes, and can be refused outright.

    The band then comes off the opening probe alone and the row says so, because a
    reader comparing two reports has to tell a band that widened because the machine
    drifted mid-run from one that widened because the disk got worse.
    """
    entries = [
        build_measurement(
            "concurrency_8",
            {},
            {"throughput_per_s": 250.0, "commits_per_task": 2.0},
        )
    ]

    assert (
        "- `concurrency_8`: 500 commits/s per worker connection (250.0 x 2.00 / 1), "
        "74%-108% of the durable ceiling across the opening probe alone — "
        "connection-bound\n"
    ) in render(
        capsys,
        tmp_path,
        "worker_knobs",
        entries,
        commit_ceiling_durable={
            "median_per_s": 615.0,
            "cv": 0.136,
            "range_low": 465.0,
            "range_high": 673.0,
        },
        commit_ceiling_durable_after=None,
    )


def test_says_where_each_measurement_spent_its_time_per_task(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The commit budget says what a task asked of the disk; this says who asked.

    Every statement the phase issued, how many times each ran for one task and what it
    cost the server, then the wall clock left over — which is the number that separates
    Absurd's SQL being expensive from the harness's own Python being expensive. Nested
    rows are marked because only the top-level ones sum to the server side: a nested
    statement ran inside a call the row above it already charged for.

    Capped at the costliest few, since the tail is microsecond bookkeeping repeated
    under every row in the stage, and each statement is one line however Postgres
    wrapped it. A saturation row whose reps recorded none says so, and the paced row is
    left out — its rate was the offer's, so ms per task says how big the offer was.
    """
    entries = [
        build_measurement("concurrency_1", {}, {"statement_stats": STATEMENT_STATS}),
        build_measurement("concurrency_2", {}, {}),
        build_measurement("rate_25pct", {"mode": "rate"}, {}),
    ]

    assert (
        "Per-task cost (median rep, ms/task; server time is summed over every backend "
        "the phase used, so above one worker it counts concurrent work against one "
        "wall clock):\n"
        "\n"
        "- `concurrency_1`: 7.89 wall = 2.10 server + 5.79 client\n"
        "  - 3.00 calls x 0.910 ms nested: `update absurd.t_bench set state = $1, "
        "updated_at = now() where task_id = $2 and state = $3 and attempt = $4 re…`\n"
        "  - 1.00 calls x 0.620 ms: `select * from absurd.claim_task($1, $2)`\n"
        "  - 1.00 calls x 0.410 ms: `insert into absurd.r_bench (task_id, attempt) "
        "values ($1, $2)`\n"
        "  - 1.00 calls x 0.300 ms: `commit`\n"
        "  - 0.02 calls x 0.110 ms: `select count(*) from absurd.t_bench where "
        "state <> $1`\n"
        "- `concurrency_2`: no statement stats recorded, so nothing itemises it\n"
    ) in render(capsys, tmp_path, "worker_knobs", entries)


def test_says_the_commit_ceiling_is_missing_rather_than_omitting_it(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A run whose calibration probe was refused still runs and still reports.

    What it must not do is read like a calibrated one: the header says the ceiling is
    missing and every commit budget under it says it has nothing to be measured
    against, rather than the two lines quietly disappearing.
    """
    entries = [
        build_measurement(
            "concurrency_1",
            {},
            {"throughput_per_s": 150.0, "commits_per_task": 3.55},
        )
    ]
    rendered = render(
        capsys, tmp_path, "worker_knobs", entries, **dict.fromkeys(COMMIT_CEILING)
    )

    assert (
        "- commit ceiling (commits/s, one connection): not measured, so nothing below "
        "says whether a throughput is this connection's number or Absurd's\n"
    ) in rendered
    assert (
        "- `concurrency_1`: 532 commits/s per worker connection (150.0 x 3.55 / 1), "
        "against no ceiling — nothing here says what bound it\n"
    ) in rendered


def test_says_which_halves_of_the_commit_ceiling_were_measured(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The three probes are three separate attempts and any of them can be refused.

    A run killed before its closing probe has no after-the-run ceiling and no ratio to
    quote, which the header names one field at a time rather than dropping the whole
    line for the one number it is missing.
    """
    entries = [build_measurement("concurrency_1", {}, {})]

    rendered = render(
        capsys,
        tmp_path,
        "worker_knobs",
        entries,
        commit_ceiling_durable_after=None,
        commit_ceiling_nondurable=None,
    )

    assert (
        "- commit ceiling (commits/s, one connection): durable 2016 (cv 28%, "
        "1600-2262), after the run not measured, non-durable not measured, "
        "ratio not measured\n"
    ) in rendered


def test_names_the_measurement_a_calibrated_stage_was_configured_from(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The working point cascades into every stage that reads an earlier one back.

    Preferring a rung that measured what was asked and repeated is right, but when
    most rungs are marked the choice lands on the slowest survivor and every number in
    this stage was measured at it — so the stage says what it inherited and how well
    that rung held up.
    """
    entries = [build_measurement("workers_1", {"workers": 1}, {})]
    calibration = {
        "stage": "worker_knobs",
        "measurement": "concurrency_8",
        "throughput_per_s": 397.2,
        "cv": 0.31,
        "invalid": False,
        "unstable": True,
    }

    assert (
        "## Process scaling\n"
        "\n"
        "Calibrated from `worker_knobs` `concurrency_8`: 397.2 tasks/s, cv 31.0%, "
        "unstable.\n"
    ) in render(capsys, tmp_path, "process_scaling", entries, calibration=calibration)


def test_names_a_working_point_that_was_never_in_doubt(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A rung with nothing wrong with it says so, so the reader can stop looking."""
    entries = [build_measurement("flat", {}, {})]
    calibration = {
        "stage": "worker_knobs",
        "measurement": "batch_2",
        "throughput_per_s": 158.0,
        "cv": 0.037,
        "invalid": False,
        "unstable": False,
    }

    assert (
        "Calibrated from `worker_knobs` `batch_2`: 158.0 tasks/s, cv 3.7%, "
        "valid and stable.\n"
    ) in render(capsys, tmp_path, "checkpoint_cost", entries, calibration=calibration)


def test_names_a_working_point_nothing_could_be_said_about(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """`--reps 1` is a dry run, so its working point has an unmeasured dispersion.

    An invalid rung is a working point too — calibrating on nothing would be worse —
    and both faults print, because a stage configured from a rung that measured the
    wrong thing is the case this line exists for.
    """
    entries = [build_measurement("flat", {}, {})]
    calibration = {
        "stage": "worker_knobs",
        "measurement": "concurrency_1",
        "throughput_per_s": 12.5,
        "cv": None,
        "invalid": True,
        "unstable": False,
    }

    assert (
        "Calibrated from `worker_knobs` `concurrency_1`: 12.5 tasks/s, cv n/a, "
        "invalid, dispersion unmeasured.\n"
    ) in render(capsys, tmp_path, "checkpoint_cost", entries, calibration=calibration)


def test_renders_the_ramp_that_measured_a_rate_stage_its_offer_rate(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A rung's offered rate means nothing without the ramp that chose it.

    The two rates in the block are different quantities: the fleet drained 4200/s off
    a backlog and absorbed 1400/s as it arrived, which is why fractions of the drain
    rate over-offered. Each probe carries what the producer actually delivered beside
    the verdict, because a refusal only bounds the fleet if the offer was made in full.
    """
    entries = [build_measurement("rate_25pct", {"mode": "rate"}, {})]

    rendered = render(
        capsys,
        tmp_path,
        "latency_under_load",
        entries,
        sustainable_rate=build_rate_ramp(),
    )

    assert (
        "Offer rate: 1400.0/s, the highest offer this ramp got through cleanly — the "
        "LOWER of the fleet's knee and the producer's own ceiling, and the ramp does "
        "not say which of the two it found. It refused 2100.0/s with the producer "
        "still delivering its target, so that refusal is the FLEET's and the knee is "
        "between the two; nothing here refines it. The rows above offer fractions of "
        "it. The drain rate this stage calibrated from was 4200.0/s, which is what "
        "the fleet completes with a backlog already waiting rather than what it can "
        "absorb as it arrives.\n"
        "\n"
        "| offered/s | offer s | achieved/s | producer kept up | absorbed "
        "| e2e p50 s | backlog at midpoint | backlog at end |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        "| 933.3 | 20 | 933.2 | yes | yes | 0.0140 | 30 | 26 |\n"
        "| 1400.0 | 20 | 1399.8 | yes | yes | 0.0180 | 42 | 39 |\n"
        "| 2100.0 | 20 | 2099.6 | yes | no | 8.4000 | 24000 | 51000 |\n"
    ) in rendered


def test_says_a_ramp_stopped_by_the_producer_bounds_the_enqueue_side(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The refusal that reads as the fleet's limit and is not.

    Producer and workers share the box, so the enqueue loop can miss its own deadlines
    before the fleet runs out — and `absorbed` alone cannot tell that apart from a
    fleet that fell behind. The rate the fleet DID complete in the refused probe is
    printed with it, because it is a floor on the fleet the ramp never reached.
    """
    entries = [build_measurement("rate_25pct", {"mode": "rate"}, {})]

    rendered = render(
        capsys,
        tmp_path,
        "latency_under_load",
        entries,
        sustainable_rate=build_producer_bound_ramp(),
    )

    assert (
        "It refused 2100.0/s, but the producer itself only achieved 1890.0/s there, "
        "so that refusal bounds the ENQUEUE side and not the fleet — which completed "
        "1804.0/s inside the same probe. The fleet's own knee is somewhere above "
        "that, unmeasured."
    ) in rendered
    assert "| 2100.0 | 20 | 1890.0 | no | no | 8.4000 | 24000 | 51000 |\n" in rendered


def test_says_a_ramp_that_ran_out_of_ceiling_never_found_the_knee(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A ramp climbs no higher than the drain rate, which nothing can be offered above.

    Absorbing every probe below it leaves the knee at or above the top of the ramp
    rather than between two probes, so there is no bracket to print.
    """
    entries = [build_measurement("rate_25pct", {"mode": "rate"}, {})]
    absorbed = [probe for probe in build_rate_ramp()["probes"] if probe["sustained"]]

    rendered = render(
        capsys,
        tmp_path,
        "latency_under_load",
        entries,
        sustainable_rate=build_rate_ramp(probes=absorbed, bracket_high_per_s=None),
    )

    assert (
        "Offer rate: 1400.0/s, the highest offer this ramp got through cleanly — the "
        "LOWER of the fleet's knee and the producer's own ceiling, and the ramp does "
        "not say which of the two it found. The ramp ran out of climb below the drain "
        "rate without finding an offer it could not, so the knee is at or above the "
        "top of the ramp."
    ) in rendered


def test_says_a_ramp_whose_every_offer_diverged_measured_at_an_unproven_rate(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Nothing absorbed means the rungs are fractions of a rate never demonstrated.

    The stage measures at the lowest rate it probed rather than refusing, so the block
    has to say the number was not measured — the marks on the rows above are then the
    finding rather than a rate to read.
    """
    entries = [build_measurement("rate_25pct", {"mode": "rate"}, {})]
    refused = [probe for probe in build_rate_ramp()["probes"] if not probe["sustained"]]

    rendered = render(
        capsys,
        tmp_path,
        "latency_under_load",
        entries,
        sustainable_rate=build_rate_ramp(
            probes=refused, rate_per_s=2100.0, sustained=False
        ),
    )

    assert (
        "Offer rate: 2100.0/s — the LOWEST rate the ramp probed, and one it did not "
        "absorb, so every rung above is a fraction of an unproven rate and the marks "
        "on them are the finding. The drain rate this stage calibrated from was "
        "4200.0/s, which is what the fleet completes with a backlog already waiting "
        "rather than what it can absorb as it arrives.\n"
    ) in rendered


def test_renders_split_over_pooled_throughput_for_pooled_vs_split(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The one number the stage exists for, with both arms' spreads carried through.

    Split over pooled, so a ratio above 1 reads as the split arm winning — and the
    order the arms ran in prints under it, because an arm that always went first would
    have drained the emptier tables every rep and no column in the table says so.
    """
    rendered = render(
        capsys,
        tmp_path,
        "pooled_vs_split",
        build_pooled_vs_split_entries(),
        run_order=["pooled_4", "split_4", "split_4", "pooled_4"],
    )

    assert (
        "Split / pooled throughput at the same total concurrency:\n"
        "\n"
        "- total 4: 1.50x (reps 1.23-1.83x)\n"
    ) in rendered
    assert (
        "Arms ran in this order, reversed each rep so neither always went first: "
        "pooled_4, split_4, split_4, pooled_4.\n"
    ) in rendered


def test_says_the_two_shapes_of_a_pair_opened_different_numbers_of_backends(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A worker process opens two backends whatever its concurrency, so the two ways
    of reaching one total concurrency do not reach it on the same connections.

    That is a confound in the stage's own comparison and the report says so rather
    than leaving a ratio to be read as the claim path alone.
    """
    shapes = [
        {
            "measurement": "pooled_4",
            "processes": 1,
            "concurrency": 4,
            "connections": 2,
        },
        {
            "measurement": "split_4",
            "processes": 4,
            "concurrency": 1,
            "connections": 8,
        },
    ]

    assert (
        "Postgres backends each shape opened (measured across starting its fleet):\n"
        "\n"
        "| measurement | processes | concurrency | connections |\n"
        "| --- | --- | --- | --- |\n"
        "| pooled_4 | 1 | 4 | 2 |\n"
        "| split_4 | 4 | 1 | 8 |\n"
        "\n"
        "At total 4 the two shapes opened different numbers of backends — a pooled "
        "arm's slots share the connections of the one process holding them, a split "
        "arm gets a set per process. Every ratio below is the claim path AND the "
        "connection count, and nothing here separates the two.\n"
    ) in render(
        capsys,
        tmp_path,
        "pooled_vs_split",
        build_pooled_vs_split_entries(),
        shape_connections=shapes,
    )


def test_says_a_pair_whose_shapes_opened_the_same_backends_is_clean(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The other reading of the same measurement, which is why it is measured.

    If a pooled arm ever opens a connection per slot the ratio becomes the claim path
    and nothing else, and the report has to be able to say that too.
    """
    shapes = [
        {
            "measurement": "pooled_4",
            "processes": 1,
            "concurrency": 4,
            "connections": 8,
        },
        {
            "measurement": "split_4",
            "processes": 4,
            "concurrency": 1,
            "connections": 8,
        },
    ]

    assert (
        "Both shapes of every pair opened the same number of backends, so a ratio "
        "below is the claim path and nothing else.\n"
    ) in render(
        capsys,
        tmp_path,
        "pooled_vs_split",
        build_pooled_vs_split_entries(),
        shape_connections=shapes,
    )


def test_names_the_pooled_vs_split_pairs_the_worker_bound_refused(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A pair the bound could not run is missing from the table, so it is named.

    Running its pooled arm at 1x8 against a split arm bounded to 4x1 would be an
    unequal comparison presented as an equal one, so neither arm runs — and a table
    two rows short with nothing said about it reads like a run that crashed.
    """
    assert (
        "Pairs not run:\n"
        "\n"
        "- total 8: `--max-workers 4` cannot spawn the 8 processes its split arm "
        "needs, so neither arm of the pair was measured\n"
    ) in render(
        capsys,
        tmp_path,
        "pooled_vs_split",
        build_pooled_vs_split_entries(),
        skipped_pairs=[{"total": 8, "max_workers": 4}],
    )


def test_falls_back_to_ratios_when_a_pooled_vs_split_pair_lost_an_arm(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The file is rewritten after every rep, so a run killed between a pair's two
    arms leaves one of them on disk, and one row still deserves a number under it."""
    entries = [
        build_measurement(
            "pooled_4",
            {"workers": 1, "worker": POOLED_WORKER},
            {"throughput_per_s": 400.0},
        )
    ]

    assert ("Throughput relative to `pooled_4`:\n\n- `pooled_4`: 1.00x\n") in render(
        capsys, tmp_path, "pooled_vs_split", entries
    )


def test_renders_a_stage_that_measured_nothing_beside_one_that_did(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A stage can refuse every measurement it was asked for and still have a section.

    An empty table under a heading that says which pairs were refused and why is the
    honest rendering; dropping the stage would leave the reader to notice its absence.
    """
    write_stage(tmp_path, "worker_knobs", [build_measurement("concurrency_1", {}, {})])

    rendered = render(
        capsys,
        tmp_path,
        "pooled_vs_split",
        [],
        skipped_pairs=[{"total": 4, "max_workers": 1}],
    )

    assert (
        "## Pooled vs split\n"
        "\n" + MEASUREMENT_TABLE_HEAD + "\n"
        "Pairs not run:\n"
        "\n"
        "- total 4: `--max-workers 1` cannot spawn the 4 processes its split arm "
        "needs, so neither arm of the pair was measured\n"
    ) in rendered


def test_says_so_when_every_stage_file_measured_nothing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The header is built out of measurement provenance, so a directory holding only
    a refused stage has no run to describe — and says that rather than crashing."""
    assert render(capsys, tmp_path, "pooled_vs_split", []) == (
        f"No measurements in the stage_*.json files under {tmp_path}.\n"
    )
