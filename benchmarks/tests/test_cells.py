import typing as t

import pytest

from benchmarks import cells, runner


def build_spec(
    mode: t.Literal["rate", "saturation"], spread_limit: float
) -> cells.CellSpec:
    return cells.CellSpec(
        name="unit",
        mode=mode,
        task_path="benchmarks.tasks.noop_sync",
        worker=runner.WorkerSpec(),
        spread_limit=spread_limit,
    )


@pytest.mark.functional
@pytest.mark.django_db
@pytest.mark.parametrize(
    ("defect_key", "defect_value"),
    [
        ("degenerate_window", True),
        ("extra_runs", 1),
        ("missing_tasks", 1),
        ("offered_ok", False),
    ],
)
def test_flags_a_cell_when_any_non_median_rep_is_defective(
    defect_key: str, defect_value: t.Any
) -> None:
    # The defect sits on the rep that sorts FURTHEST from the median, which is the
    # whole point: a median-only check cannot see it.
    reps: list[dict[str, t.Any]] = [
        {"valid": True, "end_to_end_p50_s": 0.10, defect_key: defect_value},
        {"valid": True, "end_to_end_p50_s": 0.11},
        {"valid": True, "end_to_end_p50_s": 0.12},
    ]

    summary = cells.summarize_reps(build_spec("rate", 0.5), reps)

    assert summary["median"]["end_to_end_p50_s"] == 0.11
    assert defect_key not in summary["median"]
    assert summary["flagged"] is True


@pytest.mark.functional
@pytest.mark.django_db
def test_takes_the_lower_of_two_middle_reps() -> None:
    reps: list[dict[str, t.Any]] = [
        {"valid": True, "throughput_per_s": 10.0},
        {"valid": True, "throughput_per_s": 20.0},
    ]

    summary = cells.summarize_reps(build_spec("saturation", 5.0), reps)

    assert summary["median"]["throughput_per_s"] == 10.0


@pytest.mark.functional
@pytest.mark.django_db
def test_refuses_to_read_a_zero_throughput_cell_as_stable() -> None:
    reps: list[dict[str, t.Any]] = [
        {"valid": True, "throughput_per_s": 0.0, "degenerate_window": True}
    ]

    summary = cells.summarize_reps(build_spec("saturation", 0.15), reps)

    assert summary["spread"] is None
    assert summary["flagged"] is True
