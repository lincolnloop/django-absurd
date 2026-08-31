import typing as t

import pytest

from benchmarks import measurement, runner


def build_spec(
    mode: t.Literal["rate", "saturation"],
    spread_limit: float,
    spread_floor: float = 0.0,
) -> measurement.MeasurementSpec:
    return measurement.MeasurementSpec(
        name="unit",
        mode=mode,
        task_path="benchmarks.tasks.noop_sync",
        worker=runner.WorkerSpec(),
        spread_limit=spread_limit,
        spread_floor=spread_floor,
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
def test_flags_a_measurement_when_any_non_median_rep_is_defective(
    defect_key: str, defect_value: t.Any
) -> None:
    # The defect sits on the rep that sorts FURTHEST from the median, which is the
    # whole point: a median-only check cannot see it.
    reps: list[dict[str, t.Any]] = [
        {"valid": True, "end_to_end_p50_s": 0.10, defect_key: defect_value},
        {"valid": True, "end_to_end_p50_s": 0.11},
        {"valid": True, "end_to_end_p50_s": 0.12},
    ]

    summary = measurement.summarize_reps(build_spec("rate", 0.5), reps)

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

    summary = measurement.summarize_reps(build_spec("saturation", 5.0), reps)

    assert summary["median"]["throughput_per_s"] == 10.0


@pytest.mark.functional
@pytest.mark.django_db
def test_refuses_to_read_a_zero_throughput_measurement_as_stable() -> None:
    reps: list[dict[str, t.Any]] = [
        {"valid": True, "throughput_per_s": 0.0, "degenerate_window": True}
    ]

    summary = measurement.summarize_reps(build_spec("saturation", 0.15), reps)

    assert summary["spread"] is None
    assert summary["flagged"] is True


@pytest.mark.functional
@pytest.mark.django_db
def test_spares_a_relatively_noisy_measurement_whose_reps_are_absolutely_tight() -> (
    None
):
    # 0.067/0.089/0.173s spans 159% of its median but only 106ms end to end. Relative
    # spread RISES as a measurement gets faster, so without the floor the harness
    # flags its own best results.
    reps: list[dict[str, t.Any]] = [
        {"valid": True, "end_to_end_p50_s": 0.067},
        {"valid": True, "end_to_end_p50_s": 0.089},
        {"valid": True, "end_to_end_p50_s": 0.173},
    ]

    summary = measurement.summarize_reps(build_spec("rate", 0.15, 0.15), reps)

    assert summary["spread"] > 0.15
    assert summary["absolute_spread"] == pytest.approx(0.106)
    assert summary["flagged"] is False


@pytest.mark.functional
@pytest.mark.django_db
def test_still_flags_a_measurement_that_clears_the_limit_and_the_floor() -> None:
    reps: list[dict[str, t.Any]] = [
        {"valid": True, "end_to_end_p50_s": 0.680},
        {"valid": True, "end_to_end_p50_s": 0.771},
        {"valid": True, "end_to_end_p50_s": 0.911},
    ]

    summary = measurement.summarize_reps(build_spec("rate", 0.15, 0.15), reps)

    assert summary["absolute_spread"] == pytest.approx(0.231)
    assert summary["flagged"] is True
