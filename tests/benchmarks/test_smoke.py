import pathlib

import pytest

import analysis
import measurement
import runner
import stages
from tests.benchmarks import utils

# Every test here runs real `absurd_worker` children: separate processes on their own
# connections, which cannot see rows the test has not committed.
pytestmark = pytest.mark.django_db(transaction=True)

# Above the degenerate-window floor: below about fifty tasks the trimmed completion
# window collapses and every measurement reports zero throughput.
MEASURABLE_TASKS = "60"
# Three full profile slices and a deliberate leftover, derived from the slice size so
# it stays on that boundary if the size ever moves.
PROFILED_TASKS = 3 * analysis.THROUGHPUT_SLICE_COMPLETIONS + 100


def test_reports_the_sql_metrics_of_the_backlog_a_stage_drained(
    tmp_path: pathlib.Path,
) -> None:
    """Every number here is read back off Absurd's own columns after the drain.

    `--io-seconds` is what reaches the task itself, so a body that ran for longer than
    the harness asked for would show up as an execution percentile below the ask.
    """
    stages.main(
        [
            "sync_vs_async",
            "--reps",
            "1",
            "--tasks",
            MEASURABLE_TASKS,
            "--io-seconds",
            "0.2",
            "--results-dir",
            str(tmp_path),
        ]
    )

    result = utils.read_stage(tmp_path, "sync_vs_async")
    assert [
        {
            "name": entry["spec"]["name"],
            "n_runs": entry["median"]["n_runs"],
            "extra_runs": entry["median"]["extra_runs"],
            "missing_tasks": entry["median"]["missing_tasks"],
            "fairness": entry["median"]["fairness"],
            "slept_the_io_it_was_offered": (entry["median"]["execution_p50_s"] > 0.15),
        }
        for entry in result["measurements"]
    ] == [
        {
            "name": name,
            "n_runs": 60,
            "extra_runs": 0,
            "missing_tasks": 0,
            "fairness": {"bench-0": 60},
            "slept_the_io_it_was_offered": True,
        }
        for name in (
            "async_c4",
            "sync_c4",
            "async_c16",
            "sync_c16",
            "async_c32",
            "sync_c32",
        )
    ]


def test_reports_the_latency_of_the_paced_offer_a_rate_stage_made(
    tmp_path: pathlib.Path,
) -> None:
    """A rate stage's numbers come from the trimmed middle of its offer window.

    So the run count is smaller than the offer by construction, and the fairness
    shares — grouped over those same rows — have to add back up to it. Percentiles are
    asserted as measured at all, never as fast: a rate is not something a test can
    demand.
    """
    size = [
        "--reps",
        "1",
        "--tasks",
        MEASURABLE_TASKS,
        "--duration",
        "1",
        # Nothing here reads the idle probes, which are what the default fleet of four
        # is spent on; the paced offer under test runs on one worker either way.
        "--max-workers",
        "1",
    ]
    stages.main(["worker_knobs", *size, "--results-dir", str(tmp_path)])

    stages.main(["poll_interval", *size, "--results-dir", str(tmp_path)])

    result = utils.read_stage(tmp_path, "poll_interval")
    assert [
        {
            "name": entry["spec"]["name"],
            "offered": entry["median"]["offered"],
            "measured_a_latency": entry["median"]["end_to_end_p50_s"] > 0,
            "trimmed_the_offer": (
                entry["median"]["n_runs"] < entry["median"]["offered"]
            ),
            "fairness_adds_up_to_the_runs": (
                sum(entry["median"]["fairness"].values()) == entry["median"]["n_runs"]
            ),
        }
        for entry in result["measurements"]
    ] == [
        {
            "name": name,
            "offered": 5,
            "measured_a_latency": True,
            "trimmed_the_offer": True,
            "fairness_adds_up_to_the_runs": True,
        }
        for name in ("poll_0.05", "poll_0.25", "poll_1")
    ]


