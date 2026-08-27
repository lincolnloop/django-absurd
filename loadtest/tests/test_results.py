import json
import pathlib
import typing as t

import pytest

from loadtest import results

pytestmark = pytest.mark.django_db(transaction=True)


def measure_from(values: list[float]) -> t.Callable[[], dict[str, t.Any]]:
    """A measurement that yields ``values`` in order, one per call."""
    remaining = list(values)

    def measure() -> dict[str, t.Any]:
        return {"cell": "1x1", "workload": "sync", "elapsed_s": remaining.pop(0)}

    return measure


def test_repeating_a_measurement_reports_the_median_run() -> None:
    entry = results.summarize_repeats(
        measure_from([3.0, 1.0, 2.0]), repeat=3, rank_by="elapsed_s"
    )

    assert entry["elapsed_s"] == 2.0


def test_repeating_a_measurement_keeps_the_fields_that_do_not_vary() -> None:
    entry = results.summarize_repeats(
        measure_from([3.0, 1.0, 2.0]), repeat=3, rank_by="elapsed_s"
    )

    assert entry["cell"] == "1x1"
    assert entry["workload"] == "sync"


def test_repeating_a_measurement_records_every_run_it_took() -> None:
    entry = results.summarize_repeats(
        measure_from([3.0, 1.0, 2.0]), repeat=3, rank_by="elapsed_s"
    )

    assert [run["elapsed_s"] for run in entry["runs"]] == [3.0, 1.0, 2.0]


def test_repeating_a_measurement_reports_the_spread_of_each_metric() -> None:
    entry = results.summarize_repeats(
        measure_from([3.0, 1.0, 2.0]), repeat=3, rank_by="elapsed_s"
    )

    spread = entry["spread"]["elapsed_s"]
    assert spread["min"] == 1.0
    assert spread["max"] == 3.0
    assert spread["cv"] == pytest.approx(50.0, abs=0.1)


def test_a_single_run_reports_no_spread_and_still_carries_its_run() -> None:
    entry = results.summarize_repeats(
        measure_from([2.5]), repeat=1, rank_by="elapsed_s"
    )

    assert entry["elapsed_s"] == 2.5
    assert entry["spread"]["elapsed_s"]["cv"] == 0.0
    assert len(entry["runs"]) == 1


def test_repeating_refuses_a_repeat_count_below_one() -> None:
    with pytest.raises(ValueError, match="repeat must be at least 1"):
        results.summarize_repeats(measure_from([1.0]), repeat=0, rank_by="elapsed_s")


def test_a_written_run_stamps_the_environment_it_was_measured_on() -> None:
    path = pathlib.Path(results.write_run("probe", {"entries": []}))
    payload: dict[str, t.Any] = json.loads(path.read_text(encoding="utf-8"))

    environment = payload["environment"]
    assert environment["python"].startswith("3.")
    assert environment["django"]
    assert environment["absurd_sdk"]
    assert environment["machine"]
    assert environment["postgres"].startswith("PostgreSQL")
    assert environment["shared_buffers"]
    assert "git_sha" in environment
