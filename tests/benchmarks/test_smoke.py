import pathlib

import pytest

from benchmarks import measurement, runner, stages
from tests.benchmarks import utils

# Above the degenerate-window floor: below about fifty tasks the trimmed completion
# window collapses and every measurement reports zero throughput.
MEASURABLE_TASKS = "60"


@pytest.mark.django_db(transaction=True)
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


@pytest.mark.django_db(transaction=True)
def test_reports_the_latency_of_the_paced_offer_a_rate_stage_made(
    tmp_path: pathlib.Path,
) -> None:
    """A rate stage's numbers come from the trimmed middle of its offer window.

    So the run count is smaller than the offer by construction, and the fairness
    shares — grouped over those same rows — have to add back up to it. Percentiles are
    asserted as measured at all, never as fast: a rate is not something a test can
    demand.
    """
    size = ["--reps", "1", "--tasks", MEASURABLE_TASKS, "--duration", "1"]
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


# The two below build their own measurement because no stage can ask for one. Both
# want a task that misbehaves — one that outlives its claim lease, one that never
# completes — and every workload the driver offers is a task that succeeds.
@pytest.mark.django_db(transaction=True)
def test_saturation_measurement_flags_a_task_that_outlived_its_claim_lease() -> None:
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
    assert result["flagged"] is True


@pytest.mark.django_db(transaction=True)
def test_saturation_measurement_flags_tasks_that_never_completed() -> None:
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
    assert result["flagged"] is True