# Everything below builds its own measurement rather than driving a stage. The first
# three want a task that misbehaves — one that outlives its claim lease, one that never
# completes, one that never drains — and every workload the driver offers is a task
# that succeeds; the last four want one measurement at a size of their own — repeated,
# napped, too small to slice, or big enough to slice — which the smallest stage would
# charge six of.
def test_saturation_measurement_invalidates_a_task_that_outlived_its_claim_lease() -> (
    None
):
    spec = measurement.MeasurementSpec(
        name="smoke-redelivery",
        mode="saturation",
        task_path="tests.benchmarks.utils.sleep_past_claim_lease",
        tasks=1,
        workers=1,
        worker=runner.WorkerSpec(concurrency=2, poll_interval=0.05, claim_timeout=1),
        reps=1,
        timeout_s=60,
    )

    result = measurement.run_measurement(spec)

    assert result["median"]["extra_runs"] == 1
    assert result["median"]["max_attempt"] == 2
    assert result["invalid"] is True


def test_saturation_measurement_invalidates_tasks_that_never_completed() -> None:
    spec = measurement.MeasurementSpec(
        name="smoke-missing",
        mode="saturation",
        task_path="tests.benchmarks.utils.fail_on_its_only_attempt",
        tasks=1,
        workers=1,
        worker=runner.WorkerSpec(concurrency=2, poll_interval=0.05),
        reps=1,
        timeout_s=60,
    )

    result = measurement.run_measurement(spec)

    assert result["median"]["missing_tasks"] == 1
    assert result["median"]["extra_runs"] == 0
    assert result["invalid"] is True


def test_saturation_measurement_refuses_a_backlog_that_never_drained() -> None:
    """A backlog still moving when the clock runs out is refused, not recorded.

    Its metrics would be read off whatever had finished by then, which is a smaller
    measurement wearing the size it was asked for.
    """
    spec = measurement.MeasurementSpec(
        name="smoke-stalled",
        mode="saturation",
        task_path="tests.benchmarks.utils.sleep_past_claim_lease",
        tasks=1,
        workers=1,
        worker=runner.WorkerSpec(concurrency=1, poll_interval=0.05),
        reps=1,
        timeout_s=1.0,
    )

    with pytest.raises(measurement.MeasurementTimeoutError) as error_info:
        measurement.run_measurement(spec)

    assert str(error_info.value) == (
        "Measurement 'smoke-stalled' still had unfinished tasks after 1s. A "
        "measurement that never drains is refused rather than recorded: raise "
        "timeout_s, cut the task count, or find out why the workers stalled."
    )


def test_saturation_measurement_refuses_a_rep_the_host_slept_through() -> None:
    """A napped rep leaves nothing behind: no median, no dispersion, no endpoints.

    The drain phase is wall time, so a host that suspends mid-drain would publish a
    throughput measured over a window it was unconscious for. It is invalid rather
    than unstable: nothing disagreed, there was simply nothing left to compare.
    """
    spec = measurement.MeasurementSpec(
        name="smoke-napped",
        mode="saturation",
        task_path="tasks.noop_sync",
        tasks=int(MEASURABLE_TASKS),
        workers=1,
        worker=runner.WorkerSpec(concurrency=1, poll_interval=0.05),
        reps=1,
        timeout_s=60,
    )

    with utils.nap_the_wall_clock():
        result = measurement.run_measurement(spec)

    assert {
        "reps": [utils.normalize_measured_durations(rep) for rep in result["reps"]],
        "median": result["median"],
        "spread": result["spread"],
        "absolute_spread": result["absolute_spread"],
        "cv": result["cv"],
        "range_low": result["range_low"],
        "range_high": result["range_high"],
        "invalid": result["invalid"],
        "unstable": result["unstable"],
    } == {
        "reps": [
            {
                "valid": False,
                "error": (
                    "Wall clock advanced Ns over a phase the monotonic clock measured "
                    "at Ns: the host suspended or stalled mid-phase, so every number "
                    "this phase produced is fiction. Re-run the measurement on a "
                    "machine that stays awake."
                ),
            }
        ],
        "median": {},
        "spread": None,
        "absolute_spread": None,
        "cv": None,
        "range_low": None,
        "range_high": None,
        "invalid": True,
        "unstable": False,
    }


