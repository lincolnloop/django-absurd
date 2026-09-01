import pathlib

import pytest

from benchmarks import stages
from tests.benchmarks import utils

# Every test here drives a stage, and a stage spawns real `absurd_worker` children:
# separate processes on their own connections, which cannot see rows the test has
# not committed.
pytestmark = pytest.mark.django_db(transaction=True)

# Comfortably above the floor where a trimmed completion window still divides. How
# low that floor sits depends on the worker: at four tasks a concurrency-1 rung still
# measures while the concurrency-16 rung collapses, so a ladder sized near it flags
# for that rather than for whatever the test is about.
MEASURABLE_TASKS = "60"
# The other end of that floor: a single task completes at a single instant, so the
# p10-p90 window a throughput divides by is empty and every measurement in a ladder
# reports zero, however the worker is configured.
UNMEASURABLE_TASKS = "1"


def test_runs_the_producer_stage_at_the_size_it_was_asked_for(
    tmp_path: pathlib.Path,
) -> None:
    stages.main(
        [
            "producer_ceiling",
            "--reps",
            "1",
            "--tasks",
            "10",
            "--results-dir",
            str(tmp_path),
        ]
    )

    result = utils.read_stage(tmp_path, "producer_ceiling")
    assert [
        {
            "name": entry["spec"]["name"],
            "count": entry["median"]["count"],
            "spread": entry["spread"],
            "flagged": entry["flagged"],
        }
        for entry in result["measurements"]
    ] == [
        {"name": "single", "count": 10, "spread": None, "flagged": True},
        {"name": "threaded", "count": 10, "spread": None, "flagged": True},
        {"name": "atomic", "count": 10, "spread": None, "flagged": True},
    ]


def test_runs_a_saturation_stage_at_the_size_it_was_asked_for(
    tmp_path: pathlib.Path,
) -> None:
    stages.main(
        ["sync_vs_async", "--reps", "1", "--tasks", "8", "--results-dir", str(tmp_path)]
    )

    result = utils.read_stage(tmp_path, "sync_vs_async")
    assert [
        {
            "name": entry["spec"]["name"],
            "tasks": entry["median"]["n_tasks"],
            "spread": entry["spread"],
            "flagged": entry["flagged"],
        }
        for entry in result["measurements"]
    ] == [
        {"name": "async_c4", "tasks": 8, "spread": None, "flagged": True},
        {"name": "sync_c4", "tasks": 8, "spread": None, "flagged": True},
        {"name": "async_c16", "tasks": 8, "spread": None, "flagged": True},
        {"name": "sync_c16", "tasks": 8, "spread": None, "flagged": True},
        {"name": "async_c32", "tasks": 8, "spread": None, "flagged": True},
        {"name": "sync_c32", "tasks": 8, "spread": None, "flagged": True},
    ]


def test_runs_a_rate_stage_and_its_idle_probes_at_the_duration_it_was_asked_for(
    tmp_path: pathlib.Path,
) -> None:
    """Rate stages are sized in seconds, and the idle probes are rate work too.

    The probes carry their own hardcoded duration that neither size flag reaches, and
    they outlast every measurement they sit beside. How many tasks a paced offer gets
    out in a second is not fixed, so the count is not part of the result here.

    `--max-workers` is the probes' other size: a probe spawns four workers per poll
    interval by default, and its per-worker rate is divided by however many it got.
    """
    size = [
        "--reps",
        "1",
        "--tasks",
        MEASURABLE_TASKS,
        "--duration",
        "1",
        "--max-workers",
        "1",
    ]
    stages.main(["worker_knobs", *size, "--results-dir", str(tmp_path)])

    stages.main(["poll_interval", *size, "--results-dir", str(tmp_path)])

    result = utils.read_stage(tmp_path, "poll_interval")
    assert [
        {
            "name": entry["spec"]["name"],
            "duration_s": entry["spec"]["duration_s"],
            "spread": entry["spread"],
            "flagged": entry["flagged"],
        }
        for entry in result["measurements"]
    ] == [
        {"name": "poll_0.05", "duration_s": 1.0, "spread": None, "flagged": True},
        {"name": "poll_0.25", "duration_s": 1.0, "spread": None, "flagged": True},
        {"name": "poll_1", "duration_s": 1.0, "spread": None, "flagged": True},
    ]
    assert [
        {
            "poll_interval": probe["poll_interval"],
            "seconds": probe["seconds"],
            "workers": probe["workers"],
        }
        for probe in result["idle_probes"]
    ] == [
        {"poll_interval": 0.05, "seconds": 1.0, "workers": 1},
        {"poll_interval": 0.25, "seconds": 1.0, "workers": 1},
        {"poll_interval": 1.0, "seconds": 1.0, "workers": 1},
    ]


