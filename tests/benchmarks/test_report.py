import json
import typing as t
from pathlib import Path

import pytest

from benchmarks import report

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

HEADER = (
    "# django-absurd benchmark results\n"
    "\n"
    "- git sha: `deadbeef`\n"
    "- captured at: 2026-08-27T12:00:00+00:00\n"
    "- cpu count: 8, load average (1m): 0.50\n"
    "- python 3.14.3, Django 6.1, absurd-sdk 0.5.0\n"
    "- postgres: PostgreSQL 18.0 on x86_64-pc-linux-gnu\n"
)

MEASUREMENT_TABLE_HEAD = (
    "| measurement | mode | workers | concurrency | batch | poll | tasks/s "
    "| e2e p50 s | e2e p90 s | e2e p99 s | spread | notes |\n"
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
)


def build_measurement(
    name: str,
    spec: dict[str, t.Any],
    median: dict[str, t.Any],
    flagged: bool = False,
    spread: float | None = 0.04,
    host: dict[str, t.Any] | None = None,
) -> dict[str, t.Any]:
    return {
        "spec": {
            "name": name,
            "mode": "saturation",
            "workers": 1,
            "task_path": "benchmarks.tasks.noop_sync",
            "worker": {"concurrency": 1, "batch_size": None, "poll_interval": 0.25},
            **spec,
        },
        "median": {
            "throughput_per_s": 100.0,
            "end_to_end_p50_s": 0.012,
            "end_to_end_p90_s": 0.03,
            "end_to_end_p99_s": 0.05,
            **median,
        },
        "spread": spread,
        "flagged": flagged,
        "host": host or HOST,
    }


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
    (tmp_path / f"stage_{stage}.json").write_text(
        json.dumps({"stage": stage, "measurements": entries, **extra})
    )
    report.main(["--results-dir", str(tmp_path)])
    return capsys.readouterr().out


