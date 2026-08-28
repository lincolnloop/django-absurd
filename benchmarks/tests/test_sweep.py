import typing as t

import pytest

from benchmarks import cells, runner, sweep

HEALTHY_REP = {
    "valid": True,
    "mode": "single",
    "count": 5000,
    "enqueues_per_s": 100.0,
    "enqueue_p50_s": 0.01,
    "enqueue_p99_s": 0.02,
}


@pytest.mark.functional
@pytest.mark.django_db
def test_flags_a_producer_stage_the_host_slept_through() -> None:
    reps: list[dict[str, t.Any]] = [
        HEALTHY_REP,
        {"valid": False, "error": "wall clock advanced 40.0s"},
    ]

    summary = sweep.summarize_producer_reps("single", reps)

    assert summary["median"]["enqueues_per_s"] == 100.0
    assert summary["flagged"] is True


@pytest.mark.functional
@pytest.mark.django_db
def test_summarizes_a_clean_producer_stage_without_flagging() -> None:
    summary = sweep.summarize_producer_reps("single", [HEALTHY_REP])

    assert summary["spread"] == 0.0
    assert summary["flagged"] is False


@pytest.mark.internal
def test_refuses_to_calibrate_a_later_stage_from_cells_that_measured_nothing() -> None:
    degenerate = [
        {
            "spec": {"name": "a1_c1", "worker": {}},
            "median": {"throughput_per_s": 0.0},
            "flagged": True,
        }
    ]

    with pytest.raises(sweep.UncalibratableStageError):
        sweep.pick_best_cell(degenerate)


@pytest.mark.functional
@pytest.mark.django_db
def test_summarizes_a_cell_whose_reps_were_all_discarded() -> None:
    spec = cells.CellSpec(
        name="napped",
        mode="saturation",
        task_path="benchmarks.tasks.noop_sync",
        worker=runner.WorkerSpec(),
    )

    summary = cells.summarize_reps(spec, [{"valid": False, "error": "host suspended"}])

    assert summary["spread"] is None
    assert sweep.summarize_cell(summary) == (
        "napped: 0.0 tasks/s, e2e p50 0.0ms, spread n/a [FLAGGED]"
    )


@pytest.mark.internal
def test_calibrates_a_rate_stage_from_a_cell_the_producer_can_outrun() -> None:
    recorded = [
        {
            "spec": {"name": "b_workers_4", "workers": 4, "worker": {}},
            "median": {"throughput_per_s": 375.5},
            "flagged": False,
        },
        {
            "spec": {"name": "b_workers_8", "workers": 8, "worker": {}},
            "median": {"throughput_per_s": 657.4},
            "flagged": False,
        },
    ]

    # The fastest cell is the 8-worker one, but a rate stage must offer from the same
    # box, so it calibrates on the fastest cell that leaves the producer some cores.
    assert sweep.pick_best_cell(recorded)["spec"]["name"] == "b_workers_8"
    assert sweep.pick_rate_calibration_cell(recorded)["spec"]["name"] == "b_workers_4"


@pytest.mark.internal
@pytest.mark.parametrize(
    ("cores", "expected"),
    [
        (1, [1]),
        (2, [1, 2]),
        (4, [1, 2, 3, 4]),
        (8, [1, 2, 4, 6, 8]),
        (16, [1, 2, 4, 8, 12, 16]),
        (32, [1, 2, 8, 16, 24, 32]),
    ],
)
def test_sizes_the_worker_ladder_to_the_host(cores: int, expected: list[int]) -> None:
    assert sweep.build_worker_ladder(cores) == expected