def test_runs_every_calibrated_stage_from_its_prerequisite(
    tmp_path: pathlib.Path,
) -> None:
    """The whole chain, in dependency order, at the smallest size that measures.

    B, C and E calibrate from A and G calibrates from B, each reading the earlier
    stage back off disk, so a stage that runs alone proves nothing about the chain.

    Three workers is the smallest bound that leaves stage B's ladder a ladder — at two
    it is only the anchors, and asserting it climbs would then be vacuous.
    """
    size = [
        "--reps",
        "1",
        "--tasks",
        MEASURABLE_TASKS,
        "--duration",
        "1",
        "--max-workers",
        "3",
    ]
    stages.main(["worker_knobs", *size, "--results-dir", str(tmp_path)])

    for stage in ("process_scaling", "checkpoint_cost", "latency_under_load"):
        stages.main([stage, *size, "--results-dir", str(tmp_path)])

    assert [
        entry["spec"]["name"]
        for entry in utils.read_stage(tmp_path, "checkpoint_cost")["measurements"]
    ] == ["flat", "workflow"]
    assert [
        entry["spec"]["name"]
        for entry in utils.read_stage(tmp_path, "latency_under_load")["measurements"]
    ] == ["rate_25pct", "rate_50pct", "rate_75pct", "rate_90pct"]
    # Stage B's ladder is derived from the host's core count, so its names are not
    # fixed. What is fixed is that it anchors at one worker, climbs, and honours the
    # bound — which a two-core host reaches before `--max-workers` does.
    workers = [
        entry["spec"]["workers"]
        for entry in utils.read_stage(tmp_path, "process_scaling")["measurements"]
    ]
    assert workers[:2] == [1, 2]
    assert sorted(set(workers)) == workers
    assert max(workers) <= 3
    # Stage G calibrates from one of those rungs, so the bound carries into its fleet.
    assert {
        entry["spec"]["workers"]
        for entry in utils.read_stage(tmp_path, "latency_under_load")["measurements"]
    } <= set(workers)


def test_runs_a_prerequisite_before_the_stage_that_calibrates_from_it(
    tmp_path: pathlib.Path,
) -> None:
    """Naming several stages runs them in dependency order, not the order given.

    E calibrates from A, so asking for them the wrong way round has to work or the
    ordering is the caller's problem rather than the driver's.
    """
    stages.main(
        [
            "checkpoint_cost",
            "worker_knobs",
            "--reps",
            "1",
            "--tasks",
            MEASURABLE_TASKS,
            "--results-dir",
            str(tmp_path),
        ]
    )

    assert [
        entry["spec"]["name"]
        for entry in utils.read_stage(tmp_path, "checkpoint_cost")["measurements"]
    ] == ["flat", "workflow"]


def test_reports_a_spread_once_there_are_reps_to_compare(
    tmp_path: pathlib.Path,
) -> None:
    """Two reps have a spread; one has an unknown one, not a zero.

    Asserts a spread was computed, never that it was small: at these sizes stability is
    not something the harness can promise, and demanding it would fail the suite on an
    honest measurement.
    """
    stages.main(
        [
            "producer_ceiling",
            "--reps",
            "2",
            "--tasks",
            "10",
            "--results-dir",
            str(tmp_path),
        ]
    )

    result = utils.read_stage(tmp_path, "producer_ceiling")
    assert [isinstance(entry["spread"], float) for entry in result["measurements"]] == [
        True,
        True,
        True,
    ]


def test_repeats_a_measurement_three_times_when_no_rep_count_is_given(
    tmp_path: pathlib.Path,
) -> None:
    """`--reps` is the one size flag whose default the harness owns, not the caller.

    Run at one task so the ladder is paying for nothing but that default; it cannot
    calibrate what comes after it, which is why the run ends where it does.
    """
    with pytest.raises(SystemExit):
        stages.main(
            [
                "worker_knobs",
                "--tasks",
                UNMEASURABLE_TASKS,
                "--results-dir",
                str(tmp_path),
            ]
        )

    assert [
        len(entry["reps"])
        for entry in utils.read_stage(tmp_path, "worker_knobs")["measurements"]
    ] == [3, 3, 3, 3, 3]


