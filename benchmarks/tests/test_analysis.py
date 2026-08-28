import typing as t

import pytest

from benchmarks import analysis

LATENCIES = (0.1, 0.2, 0.3, 0.01, 0.02, 0.03, 0.11, 0.22, 0.33)


def build_row(
    n_tasks: int, n_runs: int, p10: float | None, p90: float | None
) -> tuple[t.Any, ...]:
    return (n_tasks, n_runs, p10, p90, *LATENCIES)


@pytest.mark.internal
def test_reports_throughput_over_the_trimmed_completion_window() -> None:
    # total_runs deliberately EXCEEDS the completed count: throughput must be driven
    # by completed runs only, or a redelivery would inflate it.
    metrics = analysis.build_metrics(
        build_row(50, 50, 1000.0, 1002.0), (60, 50, 2), {"bench-0": 50}
    )

    assert metrics["throughput_per_s"] == 20.0
    assert metrics["extra_runs"] == 10
    assert metrics["degenerate_window"] is False


@pytest.mark.internal
def test_refuses_throughput_from_a_degenerate_completion_window() -> None:
    metrics = analysis.build_metrics(
        build_row(50, 50, 1000.0, 1000.0), (50, 50, 1), {"bench-0": 50}
    )

    assert metrics["throughput_per_s"] == 0.0
    assert metrics["degenerate_window"] is True


@pytest.mark.internal
def test_counts_extra_runs_from_every_run_row_not_just_completed_ones() -> None:
    metrics = analysis.build_metrics(build_row(0, 0, None, None), (2, 1, 2), {})

    assert metrics["extra_runs"] == 1
    assert metrics["max_attempt"] == 2
