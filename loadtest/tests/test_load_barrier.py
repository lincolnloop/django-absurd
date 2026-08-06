import json
import pathlib
import typing as t

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from loadtest import models

pytestmark = pytest.mark.django_db(transaction=True)

TINY = {
    "fast": 6,
    "slow": 2,
    "fast_seconds": 0.01,
    "slow_seconds": 0.2,
    "concurrency": 2,
    "timeout": 120.0,
}
TINY_TASKS = 8


def run_barrier(**options: object) -> dict[str, t.Any]:
    path = pathlib.Path(call_command("load_barrier", **TINY | options))
    payload: dict[str, t.Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def test_barrier_measures_both_topologies_on_both_distributions() -> None:
    payload = run_barrier(workload="async")

    assert [
        (e["distribution"], e["arm"], e["topology"]) for e in payload["entries"]
    ] == [
        ("uniform", "pooled", "1x2"),
        ("uniform", "split", "2x1"),
        ("mixed", "pooled", "1x2"),
        ("mixed", "split", "2x1"),
    ]


def test_barrier_runs_both_workloads_when_asked() -> None:
    payload = run_barrier(workload="both")

    assert [e["workload"] for e in payload["entries"]] == ["sync"] * 4 + ["async"] * 4


def test_barrier_gives_every_arm_the_same_total_slots() -> None:
    payload = run_barrier(workload="async")

    assert {e["slots"] for e in payload["entries"]} == {TINY["concurrency"]}
    assert [(e["workers"], e["concurrency"]) for e in payload["entries"]] == [
        (1, 2),
        (2, 1),
    ] * 2


def test_barrier_gives_the_uniform_control_the_mixed_backlogs_total_work() -> None:
    payload = run_barrier(workload="async")

    assert {e["tasks"] for e in payload["entries"]} == {TINY_TASKS}
    assert {round(e["work_s"], 6) for e in payload["entries"]} == {
        round(6 * 0.01 + 2 * 0.2, 6)
    }


def test_barrier_executes_the_whole_backlog_in_every_arm() -> None:
    payload = run_barrier(workload="async")

    assert [e["distinct_tasks"] for e in payload["entries"]] == [TINY_TASKS] * 4
    assert models.OccupancyLog.objects.count() >= TINY_TASKS


def test_barrier_reconstructs_an_occupancy_timeline_from_the_slot_intervals() -> None:
    payload = run_barrier(workload="async")

    for entry in payload["entries"]:
        assert entry["span_s"] > 0
        assert entry["tasks_per_sec"] > 0
        assert 0 < entry["mean_busy"] <= entry["slots"]
        assert 1 <= entry["max_busy"] <= entry["slots"]
        assert 0 < entry["utilization"] <= 1


def test_barrier_charges_idle_slots_only_while_the_backlog_is_nonempty() -> None:
    payload = run_barrier(workload="async")

    for entry in payload["entries"]:
        assert 0 <= entry["idle_slot_s"] <= entry["slots"] * entry["span_s"]
        # Idle slots are charged only against work still waiting, so what is busy and
        # what is wastefully idle can never together exceed the slots on offer.
        assert entry["utilization"] + entry["idle_share"] <= 1 + 1e-3


def test_barrier_compares_the_two_topologies_as_a_ratio() -> None:
    payload = run_barrier(workload="async")

    assert [c["distribution"] for c in payload["comparisons"]] == ["uniform", "mixed"]
    for comparison in payload["comparisons"]:
        assert comparison["ratio"] == pytest.approx(
            round(comparison["pooled_s"] / comparison["split_s"], 2)
        )


def test_barrier_refuses_the_queue_carrying_the_seeded_dataset() -> None:
    with pytest.raises(CommandError, match="seeded"):
        run_barrier(workload="async", queue="bulk")


def test_barrier_leaves_the_batch_size_to_the_worker_by_default() -> None:
    payload = run_barrier(workload="async")

    assert [e["batch_size"] for e in payload["entries"]] == [None] * 4


def test_barrier_drives_the_workers_with_an_explicit_batch_size() -> None:
    """A batch size the worker rejected would kill the child and fail the arm.

    So this is pass-through evidence and not merely bookkeeping: the value reaches a
    real ``absurd_worker`` in a spawned process, and every arm still drains.
    """
    payload = run_barrier(workload="async", batch_size=100)

    assert [e["batch_size"] for e in payload["entries"]] == [100] * 4
    assert [e["distinct_tasks"] for e in payload["entries"]] == [TINY_TASKS] * 4


def test_barrier_refuses_a_batch_size_below_one() -> None:
    with pytest.raises(CommandError, match="batch-size"):
        run_barrier(workload="async", batch_size=0)