def test_renders_stage_tables_from_result_files(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    entries = [
        build_measurement("a1_c1", {}, {"throughput_per_s": 412.5}),
        build_measurement(
            "a1_c2",
            {"worker": {"concurrency": 2, "batch_size": 4, "poll_interval": 0.25}},
            {"throughput_per_s": 800.0},
            flagged=True,
            spread=0.22,
        ),
    ]

    assert render(capsys, tmp_path, "a", entries) == (
        HEADER + "\n"
        "## Stage A\n"
        "\n" + MEASUREMENT_TABLE_HEAD + "| a1_c1 | saturation | 1 | 1 | default | 0.25 "
        "| 412.5 | 0.0120 | 0.0300 | 0.0500 | 4.0% |  |\n"
        "| a1_c2 | saturation | 1 | 2 | 4 | 0.25 "
        "| 800.0 | 0.0120 | 0.0300 | 0.0500 | 22.0% | ⚠ flagged |\n"
        "\n"
        "Throughput relative to `a1_c1` (flagged measurements excluded):\n"
        "\n"
        "- `a1_c1`: 1.00x\n"
    )


def test_renders_an_unmeasurable_spread_as_unavailable(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    entries = [
        build_measurement(
            "a1_c1", {}, {"throughput_per_s": 0.0}, flagged=True, spread=None
        )
    ]

    assert render(capsys, tmp_path, "a", entries) == (
        HEADER + "\n"
        "## Stage A\n"
        "\n" + MEASUREMENT_TABLE_HEAD + "| a1_c1 | saturation | 1 | 1 | default | 0.25 "
        "| 0.0 | 0.0120 | 0.0300 | 0.0500 | n/a | ⚠ flagged |\n"
        "\n"
        "No unflagged measurements; nothing derived.\n"
    )


def test_names_the_moved_baseline_when_the_first_measurement_is_flagged(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    entries = [
        build_measurement("a1_c1", {}, {"throughput_per_s": 412.5}, flagged=True),
        build_measurement("a1_c2", {}, {"throughput_per_s": 800.0}),
    ]

    assert (
        "Throughput relative to `a1_c2` (flagged measurements excluded; the "
        "stage's first measurement `a1_c1` is flagged, so the baseline moved):\n"
        "\n"
        "- `a1_c2`: 1.00x\n"
    ) in render(capsys, tmp_path, "a", entries)


def test_reports_mixed_provenance_when_measurements_disagree(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    entries = [
        build_measurement("a1_c1", {}, {}, host={**HOST, "git_sha": "bbb222"}),
        build_measurement(
            "a1_c2",
            {},
            {},
            host={
                **HOST,
                "git_sha": "aaa111",
                "captured_at": "2026-08-27T15:00:00+00:00",
            },
        ),
    ]

    rendered = render(capsys, tmp_path, "a", entries)

    assert "- git sha: mixed (`aaa111`, `bbb222`)\n" in rendered
    assert (
        "- captured at: 2026-08-27T12:00:00+00:00 .. 2026-08-27T15:00:00+00:00\n"
    ) in rendered


def test_renders_scaling_efficiency_for_stage_b(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    entries = [
        build_measurement("b_workers_1", {"workers": 1}, {"throughput_per_s": 100.0}),
        build_measurement("b_workers_2", {"workers": 2}, {"throughput_per_s": 180.0}),
    ]

    assert (
        "Scaling efficiency `T(N) / (N x T(1))` (flagged measurements excluded):\n"
        "\n"
        "- 1 worker(s): 1.00\n"
        "- 2 worker(s): 0.90\n"
    ) in render(capsys, tmp_path, "b", entries)


def test_renders_async_over_sync_ratio_for_stage_d(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    entries = [
        build_measurement(
            "d_async_c4",
            {
                "task_path": "benchmarks.tasks.sleep_async",
                "worker": {
                    "concurrency": 4,
                    "batch_size": None,
                    "poll_interval": 0.25,
                },
            },
            {"throughput_per_s": 200.0},
        ),
        build_measurement(
            "d_sync_c4",
            {
                "task_path": "benchmarks.tasks.sleep_sync",
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
        "Async / sync throughput ratio at the same IO wait "
        "(flagged measurements excluded):\n"
        "\n"
        "- concurrency 4: 2.00x\n"
    ) in render(capsys, tmp_path, "d", entries)


def test_renders_checkpoint_multiplier_for_stage_e(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    entries = [
        build_measurement("e_flat", {}, {"throughput_per_s": 400.0}),
        build_measurement(
            "e_workflow",
            {"task_path": "benchmarks.tasks.run_steps"},
            {"throughput_per_s": 100.0},
        ),
    ]

    assert (
        "Checkpoint cost (flagged measurements excluded):\n"
        "\n"
        "- one `run_steps` task costs 4.00x a flat no-op task\n"
    ) in render(capsys, tmp_path, "e", entries)


def test_renders_producer_columns_for_stage_f(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    entries = [
        build_measurement(
            "f_single",
            {"mode": "producer"},
            {
                "count": 5000,
                "enqueues_per_s": 250.0,
                "enqueue_p50_s": 0.004,
                "enqueue_p99_s": 0.009,
            },
            spread=0.03,
        )
    ]

    assert render(capsys, tmp_path, "f", entries) == (
        HEADER + "\n"
        "## Stage F\n"
        "\n"
        "| mode | enqueues | enqueues/s | enqueue p50 s | enqueue p99 s "
        "| spread | notes |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| f_single | 5000 | 250.0 | 0.00400 | 0.00900 | 3.0% |  |\n"
    )


def test_renders_idle_polling_tax_and_latency_ratios_for_stage_c(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    entries = [
        build_measurement("c_poll_0.25", {"mode": "rate"}, {"end_to_end_p50_s": 0.02}),
        build_measurement(
            "c_poll_1",
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

    rendered = render(capsys, tmp_path, "c", entries, idle_probes=probes)

    assert (
        "Idle polling tax (workers parked on an empty queue):\n"
        "\n"
        "| poll interval s | workers | claims/s/worker | 1/poll_interval |\n"
        "| --- | --- | --- | --- |\n"
        "| 0.05 | 4 | 19.80 | 20.00 |\n"
        "| 1 | 4 | 0.99 | 1.00 |\n"
    ) in rendered
    assert (
        "End-to-end p50 relative to `c_poll_0.25` (flagged measurements excluded):\n"
        "\n"
        "- `c_poll_0.25`: 1.00x\n"
        "- `c_poll_1`: 3.00x\n"
    ) in rendered
