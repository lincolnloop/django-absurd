import datetime as dt
import json
import pathlib
import typing as t

import pytest
from django.core.management import call_command
from django.db import connections

import analysis
import stages
from django_absurd.queues import resolve_absurd_database
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
# A durable body long enough to hold a thread and no longer, for the stages that are
# about something else: the production default would put minutes on this suite.
BRIEF_DURABLE_SECONDS = "0.05"
# Long enough that the connection probe's sampler, which reads `pg_stat_activity`
# every 50 ms, cannot miss the window in which every slot is working.
SAMPLEABLE_DURABLE_SECONDS = "0.5"


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
    a null the reader has to go look up — neither `--io-seconds` nor `--durable-seconds`
    was passed here and the file records the 0.05 s and the 2 s the harness used.
    `--duration` is the one with no single default to resolve to (a rate stage offers
    for 60 s, an idle probe runs for 30), so it records that the stage sized itself and
    each measurement's spec carries what it actually ran at.
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
        "durable_seconds": 2.0,
        "duration_s": None,
        "io_seconds": 0.05,
        "max_workers": 1,
        "reps": 1,
        "tasks": 10,
    }


def test_records_the_commit_ceiling_this_machine_was_measured_against(
    tmp_path: pathlib.Path,
) -> None:
    """A throughput is a property of this connection unless the ceiling says so.

    Recorded as a distribution, because the durable rate is one: a median with the CV
    and the endpoints of the rounds it came from, in the same vocabulary a
    measurement's reps use. Bookended rather than measured once — a run leaves behind
    the bloat and the WAL churn it made, so only the closing probe says whether the
    ceiling its throughputs were read against still held at the end.

    Asserted as measured, as bracketed and as ordered — taking the fsync out cannot
    make a server commit slower — never at a number, which is the machine's to decide.
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
    durable = result["commit_ceiling_durable"]
    assert {
        "measured": durable["valid"],
        "durable": durable["median_per_s"] > 0,
        "durable_after": result["commit_ceiling_durable_after"]["median_per_s"] > 0,
        "measured_its_own_dispersion": durable["cv"] >= 0,
        "endpoints_bracket_the_median": (
            durable["range_low"] <= durable["median_per_s"] <= durable["range_high"]
        ),
        "fsync_costs_something": (
            result["commit_ceiling_nondurable"]["median_per_s"]
            > durable["median_per_s"]
        ),
    } == {
        "measured": True,
        "durable": True,
        "durable_after": True,
        "measured_its_own_dispersion": True,
        "endpoints_bracket_the_median": True,
        "fsync_costs_something": True,
    }


def test_records_what_the_server_said_when_the_probe_was_refused(
    tmp_path: pathlib.Path,
) -> None:
    """A run nobody could calibrate still runs; it records why it has no ceiling.

    Refusing to measure because a calibration probe failed would be worse than
    reporting an uncalibrated number and saying so, which is what these blocks and the
    report's header line together do. What they say is that the server was ASKED and
    said no — a run that ended before a probe was ever taken records the other thing,
    and a reader deciding whether to trust a rate has to tell them apart.
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
        "commit_ceiling_durable": result["commit_ceiling_durable"],
        "commit_ceiling_nondurable": result["commit_ceiling_nondurable"],
        "commit_ceiling_durable_after": result["commit_ceiling_durable_after"],
        "measured_anyway": result["measurements"][0]["median"]["enqueues_per_s"] > 0,
    } == {
        "commit_ceiling_durable": {
            "valid": False,
            "error": (
                "the server refused the probe: relation "
                '"benchmark_commit_ceiling_probe" already exists'
            ),
        },
        "commit_ceiling_nondurable": {
            "valid": False,
            "error": (
                "the server refused the probe: relation "
                '"benchmark_commit_ceiling_probe" already exists'
            ),
        },
        "commit_ceiling_durable_after": {
            "valid": False,
            "error": (
                "the server refused the probe: relation "
                '"benchmark_commit_ceiling_probe" already exists'
            ),
        },
        "measured_anyway": True,
    }


