import json
import pathlib
import typing as t

import pytest
from django.core.management import call_command

from loadtest import models

pytestmark = pytest.mark.django_db(transaction=True)


def run_drain(**options: object) -> dict[str, t.Any]:
    path = pathlib.Path(call_command("load_drain", **options))
    payload: dict[str, t.Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def test_drain_reports_one_entry_per_requested_cell() -> None:
    payload = run_drain(tasks=20, cell=["1x1", "1x2"], workload="sync")

    assert [e["cell"] for e in payload["entries"]] == ["1x1", "1x2"]
    assert [e["concurrency"] for e in payload["entries"]] == [1, 2]


def test_drain_executes_every_enqueued_task() -> None:
    run_drain(tasks=20, cell=["1x1"], workload="sync")

    assert models.ExecutionLog.objects.values("task_id").distinct().count() == 20


def test_drain_reports_a_positive_throughput() -> None:
    payload = run_drain(tasks=20, cell=["1x1"], workload="sync")

    entry = payload["entries"][0]
    assert entry["elapsed_s"] > 0
    assert entry["tasks_per_sec"] > 0


def test_drain_derives_duplicates_from_executions_minus_distinct_tasks() -> None:
    payload = run_drain(tasks=20, cell=["1x1"], workload="sync")

    entry = payload["entries"][0]
    assert entry["distinct_tasks"] == 20
    assert entry["duplicates"] == entry["executions"] - entry["distinct_tasks"]


def test_drain_runs_both_workloads_when_asked() -> None:
    payload = run_drain(tasks=10, cell=["1x1"], workload="both")

    assert [e["workload"] for e in payload["entries"]] == ["sync", "async"]


def test_drain_repeats_each_cell_and_reports_the_median() -> None:
    payload = run_drain(tasks=20, cell=["1x1"], workload="sync", repeat=3)

    entry = payload["entries"][0]
    assert len(entry["runs"]) == 3
    assert entry["spread"]["tasks_per_sec"]["min"] <= entry["tasks_per_sec"]
    assert entry["tasks_per_sec"] <= entry["spread"]["tasks_per_sec"]["max"]


def test_drain_measures_each_cell_once_by_default() -> None:
    payload = run_drain(tasks=20, cell=["1x1"], workload="sync", repeat=1)

    assert len(payload["entries"][0]["runs"]) == 1