def test_records_an_unknown_git_sha_when_git_is_out_of_reach(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The compose `bench` container ships no git binary and no `.git` directory.

    Provenance is worth less than the measurement, so an unreachable git degrades the
    field rather than aborting the run. A `PATH` with no git on it is the container's
    condition, reproduced.
    """
    monkeypatch.setenv("PATH", str(tmp_path / "no-binaries-here"))

    stages.main(
        [
            "producer_ceiling",
            "--reps",
            "1",
            "--tasks",
            "10",
            "--results-dir",
            str(tmp_path),
        ]
    )

    result = utils.read_stage(tmp_path, "producer_ceiling")
    assert [entry["host"]["git_sha"] for entry in result["measurements"]] == [
        "unknown",
        "unknown",
        "unknown",
    ]


def test_refuses_every_producer_rep_the_host_slept_through(
    tmp_path: pathlib.Path,
) -> None:
    """A napped rep timed nothing, so it is thrown away rather than summarized.

    perf_counter stops with the host, so the enqueue rate a slept-through rep reports
    looks perfectly ordinary — the wall clock is the only witness that it is fiction.
    """
    with utils.nap_the_wall_clock():
        stages.main(
            [
                "producer_ceiling",
                "--reps",
                "1",
                "--tasks",
                "400",
                "--results-dir",
                str(tmp_path),
            ]
        )

    result = utils.read_stage(tmp_path, "producer_ceiling")
    assert [
        {
            "name": entry["spec"]["name"],
            "reps": [utils.normalize_measured_durations(rep) for rep in entry["reps"]],
            "median": entry["median"],
            "spread": entry["spread"],
            "flagged": entry["flagged"],
        }
        for entry in result["measurements"]
    ] == [
        {
            "name": name,
            "reps": [
                {
                    "valid": False,
                    "error": (
                        "Wall clock advanced Ns over a phase the monotonic clock "
                        "measured at Ns: the host suspended or stalled mid-phase, so "
                        "every number this phase produced is fiction. Re-run the "
                        "measurement on a machine that stays awake."
                    ),
                }
            ],
            "median": {},
            "spread": None,
            "flagged": True,
        }
        for name in ("single", "threaded", "atomic")
    ]


def test_refuses_a_stage_whose_prerequisite_was_never_run(
    capsys: pytest.CaptureFixture[str], tmp_path: pathlib.Path
) -> None:
    """A missing prerequisite is the caller's mistake, so it reads as one."""
    with pytest.raises(SystemExit) as exit_info:
        stages.main(["checkpoint_cost", "--results-dir", str(tmp_path)])

    assert (exit_info.value.code, capsys.readouterr().err) == (
        1,
        (
            f"{tmp_path / 'stage_worker_knobs.json'} is missing, and this stage is "
            "calibrated from it. Run `python -m benchmarks.stages worker_knobs` "
            "first.\n"
        ),
    )


def test_refuses_a_stage_whose_prerequisite_measured_no_throughput(
    capsys: pytest.CaptureFixture[str], tmp_path: pathlib.Path
) -> None:
    """worker_knobs calibrates its own batch-size measurements from its concurrency
    ladder, and at one task every rung of that ladder measures nothing."""
    with pytest.raises(SystemExit) as exit_info:
        stages.main(
            [
                "worker_knobs",
                "--reps",
                "1",
                "--tasks",
                UNMEASURABLE_TASKS,
                "--results-dir",
                str(tmp_path),
            ]
        )

    assert (exit_info.value.code, capsys.readouterr().err) == (
        1,
        (
            "None of the 5 recorded measurement(s) measured any throughput, so there "
            "is no winning configuration to calibrate the next stage from. Re-run the "
            "earlier stage on a quiet machine and check its flags.\n"
        ),
    )


def test_stops_the_run_at_the_stage_that_could_not_calibrate(
    tmp_path: pathlib.Path,
) -> None:
    """producer_ceiling depends on nothing and enqueues without measuring throughput.

    It is named alongside worker_knobs and runs after it, so the only reason it wrote
    no results file is that the run stopped where it broke.
    """
    with pytest.raises(SystemExit):
        stages.main(
            [
                "worker_knobs",
                "producer_ceiling",
                "--reps",
                "1",
                "--tasks",
                UNMEASURABLE_TASKS,
                "--results-dir",
                str(tmp_path),
            ]
        )

    assert [path.name for path in sorted(tmp_path.iterdir())] == [
        "stage_worker_knobs.json"
    ]
