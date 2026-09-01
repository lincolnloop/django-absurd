import pytest

from benchmarks import measurement, runner


@pytest.mark.django_db(transaction=True)
def test_saturation_measurement_drains_backlog_and_reports_sql_metrics() -> None:
    spec = measurement.MeasurementSpec(
        name="smoke",
        mode="saturation",
        task_path="benchmarks.tasks.noop_sync",
        tasks=50,
        workers=1,
        worker=runner.WorkerSpec(concurrency=2, poll_interval=0.05),
        # Two reps so the unflagged assertion below means what it says: a single rep
        # has no spread to report and is flagged for that alone.
        reps=2,
        timeout_s=60,
    )

    result = measurement.run_measurement(spec)

    assert result["median"]["n_runs"] == 50
    assert result["median"]["extra_runs"] == 0
    assert (result["median"]["throughput_per_s"] > 0) is True
    assert sum(result["median"]["fairness"].values()) == 50
    assert result["host"]["cpu_count"] >= 1
    assert result["flagged"] is False


@pytest.mark.django_db(transaction=True)
def test_rate_measurement_reports_latency_percentiles() -> None:
    spec = measurement.MeasurementSpec(
        name="smoke-rate",
        mode="rate",
        task_path="benchmarks.tasks.noop_sync",
        rate_per_s=20,
        duration_s=3,
        workers=1,
        worker=runner.WorkerSpec(concurrency=2, poll_interval=0.05),
        reps=1,
        timeout_s=60,
    )

    result = measurement.run_measurement(spec)

    assert (result["median"]["end_to_end_p50_s"] > 0) is True
    assert result["median"]["offered_ok"] is True
    assert result["median"]["missed_deadline_count"] == 0


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
def test_rate_measurement_fairness_agrees_with_its_windowed_run_count() -> None:
    spec = measurement.MeasurementSpec(
        name="smoke-fairness",
        mode="rate",
        task_path="benchmarks.tasks.noop_sync",
        rate_per_s=20,
        duration_s=3,
        workers=1,
        worker=runner.WorkerSpec(concurrency=2, poll_interval=0.05),
        reps=1,
        timeout_s=60,
    )

    result = measurement.run_measurement(spec)

    assert sum(result["median"]["fairness"].values()) == result["median"]["n_runs"]
    assert (result["median"]["n_runs"] < result["median"]["offered"]) is True


@pytest.mark.django_db(transaction=True)
def test_rate_measurement_offers_the_task_kwargs_it_was_given() -> None:
    spec = measurement.MeasurementSpec(
        name="smoke-kwargs",
        mode="rate",
        task_path="benchmarks.tasks.sleep_sync",
        task_kwargs={"seconds": 0.2},
        rate_per_s=10,
        duration_s=2,
        workers=1,
        worker=runner.WorkerSpec(concurrency=4, poll_interval=0.05),
        reps=1,
        timeout_s=60,
    )

    result = measurement.run_measurement(spec)

    assert (result["median"]["execution_p50_s"] > 0.15) is True


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