def test_saturation_measurement_records_every_dispersion_of_its_reps() -> None:
    """All four are recorded once there are reps to compare, never some of them.

    They say different things about the same reps: the CV is what instability is
    thresholded on, the absolute spread is the floor that keeps a tiny difference from
    tripping it, and the endpoints are what a report prints instead of a percentage.
    Asserted as measured at all, never as small: stability is not something a test can
    demand of a real worker.
    """
    spec = measurement.MeasurementSpec(
        name="smoke-repeated",
        mode="saturation",
        task_path="tasks.noop_sync",
        tasks=int(MEASURABLE_TASKS),
        workers=1,
        worker=runner.WorkerSpec(concurrency=1, poll_interval=0.05),
        reps=2,
        timeout_s=60,
    )

    result = measurement.run_measurement(spec)

    assert {
        "reps": len(result["reps"]),
        "measured_a_spread": isinstance(result["spread"], float),
        "measured_an_absolute_spread": isinstance(result["absolute_spread"], float),
        "measured_a_cv": isinstance(result["cv"], float),
        "endpoints_bracket_the_median": (
            result["range_low"]
            <= result["median"]["throughput_per_s"]
            <= result["range_high"]
        ),
    } == {
        "reps": 2,
        "measured_a_spread": True,
        "measured_an_absolute_spread": True,
        "measured_a_cv": True,
        "endpoints_bracket_the_median": True,
    }


def test_saturation_rep_profiles_its_throughput_across_the_drain() -> None:
    """A rep drains a full queue to empty, so one number averages a moving quantity.

    The profile is what separates a cost that rises with queue depth from one that
    differs rep to rep: slice 0 drained the fullest queue and the last slice the
    emptiest. Sized at three full slices plus a leftover, so the partial slice the
    drain ends on has to be dropped rather than divided — its 100 completions over
    the same instants would read as a rate of their own.

    Asserted as measured and ordered, never as fast or as flat: a real worker's shape
    is the finding, not something a test can demand.
    """
    spec = measurement.MeasurementSpec(
        name="smoke-profile",
        mode="saturation",
        task_path="tasks.noop_sync",
        tasks=PROFILED_TASKS,
        workers=1,
        worker=runner.WorkerSpec(concurrency=8, poll_interval=0.05),
        reps=1,
        timeout_s=90,
    )

    rep = measurement.run_measurement(spec)["reps"][0]

    assert {
        "n_runs": rep["n_runs"],
        "full_slices": len(rep["profile_slices"]),
        "every_slice_measured_a_rate": all(rate > 0 for rate in rep["profile_slices"]),
        "median_is_the_middle_slice": (
            rep["profile_median_per_s"] == sorted(rep["profile_slices"])[1]
        ),
        "measured_a_cv": rep["profile_cv"] > 0,
    } == {
        "n_runs": PROFILED_TASKS,
        "full_slices": 3,
        "every_slice_measured_a_rate": True,
        "median_is_the_middle_slice": True,
        "measured_a_cv": True,
    }


def test_saturation_rep_too_small_to_slice_records_no_profile() -> None:
    """Two slices are a line whatever the drain did, so no profile is recorded.

    A smoke-sized backlog would otherwise publish a shape read off a handful of
    completions, and nothing downstream could tell it from a measured one.
    """
    spec = measurement.MeasurementSpec(
        name="smoke-unprofilable",
        mode="saturation",
        task_path="tasks.noop_sync",
        tasks=int(MEASURABLE_TASKS),
        workers=1,
        worker=runner.WorkerSpec(concurrency=8, poll_interval=0.05),
        reps=1,
        timeout_s=60,
    )

    rep = measurement.run_measurement(spec)["reps"][0]

    assert {
        "n_runs": rep["n_runs"],
        "profile_slices": rep["profile_slices"],
        "profile_median_per_s": rep["profile_median_per_s"],
        "profile_cv": rep["profile_cv"],
    } == {
        "n_runs": int(MEASURABLE_TASKS),
        "profile_slices": None,
        "profile_median_per_s": None,
        "profile_cv": None,
    }
