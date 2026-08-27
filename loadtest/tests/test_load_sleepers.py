import json
import pathlib
import typing as t

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from loadtest import models

pytestmark = pytest.mark.django_db(transaction=True)

TINY = {
    "sleepers": 4,
    "quick": 5,
    "concurrency": 2,
    "sleep_seconds": 600.0,
    "timeout": 120.0,
}


def run_sleepers(**options: object) -> dict[str, t.Any]:
    path = pathlib.Path(call_command("load_sleepers", **TINY | options))
    payload: dict[str, t.Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def test_sleepers_measures_a_control_arm_and_a_sleepers_arm() -> None:
    payload = run_sleepers(workload="sync")

    assert [e["arm"] for e in payload["entries"]] == ["control", "sleepers"]
    assert [e["sleepers"] for e in payload["entries"]] == [0, TINY["sleepers"]]


def test_sleepers_runs_both_workloads_when_asked() -> None:
    payload = run_sleepers(workload="both")

    assert [(e["workload"], e["arm"]) for e in payload["entries"]] == [
        ("sync", "control"),
        ("sync", "sleepers"),
        ("async", "control"),
        ("async", "sleepers"),
    ]


def test_sleepers_drains_every_quick_task_in_both_arms() -> None:
    payload = run_sleepers(workload="sync")

    assert [e["distinct_tasks"] for e in payload["entries"]] == [TINY["quick"]] * 2
    assert (
        models.ExecutionLog.objects.values("task_id").distinct().count()
        == (TINY["quick"])
    )


def test_sleepers_reports_a_positive_drain_timing() -> None:
    payload = run_sleepers(workload="sync")

    for entry in payload["entries"]:
        assert entry["elapsed_s"] > 0
        assert entry["tasks_per_sec"] > 0


def test_sleepers_samples_the_sleeper_run_states_while_the_quick_tasks_drain() -> None:
    payload = run_sleepers(workload="sync")

    entry = payload["entries"][1]
    assert entry["state_samples"] >= 1
    assert entry["sleeping_min"] + entry["running_max"] <= TINY["sleepers"]


def test_sleepers_compares_the_two_arms_as_a_ratio() -> None:
    payload = run_sleepers(workload="sync")

    comparison = payload["comparisons"][0]
    assert comparison["workload"] == "sync"
    assert comparison["ratio"] == pytest.approx(
        round(comparison["sleepers_s"] / comparison["control_s"], 2)
    )


def test_sleepers_refuses_the_queue_carrying_the_seeded_dataset() -> None:
    with pytest.raises(CommandError, match="seeded"):
        run_sleepers(workload="sync", queue="bulk")


def test_sleepers_repeats_each_arm_and_reports_the_median() -> None:
    payload = run_sleepers(queue="alpha", quick=6, sleepers=2, concurrency=2, repeat=2)

    entry = payload["entries"][0]
    assert len(entry["runs"]) == 2
    assert entry["spread"]["elapsed_s"]["min"] <= entry["elapsed_s"]
