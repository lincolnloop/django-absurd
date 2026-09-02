import pathlib

import pytest

import stages
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
            "cv": entry["cv"],
            "invalid": entry["invalid"],
            "unstable": entry["unstable"],
        }
        for entry in result["measurements"]
    ] == [
        {
            "name": name,
            "count": 10,
            "spread": None,
            "cv": None,
            "invalid": False,
            "unstable": False,
        }
        for name in ("single", "threaded", "atomic")
    ]


def test_records_the_configuration_a_stage_was_run_at(
    tmp_path: pathlib.Path,
) -> None:
    """A results file that cannot name its configuration cannot be compared with
    another one: the same worker ladder comes out of an unbounded run on a ten-core
    host and a run bounded to ten on a fourteen-core one.

    Resolved rather than raw, so an unset flag records what it fell back to instead of
    a null the reader has to go look up — `--io-seconds` was not passed here and the
    file records the 0.05 s the harness used. `--duration` is the one with no single
    default to resolve to (a rate stage offers for 60 s, an idle probe runs for 30), so
    it records that the stage sized itself and each measurement's spec carries what it
    actually ran at.
    """
    stages.main(
        [
            "producer_ceiling",
            "--reps",
            "1",
            "--tasks",
            "10",
            "--max-workers",
            "1",
            "--results-dir",
            str(tmp_path),
        ]
    )

    assert utils.read_stage(tmp_path, "producer_ceiling")["options"] == {
        "duration_s": None,
        "io_seconds": 0.05,
        "max_workers": 1,
        "reps": 1,
        "tasks": 10,
    }


def test_records_the_commit_ceiling_this_machine_was_measured_against(
    tmp_path: pathlib.Path,
) -> None:
    """A throughput is a property of this disk unless the ceiling beside it says so.

    Bookended rather than measured once: sustained write load depresses the ceiling
    and it recovers when the machine goes idle, so the same run means different things
    at its start and at its end. Asserted as measured and as ordered — taking the
    fsync out cannot make a server commit slower — never at a number, which is the
    machine's to decide.
    """
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
    assert {
        "durable": result["commit_ceiling_durable_per_s"] > 0,
        "durable_after": result["commit_ceiling_durable_after_per_s"] > 0,
        "fsync_costs_something": (
            result["commit_ceiling_nondurable_per_s"]
            > result["commit_ceiling_durable_per_s"]
        ),
    } == {"durable": True, "durable_after": True, "fsync_costs_something": True}


def test_records_no_commit_ceiling_when_the_probe_was_refused(
    tmp_path: pathlib.Path,
) -> None:
    """A run nobody could calibrate still runs; it records that it has no ceiling.

    Refusing to measure because a calibration probe failed would be worse than
    reporting an uncalibrated number and saying so, which is what the null here and
    the report's header line together do.
    """
    with utils.hold_the_commit_probe_table():
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
    assert {
        "commit_ceiling_durable_per_s": result["commit_ceiling_durable_per_s"],
        "commit_ceiling_nondurable_per_s": result["commit_ceiling_nondurable_per_s"],
        "commit_ceiling_durable_after_per_s": result[
            "commit_ceiling_durable_after_per_s"
        ],
        "measured_anyway": result["measurements"][0]["median"]["enqueues_per_s"] > 0,
    } == {
        "commit_ceiling_durable_per_s": None,
        "commit_ceiling_nondurable_per_s": None,
        "commit_ceiling_durable_after_per_s": None,
        "measured_anyway": True,
    }