def test_a_run_that_died_still_records_the_ceiling_its_stages_were_read_against(
    tmp_path: pathlib.Path,
) -> None:
    """A stage that raises must not take the finished stages' calibration with it.

    The closing probe is what says whether the ceiling those stages were read against
    still held, and it lands after the last stage — so an invocation that dies at a
    later one has to take it anyway, or 75 minutes of measurement bands against the
    opening probe alone for the sake of the stage that failed. The stage that never
    wrote a file is skipped rather than being an error of its own.

    latency_under_load reads `stage_process_scaling.json` back off disk, and there is
    none here, so it raises after producer_ceiling has finished and written its own.
    """
    with pytest.raises(SystemExit) as exit_info:
        stages.main(
            [
                "producer_ceiling",
                "latency_under_load",
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
        "exit_code": exit_info.value.code,
        "kept_its_measurements": len(result["measurements"]),
        "closing_probe_measured": result["commit_ceiling_durable_after"]["valid"],
        "wrote_the_stage_that_never_ran": (
            tmp_path / "stage_latency_under_load.json"
        ).exists(),
    } == {
        "exit_code": 1,
        "kept_its_measurements": 3,
        "closing_probe_measured": True,
        "wrote_the_stage_that_never_ran": False,
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


def test_calibrates_from_the_fastest_rung_no_mark_disqualified(
    tmp_path: pathlib.Path,
) -> None:
    """The working point is an argmax over the rungs a mark left in the running.

    A marked rung measured something other than what it was asked to, so calibrating
    on one aims every later stage at a number the run itself refused — and the rung
    that a mark disqualifies is often the FASTEST, because measuring less than it was
    asked to is exactly what makes it look fast. The two clean rungs differ in
    throughput so that picking either end of them is a different answer.
    """
    (tmp_path / "stage_worker_knobs.json").write_text(
        json.dumps(
            {
                "measurements": [
                    build_recorded_rung("invalid_c8", 900.0, 8, invalid=True),
                    build_recorded_rung("unstable_c16", 800.0, 16, unstable=True),
                    build_recorded_rung("clean_c2", 500.0, 2),
                    build_recorded_rung("clean_c32", 100.0, 32),
                ]
            }
        )
    )

    stages.main(
        [
            "checkpoint_cost",
            "--reps",
            "1",
            "--tasks",
            "8",
            "--results-dir",
            str(tmp_path),
        ]
    )

    recorded = utils.read_stage(tmp_path, "checkpoint_cost")
    assert {
        "calibration": recorded["calibration"],
        "concurrency_each_measurement_ran_at": [
            entry["spec"]["worker"]["concurrency"] for entry in recorded["measurements"]
        ],
    } == {
        "calibration": {
            "stage": "worker_knobs",
            "measurement": "clean_c2",
            "throughput_per_s": 500.0,
            "cv": 0.02,
            "invalid": False,
            "unstable": False,
        },
        "concurrency_each_measurement_ran_at": [2, 2],
    }


def build_recorded_rung(
    name: str,
    throughput_per_s: float,
    concurrency: int,
    *,
    invalid: bool = False,
    unstable: bool = False,
) -> dict[str, t.Any]:
    """One rung of a recorded stage file, as much of one as a reader of it needs.

    Written rather than measured because the marks are the input: a real ladder is
    marked by what the machine did during it, and this stage picks between rungs on
    marks it cannot be asked to produce on demand.
    """
    return {
        "spec": {
            "name": name,
            "workers": 1,
            "worker": {
                "concurrency": concurrency,
                "batch_size": None,
                "poll_interval": 0.05,
                "claim_timeout": 120,
                "queue": "bench",
            },
        },
        "median": {"throughput_per_s": throughput_per_s},
        "cv": 0.02,
        "invalid": invalid,
        "unstable": unstable,
    }


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
        ("--durable-seconds", "0", "0.001"),
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
    sort, a zero-length window is what a rate divides by, and a durable body of no
    duration is the nano-task arm again under a durable name.
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


# Four stages in one body, where every other test here drives one: the suite's own
# 120 s alarm is sized for a single stage, and firing it mid-drain would fail the test
# for its length rather than for anything it asserts. Raised rather than split because
# what is under test IS the chain — each stage reads the previous one back off disk,
# so splitting it either re-runs worker_knobs per test or moves the same 70 seconds
# into one test's fixture setup, where the same alarm still covers it.
@pytest.mark.timeout(600)
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
    latency = utils.read_stage(tmp_path, "latency_under_load")
    assert {entry["spec"]["workers"] for entry in latency["measurements"]} <= set(
        workers
    )
    # What it inherits from that rung is the FLEET and the drain rate its own ramp
    # climbs from — never the offered rate, which is the ramp's own measurement.
    ramp = latency["sustainable_rate"]
    assert ramp["drain_ceiling_per_s"] == latency["calibration"]["throughput_per_s"]
    assert [probe["rate_per_s"] for probe in ramp["probes"]] == [
        pytest.approx(
            ramp["drain_ceiling_per_s"]
            * stages.RATE_RAMP_START_FRACTION
            * stages.RATE_RAMP_STEP**step
        )
        for step in range(len(ramp["probes"]))
    ]
    # It stopped at the first offer the fleet could not absorb, and measured at the
    # highest one it could; which rung that is belongs to the machine, not the test.
    assert all(probe["sustained"] for probe in ramp["probes"][:-1]) is True
    assert [entry["spec"]["rate_per_s"] for entry in latency["measurements"]] == [
        pytest.approx(ramp["rate_per_s"] * fraction)
        for fraction in stages.RATE_FRACTIONS
    ]


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


def test_records_the_cluster_name_of_the_server_it_measured(
    tmp_path: pathlib.Path,
) -> None:
    """`db_bench` names its cluster `bench-tmpfs` because its data directory is RAM,
    and that name is the only thing in a results file that says an absolute rate off
    it is not a durable one. The suites run against `db`, which declares no name, so
    what this pins is that the field is read off the server the run measured rather
    than assumed."""
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute("show cluster_name")
        (declared,) = cursor.fetchone()

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
    assert [entry["host"]["cluster_name"] for entry in result["measurements"]] == [
        declared,
        declared,
        declared,
    ]


def test_records_the_resource_settings_of_the_server_it_measured(
    tmp_path: pathlib.Path,
) -> None:
    """`shared_buffers` and `max_connections` are overridable per machine, so two
    results files could differ for a reason nothing in either explains unless both
    carry them.

    Read off the server, not out of the harness's environment: the variables size the
    container at `up` time and the process taking the measurement need never have seen
    them, while a server already running answers for what it was actually started
    with. Asserted against what the server reports and never at a value — the suites'
    `db` runs at its own defaults, which is the point."""
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute("show shared_buffers")
        (shared_buffers,) = cursor.fetchone()
        cursor.execute("show max_connections")
        (max_connections,) = cursor.fetchone()

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
        (entry["host"]["shared_buffers"], entry["host"]["max_connections"])
        for entry in result["measurements"]
    ] == [(shared_buffers, max_connections)] * 3


def test_records_the_container_limits_the_run_was_asked_to_impose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A container CPU or memory limit reaches no SQL, so the variables that set it are
    the only witness a measurement has — and they witness a REQUEST, which is what the
    field is named for. Set here the way `docker compose up` reads them, because
    nothing in a run can be made to observe the limit itself; an unset variable stays
    unknown rather than becoming `unlimited`."""
    monkeypatch.setenv("BENCH_CPUS", "4")
    monkeypatch.setenv("BENCH_MEMORY", "8g")

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
        (
            entry["host"]["requested_container_cpus"],
            entry["host"]["requested_container_memory"],
        )
        for entry in result["measurements"]
    ] == [("4", "8g")] * 3


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


def test_alternates_the_two_shapes_of_a_pooled_vs_split_pair_across_its_reps(
    tmp_path: pathlib.Path,
) -> None:
    """Both arms of a pair run back to back, and the pair runs both ways round.

    Cumulative database state only grows across a stage, so an arm that always went
    first would drain the emptier tables every time and nothing in its row would say
    so. Reversing the schedule on the odd reps is what stops that lining up — the
    mistake that invalidated an earlier control in this repo.

    Four arms, not two: each total is measured on a nano-task body and on a durable
    one, and the durable pair alternates like any other.
    """
    stages.main(
        [
            "pooled_vs_split",
            "--reps",
            "2",
            "--tasks",
            MEASURABLE_TASKS,
            "--durable-seconds",
            BRIEF_DURABLE_SECONDS,
            "--max-workers",
            "4",
            "--results-dir",
            str(tmp_path),
        ]
    )

    result = utils.read_stage(tmp_path, "pooled_vs_split")
    assert {
        "measurements": [entry["spec"]["name"] for entry in result["measurements"]],
        "reps": [len(entry["reps"]) for entry in result["measurements"]],
        "run_order": result["run_order"],
    } == {
        "measurements": [
            "pooled_4",
            "split_4",
            "pooled_durable_4",
            "split_durable_4",
        ],
        "reps": [2, 2, 2, 2],
        "run_order": [
            "pooled_4",
            "split_4",
            "pooled_durable_4",
            "split_durable_4",
            "split_durable_4",
            "pooled_durable_4",
            "split_4",
            "pooled_4",
        ],
    }


def test_records_the_backends_each_pooled_vs_split_shape_opened(
    tmp_path: pathlib.Path,
) -> None:
    """The comparison's own confound, and what a working body does to it.

    An IDLE worker process opens two backends whatever its concurrency: the SDK
    client's own connection and Django's. So the two ways of reaching one total
    concurrency do not reach it on the same number of connections — four slots in one
    process share one claim connection where four processes have four.

    A durable body changes the second number entirely. A sync body runs on the
    worker's own thread pool and Django's connections are thread-local, so ORM work
    inside one opens a backend that lives as long as the body: every busy slot is one
    more backend, and a process working flat out holds its concurrency plus two.

    The 8 pair is skipped whole rather than run at the bound: an 8x1 quietly bounded
    to 4x1 is an unequal comparison presented as an equal one.
    """
    stages.main(
        [
            "pooled_vs_split",
            "--reps",
            "1",
            "--tasks",
            MEASURABLE_TASKS,
            "--durable-seconds",
            SAMPLEABLE_DURABLE_SECONDS,
            "--max-workers",
            "4",
            "--results-dir",
            str(tmp_path),
        ]
    )

    result = utils.read_stage(tmp_path, "pooled_vs_split")
    assert {
        "measurements": [entry["spec"]["name"] for entry in result["measurements"]],
        "shape_connections": result["shape_connections"],
        "skipped_pairs": result["skipped_pairs"],
    } == {
        "measurements": [
            "pooled_4",
            "split_4",
            "pooled_durable_4",
            "split_durable_4",
        ],
        "shape_connections": [
            {
                "shape": "1x4",
                "processes": 1,
                "concurrency": 4,
                "connections_idle": 2,
                "connections_busy": 6,
            },
            {
                "shape": "4x1",
                "processes": 4,
                "concurrency": 1,
                "connections_idle": 8,
                "connections_busy": 12,
            },
        ],
        "skipped_pairs": [{"total": 8, "max_workers": 4}],
    }


def test_measures_no_pooled_vs_split_pair_the_worker_bound_cannot_spawn(
    capsys: pytest.CaptureFixture[str], tmp_path: pathlib.Path
) -> None:
    """A bound under every total leaves nothing to compare, and the file says so.

    It is not an error — a bounded full run must not die at this stage — and it is not
    silence either: the pairs it refused and the bound that refused them are on the
    console as it happens and in the results file afterwards.
    """
    stages.main(
        [
            "pooled_vs_split",
            "--reps",
            "1",
            "--tasks",
            MEASURABLE_TASKS,
            "--max-workers",
            "1",
            "--results-dir",
            str(tmp_path),
        ]
    )

    result = utils.read_stage(tmp_path, "pooled_vs_split")
    assert {
        "measurements": result["measurements"],
        "run_order": result["run_order"],
        "shape_connections": result["shape_connections"],
        "skipped_pairs": result["skipped_pairs"],
    } == {
        "measurements": [],
        "run_order": [],
        "shape_connections": [],
        "skipped_pairs": [
            {"total": 4, "max_workers": 1},
            {"total": 8, "max_workers": 1},
        ],
    }
    assert capsys.readouterr().out == (
        "stage POOLED_VS_SPLIT: one total concurrency reached two ways: slots in one "
        "process, or one slot in each of that many processes, on a nano-task body and "
        "on a durable one\n"
        "total 4: not run, --max-workers 1 cannot spawn its 4-process split arm\n"
        "total 8: not run, --max-workers 1 cannot spawn its 8-process split arm\n"
    )


@pytest.mark.usefixtures("_isolate_queues")
def test_measures_one_pending_depth_on_three_sizes_of_table(
    tmp_path: pathlib.Path,
) -> None:
    """The stage's whole design, in the two things a results file has to show.

    Every arm drains exactly `--tasks`: the ballast is laid and drained before the
    measured tasks exist and the metrics' window opens after it, so a ballast that
    leaked in reads here as a task count nobody asked for. And the tables each arm
    drained on: four times the rows at the same pending depth, in BOTH tables, an
    enqueue writing a run row as well as a task row. Only the vacuumed arm's dead rows
    are asserted — autovacuum may already have taken the other's.

    The one test here that reads absolute row counts, so it is the one that takes the
    queue topology away on both sides and provisions its own.
    """
    call_command("absurd_sync_queues")  # _isolate_queues dropped the queue on setup

    stages.main(
        [
            "size_vs_depth",
            "--reps",
            "1",
            "--tasks",
            MEASURABLE_TASKS,
            "--results-dir",
            str(tmp_path),
        ]
    )

    result = utils.read_stage(tmp_path, "size_vs_depth")
    assert [
        {
            "name": entry["spec"]["name"],
            "ballast_tasks": entry["spec"]["ballast_tasks"],
            "vacuum_ballast": entry["spec"]["vacuum_ballast"],
            "n_tasks": entry["median"]["n_tasks"],
            "missing_tasks": entry["median"]["missing_tasks"],
            "task_rows": entry["median"]["table"]["tasks"]["live_tuples"],
            "run_rows": entry["median"]["table"]["runs"]["live_tuples"],
        }
        for entry in result["measurements"]
    ] == [
        {
            "name": "fresh_table",
            "ballast_tasks": 0,
            "vacuum_ballast": False,
            "n_tasks": 60,
            "missing_tasks": 0,
            "task_rows": 60,
            "run_rows": 60,
        },
        {
            "name": "aged_table",
            "ballast_tasks": 180,
            "vacuum_ballast": False,
            "n_tasks": 60,
            "missing_tasks": 0,
            "task_rows": 240,
            "run_rows": 240,
        },
        {
            "name": "vacuumed_table",
            "ballast_tasks": 180,
            "vacuum_ballast": True,
            "n_tasks": 60,
            "missing_tasks": 0,
            "task_rows": 240,
            "run_rows": 240,
        },
    ]
    assert [
        entry["median"]["table"]["runs"]["dead_tuples"]
        for entry in result["measurements"]
        if entry["spec"]["vacuum_ballast"]
    ] == [0]


def test_installs_the_extension_a_run_itemises_its_tasks_with(
    tmp_path: pathlib.Path,
) -> None:
    """Nothing counts a statement until the extension exists on this database.

    It is per-database and `db_bench` keeps its data directory in RAM, so every restart
    hands a run a server that has never had it — which makes this a bootstrap step of
    the run rather than a setup instruction somebody would have to remember. Driven
    through the cheapest stage there is, because the bootstrap belongs to the run and
    not to any stage.

    Installed is all this can assert here: the suite's server does not preload the
    library, so the view it creates counts nothing and reads back as no statement
    stats, which `tests/benchmarks/test_smoke.py` asserts.
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

    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute(
            "select count(*) from pg_extension where extname = %s",
            [analysis.STATEMENT_STATS_EXTENSION],
        )
        installed = cursor.fetchone()[0]
    result = utils.read_stage(tmp_path, "producer_ceiling")
    assert {
        "installed": installed,
        "measured": result["measurements"][0]["median"]["enqueues_per_s"] > 0,
    } == {"installed": 1, "measured": True}


def test_runs_without_statement_stats_when_the_extension_cannot_be_created(
    tmp_path: pathlib.Path,
) -> None:
    """A managed database whose role cannot create extensions still gets its run.

    The instrument is worth a run's statement stats and not the run itself, so a
    refused bootstrap costs the itemisation and nothing else — the same trade the
    commit-ceiling probe already makes when it is refused.
    """
    with utils.hold_the_statement_stats_name():
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
        with connections[resolve_absurd_database()].cursor() as cursor:
            cursor.execute(
                "select count(*) from pg_extension where extname = %s",
                [analysis.STATEMENT_STATS_EXTENSION],
            )
            installed = cursor.fetchone()[0]

    result = utils.read_stage(tmp_path, "producer_ceiling")
    assert {
        "installed": installed,
        "measured_anyway": result["measurements"][0]["median"]["enqueues_per_s"] > 0,
    } == {"installed": 0, "measured_anyway": True}


@pytest.mark.timeout(300)
def test_a_saturation_rep_counts_runs_over_the_window_its_commits_came_from(
    tmp_path: pathlib.Path,
) -> None:
    """The commit counter starts once the fleet is up, so the run counter must too.

    Two machine-independent things about the mark a rep recorded: it sits past that
    rep's whole preload, which is what starts before the fleet does, and the run count
    beside it is the one taken over it. Every rep truncates the queue in front of
    itself, so the rows read below are the last rung's own, and
    `tests/benchmarks/test_metrics.py` pins the windowing itself at chosen timestamps.
    """
    (tmp_path / "stage_worker_knobs.json").write_text(
        json.dumps({"measurements": [build_recorded_rung("clean_c1", 500.0, 1)]})
    )

    stages.main(
        [
            "process_scaling",
            "--reps",
            "1",
            "--tasks",
            MEASURABLE_TASKS,
            "--max-workers",
            "2",
            "--results-dir",
            str(tmp_path),
        ]
    )

    fleet = next(
        entry
        for entry in utils.read_stage(tmp_path, "process_scaling")["measurements"]
        if entry["spec"]["workers"] == 2
    )
    window_start = fleet["median"]["window_start"]
    assert {
        "runs_counted_over_the_mark": fleet["median"]["n_runs"],
        "mark_is_past_the_preload": (
            dt.datetime.fromisoformat(window_start) > utils.read_latest_enqueue_at()
        ),
    } == {
        "runs_counted_over_the_mark": utils.count_runs_completed_after(window_start),
        "mark_is_past_the_preload": True,
    }
