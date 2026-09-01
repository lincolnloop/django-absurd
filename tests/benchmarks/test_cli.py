import pathlib

import pytest

from benchmarks import stages
from tests.benchmarks import utils

# Above the degenerate-window floor: below about fifty tasks the trimmed completion
# window collapses, every measurement reports zero throughput, and a stage flags for
# that rather than for whatever the test is about.
MEASURABLE_TASKS = "60"


@pytest.mark.django_db(transaction=True)
def test_runs_the_producer_stage_at_the_size_it_was_asked_for(
    tmp_path: pathlib.Path,
) -> None:
    stages.main(
        ["--stage", "f", "--reps", "1", "--tasks", "10", "--results-dir", str(tmp_path)]
    )

    result = utils.read_stage(tmp_path, "f")
    assert [
        {
            "name": entry["spec"]["name"],
            "count": entry["median"]["count"],
            "spread": entry["spread"],
            "flagged": entry["flagged"],
        }
        for entry in result["measurements"]
    ] == [
        {"name": "f_single", "count": 10, "spread": None, "flagged": True},
        {"name": "f_threaded", "count": 10, "spread": None, "flagged": True},
        {"name": "f_atomic", "count": 10, "spread": None, "flagged": True},
    ]


@pytest.mark.django_db(transaction=True)
def test_runs_a_saturation_stage_at_the_size_it_was_asked_for(
    tmp_path: pathlib.Path,
) -> None:
    stages.main(
        ["--stage", "d", "--reps", "1", "--tasks", "8", "--results-dir", str(tmp_path)]
    )

    result = utils.read_stage(tmp_path, "d")
    assert [
        {
            "name": entry["spec"]["name"],
            "tasks": entry["median"]["n_tasks"],
            "spread": entry["spread"],
            "flagged": entry["flagged"],
        }
        for entry in result["measurements"]
    ] == [
        {"name": "d_async_c4", "tasks": 8, "spread": None, "flagged": True},
        {"name": "d_sync_c4", "tasks": 8, "spread": None, "flagged": True},
        {"name": "d_async_c16", "tasks": 8, "spread": None, "flagged": True},
        {"name": "d_sync_c16", "tasks": 8, "spread": None, "flagged": True},
        {"name": "d_async_c32", "tasks": 8, "spread": None, "flagged": True},
        {"name": "d_sync_c32", "tasks": 8, "spread": None, "flagged": True},
    ]


@pytest.mark.django_db(transaction=True)
def test_runs_a_rate_stage_and_its_idle_probes_at_the_duration_it_was_asked_for(
    tmp_path: pathlib.Path,
) -> None:
    """Rate stages are sized in seconds, and the idle probes are rate work too.

    The probes carry their own hardcoded duration that neither size flag reaches, and
    they outlast every measurement they sit beside. How many tasks a paced offer gets
    out in a second is not fixed, so the count is not part of the result here.
    """
    size = ["--reps", "1", "--tasks", MEASURABLE_TASKS, "--duration", "1"]
    stages.main(["--stage", "a", *size, "--results-dir", str(tmp_path)])

    stages.main(["--stage", "c", *size, "--results-dir", str(tmp_path)])

    result = utils.read_stage(tmp_path, "c")
    assert [
        {
            "name": entry["spec"]["name"],
            "duration_s": entry["spec"]["duration_s"],
            "spread": entry["spread"],
            "flagged": entry["flagged"],
        }
        for entry in result["measurements"]
    ] == [
        {"name": "c_poll_0.05", "duration_s": 1.0, "spread": None, "flagged": True},
        {"name": "c_poll_0.25", "duration_s": 1.0, "spread": None, "flagged": True},
        {"name": "c_poll_1", "duration_s": 1.0, "spread": None, "flagged": True},
    ]
    assert [
        {"poll_interval": probe["poll_interval"], "seconds": probe["seconds"]}
        for probe in result["idle_probes"]
    ] == [
        {"poll_interval": 0.05, "seconds": 1.0},
        {"poll_interval": 0.25, "seconds": 1.0},
        {"poll_interval": 1.0, "seconds": 1.0},
    ]


@pytest.mark.django_db(transaction=True)
def test_runs_every_calibrated_stage_from_its_prerequisite(
    tmp_path: pathlib.Path,
) -> None:
    """The whole chain, in dependency order, at the smallest size that measures.

    B, C and E calibrate from A and G calibrates from B, each reading the earlier
    stage back off disk, so a stage that runs alone proves nothing about the chain.
    """
    size = ["--reps", "1", "--tasks", MEASURABLE_TASKS, "--duration", "1"]
    stages.main(["--stage", "a", *size, "--results-dir", str(tmp_path)])

    for stage in ("b", "e", "g"):
        stages.main(["--stage", stage, *size, "--results-dir", str(tmp_path)])

    assert [
        entry["spec"]["name"]
        for entry in utils.read_stage(tmp_path, "e")["measurements"]
    ] == ["e_flat", "e_workflow"]
    assert [
        entry["spec"]["name"]
        for entry in utils.read_stage(tmp_path, "g")["measurements"]
    ] == ["g_rate_25pct", "g_rate_50pct", "g_rate_75pct", "g_rate_90pct"]
    # Stage B's ladder is derived from the host's core count, so its names are not
    # fixed. What is fixed is that it anchors at one worker and climbs.
    workers = [
        entry["spec"]["workers"]
        for entry in utils.read_stage(tmp_path, "b")["measurements"]
    ]
    assert workers[:2] == [1, 2]
    assert sorted(set(workers)) == workers


@pytest.mark.django_db(transaction=True)
def test_reports_a_spread_once_there_are_reps_to_compare(
    tmp_path: pathlib.Path,
) -> None:
    """Two reps have a spread; one has an unknown one, not a zero.

    Asserts a spread was computed, never that it was small: at these sizes stability is
    not something the harness can promise, and demanding it would fail the suite on an
    honest measurement.
    """
    stages.main(
        ["--stage", "f", "--reps", "2", "--tasks", "10", "--results-dir", str(tmp_path)]
    )

    result = utils.read_stage(tmp_path, "f")
    assert [isinstance(entry["spread"], float) for entry in result["measurements"]] == [
        True,
        True,
        True,
    ]