def test_records_which_measurement_became_the_working_point(
    tmp_path: pathlib.Path,
) -> None:
    """A calibrated stage inherits one rung of an earlier one, silently until now.

    worker_knobs calibrates its own batch-size measurements from its concurrency
    ladder and checkpoint_cost calibrates from the whole of worker_knobs, so both
    files have to name what they were configured at and how well it repeated. The
    stage that measured it is named too: a results directory holds several stages and
    a bare measurement name says nothing about which one it came out of.
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

    knobs = utils.read_stage(tmp_path, "worker_knobs")
    rungs = [entry["spec"]["name"] for entry in knobs["measurements"]]
    checkpoints = utils.read_stage(tmp_path, "checkpoint_cost")
    assert [
        {
            "stage": entry["stage"],
            "names_a_rung_of_worker_knobs": entry["measurement"] in rungs,
            "measured_a_throughput": entry["throughput_per_s"] > 0,
            "cv": entry["cv"],
            "recorded_whether_the_rung_was_valid": isinstance(entry["invalid"], bool),
            "unstable": entry["unstable"],
        }
        for entry in (knobs["calibration"], checkpoints["calibration"])
    ] == [
        {
            "stage": "worker_knobs",
            "names_a_rung_of_worker_knobs": True,
            "measured_a_throughput": True,
            # One rep has nothing to disagree with, so the rung every later
            # measurement here was configured at has an unmeasured dispersion — which
            # is exactly the kind of thing this block exists to carry forward.
            "cv": None,
            "recorded_whether_the_rung_was_valid": True,
            "unstable": False,
        }
    ] * 2


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
            "cv": entry["cv"],
            "unstable": entry["unstable"],
        }
        for entry in result["measurements"]
    ] == [
        {
            "name": name,
            "tasks": 8,
            "spread": None,
            "cv": None,
            "unstable": False,
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


def test_runs_a_rate_stage_and_its_idle_probes_at_the_duration_it_was_asked_for(
    tmp_path: pathlib.Path,
) -> None:
    """Rate stages are sized in seconds, and the idle probes are rate work too.

    The probes carry their own hardcoded duration that neither size flag reaches, and
    they outlast every measurement they sit beside. How many tasks a paced offer gets
    out in a second is not fixed, so the count is not part of the result here.

    `--max-workers` is the probes' other size: a probe spawns four workers per poll
    interval by default, and its per-worker rate is divided by however many it got.

    A rate rep's own metrics come off the trimmed middle of that offer, so the phase it
    records is the only witness to the offer and the drain around it: every task is
    enqueued and completed inside it, which is why it outlasts the slowest of them.
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
            "cv": entry["cv"],
            "unstable": entry["unstable"],
            "phase_outlasted_its_slowest_task": (
                entry["reps"][0]["phase_s"] > entry["reps"][0]["end_to_end_p99_s"]
            ),
        }
        for entry in result["measurements"]
    ] == [
        {
            "name": name,
            "duration_s": 1.0,
            "spread": None,
            "cv": None,
            "unstable": False,
            "phase_outlasted_its_slowest_task": True,
        }
        for name in ("poll_0.05", "poll_0.25", "poll_1")
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


def test_prints_a_latency_percentile_only_for_the_stages_that_paced_their_offer(
    capsys: pytest.CaptureFixture[str], tmp_path: pathlib.Path
) -> None:
    """The console says the same thing the report's table does, for the same reason.

    A saturation run starts with a full queue, so every task but the first waited
    behind the whole backlog and its percentiles are drain time wearing latency's
    name. Only a paced offer has a latency worth printing.
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
        "--results-dir",
        str(tmp_path),
    ]
    stages.main(["worker_knobs", *size])
    capsys.readouterr()

    stages.main(["checkpoint_cost", *size])
    saturation = capsys.readouterr().out
    stages.main(["poll_interval", *size])
    paced = capsys.readouterr().out

    assert utils.strip_measurement_marks(
        utils.normalize_measured_numbers(saturation)
    ) == (
        "stage CHECKPOINT_COST: checkpoint cost: a 4-step workflow against a flat "
        "task\n"
        "flat: N tasks/s, spread n/a, cv n/a\n"
        "workflow: N tasks/s, spread n/a, cv n/a\n"
    )
    assert utils.strip_measurement_marks(utils.normalize_measured_numbers(paced)) == (
        "stage POLL_INTERVAL: latency under a paced offer, plus idle claim-rate "
        "probes\n"
        "poll_0.05: N tasks/s, e2e p50 Nms, spread n/a, cv n/a\n"
        "poll_0.25: N tasks/s, e2e p50 Nms, spread n/a, cv n/a\n"
        "poll_1: N tasks/s, e2e p50 Nms, spread n/a, cv n/a\n"
        "idle poll=0.05: N claims/s/worker\n"
        "idle poll=0.25: N claims/s/worker\n"
        "idle poll=1: N claims/s/worker\n"
    )


def test_records_how_long_each_measured_phase_took(tmp_path: pathlib.Path) -> None:
    """Every other number a rep carries comes off a window that trims the ramp and the
    tail, so the phase is the only thing that says where the rep's time went.

    Each task's second of simulated IO is spent inside the drain phase — the workers
    start before it opens and the queue is empty when it closes — so a phase that
    timed itself outlasts the IO it drained.
    """
    stages.main(
        [
            "sync_vs_async",
            "--reps",
            "1",
            "--tasks",
            "8",
            "--io-seconds",
            "1",
            "--results-dir",
            str(tmp_path),
        ]
    )

    result = utils.read_stage(tmp_path, "sync_vs_async")
    assert [
        {
            "name": entry["spec"]["name"],
            "outlasted_the_io_it_drained": entry["reps"][0]["phase_s"] > 1.0,
        }
        for entry in result["measurements"]
    ] == [
        {"name": name, "outlasted_the_io_it_drained": True}
        for name in (
            "async_c4",
            "sync_c4",
            "async_c16",
            "sync_c16",
            "async_c32",
            "sync_c32",
        )
    ]


@pytest.mark.parametrize(
    ("flag", "value", "floor"),
    [
        ("--duration", "0", "0.001"),
        ("--max-workers", "-3", "1"),
        ("--max-workers", "0", "1"),
        ("--tasks", "0", "1"),
    ],
)
def test_refuses_a_size_that_leaves_a_stage_nothing_to_measure(
    capsys: pytest.CaptureFixture[str],
    flag: str,
    floor: str,
    tmp_path: pathlib.Path,
    value: str,
) -> None:
    """Refused before anything runs, rather than at the number it would have written.

    Each goes wrong in its own way and none announces it: an empty ladder writes no
    results file while reporting success, a negative fleet divides an idle probe's
    claim rate into a rate no worker produced, no tasks leaves a percentile nothing to
    sort, and a zero-length window is what a rate divides by.
    """
    with pytest.raises(SystemExit) as exit_info:
        stages.main(["process_scaling", flag, value, "--results-dir", str(tmp_path)])

    assert (exit_info.value.code, capsys.readouterr().err) == (
        1,
        (
            f"{flag} {value} is below {floor}, which leaves a stage nothing to "
            "measure — no worker to spawn, no task to drain, or no window to divide "
            "by. Every number it recorded would describe work that never happened.\n"
        ),
    )


def test_runs_every_calibrated_stage_from_its_prerequisite(
    tmp_path: pathlib.Path,
) -> None:
    """The whole chain, in dependency order, at the smallest size that measures.

    process_scaling, poll_interval and checkpoint_cost calibrate from worker_knobs,
    and latency_under_load from process_scaling, each reading the earlier stage back
    off disk, so a stage that runs alone proves nothing about the chain.

    Three workers is the smallest bound that leaves the process_scaling ladder a
    ladder — at two it is only the anchors, and asserting it climbs would then be
    vacuous.
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
    # The process_scaling ladder is derived from the host's core count, so its names
    # are not fixed. What is fixed is that it anchors at one worker, climbs, and
    # honours the bound — which a two-core host reaches before `--max-workers` does.
    scaling = utils.read_stage(tmp_path, "process_scaling")
    workers = [entry["spec"]["workers"] for entry in scaling["measurements"]]
    assert workers[:2] == [1, 2]
    assert sorted(set(workers)) == workers
    assert max(workers) <= 3
    # The fleet ceiling the file records is the rung the ladder actually topped out at,
    # whichever of the bound and the host's core count set it.
    assert max(workers) == scaling["options"]["max_workers"]
    # latency_under_load calibrates from one of those rungs, so the bound carries
    # into its fleet.
    assert {
        entry["spec"]["workers"]
        for entry in utils.read_stage(tmp_path, "latency_under_load")["measurements"]
    } <= set(workers)


def test_runs_a_prerequisite_before_the_stage_that_calibrates_from_it(
    tmp_path: pathlib.Path,
) -> None:
    """Naming several stages runs them in dependency order, not the order given.

    checkpoint_cost calibrates from worker_knobs, so asking for them the wrong way
    round has to work or the ordering is the caller's problem rather than the
    driver's.
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


def test_reports_a_dispersion_once_there_are_reps_to_compare(
    tmp_path: pathlib.Path,
) -> None:
    """Two reps have a spread and a CV; one has unknown ones, not zeroes.

    Asserts they were computed, never that they were small: at these sizes stability is
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
    assert [
        (isinstance(entry["spread"], float), isinstance(entry["cv"], float))
        for entry in result["measurements"]
    ] == [(True, True)] * 3


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

    result = utils.read_stage(tmp_path, "worker_knobs")
    assert [len(entry["reps"]) for entry in result["measurements"]] == [3, 3, 3, 3, 3]
    # And the file says so, rather than leaving the reader to count them.
    assert result["options"]["reps"] == 3


def test_records_an_unknown_git_sha_when_git_is_out_of_reach(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A harness run is not always in a checkout with git on its PATH.

    Provenance is worth less than the measurement, so an unreachable git degrades the
    field rather than aborting the run. A `PATH` with no git on it is that condition,
    reproduced.
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
            "cv": entry["cv"],
            "range_low": entry["range_low"],
            "range_high": entry["range_high"],
            "invalid": entry["invalid"],
            "unstable": entry["unstable"],
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
                    "load_before": True,
                    "load_after": True,
                }
            ],
            "median": {},
            "spread": None,
            "cv": None,
            "range_low": None,
            "range_high": None,
            "invalid": True,
            "unstable": False,
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
            "calibrated from it. Run `python -m stages worker_knobs` "
            "first.\n"
        ),
    )


def test_refuses_a_stage_whose_prerequisite_measured_no_throughput(
    capsys: pytest.CaptureFixture[str], tmp_path: pathlib.Path
) -> None:
    """worker_knobs calibrates its own batch-size measurements from its concurrency
    ladder, and at one task every rung of that ladder measures nothing.

    Every rung is marked on the console as it goes, and marked for two separate
    things: one task completes at a single instant, so the window a rate divides by is
    empty (invalid), and one rep has nothing to disagree with (dispersion unmeasured).
    Neither is the other, which is the whole reason they are two marks.
    """
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

    captured = capsys.readouterr()
    assert utils.normalize_measured_numbers(captured.out) == (
        "stage WORKER_KNOBS: one worker's knobs: concurrency ladder, then batch "
        "size, then async dispatch\n"
        + "".join(
            f"concurrency_{rung}: N tasks/s, spread n/a, cv n/a "
            "[INVALID DISPERSION UNMEASURED]\n"
            for rung in (1, 2, 4, 8, 16)
        )
    )
    assert (exit_info.value.code, captured.err) == (
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
