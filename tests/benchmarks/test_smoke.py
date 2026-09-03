import dataclasses
import pathlib
import time
import typing as t

import pytest
from django.db import connections

import analysis
import measurement
import producer
import runner
import stages
from django_absurd.queues import resolve_absurd_database
from tests.benchmarks import utils

# Every test here runs real `absurd_worker` children: separate processes on their own
# connections, which cannot see rows the test has not committed.
pytestmark = pytest.mark.django_db(transaction=True)

# Above the degenerate-window floor: below about fifty tasks the trimmed completion
# window collapses and every measurement reports zero throughput.
MEASURABLE_TASKS = "60"
# Three full profile slices and a deliberate leftover, derived from the slice size so
# it stays on that boundary if the size ever moves.
PROFILED_TASKS = 3 * analysis.THROUGHPUT_SLICE_COMPLETIONS + 100

# Two offer rates for one worker, either side of anything a machine's speed could
# move: the fastest single worker ever measured on the tuned server drained 360
# tasks/s, so 25 is absorbed everywhere and 800 diverges everywhere.
ABSORBABLE_RATE_PER_S = 25.0
UNABSORBABLE_RATE_PER_S = 800.0
# A drain ceiling whose lowest ramp probe — a tenth of it — is already the rate above,
# so a ramp anchored to it cannot absorb even its first offer.
UNABSORBABLE_CEILING_PER_S = UNABSORBABLE_RATE_PER_S / stages.RATE_RAMP_START_FRACTION
# Four times what the fleet drains, so its ramp climbs THROUGH the offer rate that same
# fleet absorbs rather than up to it. Rungs run from a tenth of a ceiling to
# three-quarters of it, which puts this ladder's bottom at two-fifths of the drain rate
# and its top at three times it, while the ramps measured here refused between 0.9 and
# 1.5 times it — the drain is trimmed over a window its own fleet started inside, so it
# reads a little low. Derived from a drain the machine performs rather than fixed,
# because absorbing a rung AND refusing one is what this test is about and no constant
# puts both a fast workstation and a slow shared runner inside that window. A rung
# refusing early only shortens the climb, which the assertions allow for.
RAMP_CEILING_OVER_DRAIN = 4.0
# Enough completions that the trimmed window the ceiling is read off is not a handful
# of them, and few enough that the drain costs about a second.
RAMP_DRAIN_TASKS = 400
# Two workers rather than one because only the ends of the climb are load-bearing and a
# lone worker holds neither reliably: half the rate each leaves the bottom rung
# absorbed while one of them stalls.
RAMP_WORKERS = 2
# Two offers for ONE producer thread, either side of what a round trip to Postgres
# allows it: an enqueue takes milliseconds, so two a second leave it idling and three
# thousand a second are beyond it however long it is given. The windows differ only in
# what they cost — the second one is 900 enqueues, and every one of them is late.
DELIVERABLE_OFFER = (2.0, 2.0)
UNDELIVERABLE_OFFER = (3000.0, 0.3)

# One phase's statement counters as `pg_stat_statements` hands them over, either side
# of it. Cumulative, so the figures per task are the difference: the claim and its
# nested update both move, the drain poll is a statement of somebody else's that did
# not, and the insert is one the earlier snapshot had never seen.
STATEMENTS_BEFORE = [
    {
        "queryid": 1,
        "query": "select * from absurd.claim_task($1, $2)",
        "toplevel": True,
        "calls": 100,
        "total_exec_ms": 50.0,
        "rows": 100,
    },
    {
        "queryid": 2,
        "query": "update absurd.t_bench set state = $1 where task_id = $2",
        "toplevel": False,
        "calls": 300,
        "total_exec_ms": 30.0,
        "rows": 300,
    },
    {
        "queryid": 3,
        "query": "select count(*) from absurd.t_bench where state = $1",
        "toplevel": True,
        "calls": 40,
        "total_exec_ms": 2.0,
        "rows": 40,
    },
]
STATEMENTS_AFTER = [
    {**STATEMENTS_BEFORE[0], "calls": 108, "total_exec_ms": 54.0, "rows": 108},
    {**STATEMENTS_BEFORE[1], "calls": 324, "total_exec_ms": 46.0, "rows": 324},
    STATEMENTS_BEFORE[2],
    {
        "queryid": 4,
        "query": "insert into absurd.r_bench ($1, $2)",
        "toplevel": True,
        "calls": 8,
        "total_exec_ms": 2.0,
        "rows": 16,
    },
]


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


def test_reports_the_latency_of_the_paced_offer_a_rate_stage_made(
    tmp_path: pathlib.Path,
) -> None:
    """A rate stage's numbers come from the trimmed middle of its offer window.

    So the run count is smaller than the offer by construction, and the fairness
    shares — grouped over those same rows — have to add back up to it. Percentiles are
    asserted as measured at all, never as fast: a rate is not something a test can
    demand.
    """
    size = [
        "--reps",
        "1",
        "--tasks",
        MEASURABLE_TASKS,
        "--duration",
        "1",
        # Nothing here reads the idle probes, which are what the default fleet of four
        # is spent on; the paced offer under test runs on one worker either way.
        "--max-workers",
        "1",
    ]
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


# Everything below builds its own measurement rather than driving a stage. The first
# three want a task that misbehaves — one that outlives its claim lease, one that never
# completes, one that never drains — and every workload the driver offers is a task
# that succeeds; the rest want one measurement at a size or an offer rate of their own —
# repeated, napped, too small to slice, big enough to slice, or paced past what the
# fleet can absorb — which the smallest stage would charge six of.
def test_rate_measurement_marks_an_offer_the_fleet_never_absorbed() -> None:
    """A queue still growing when the offer stopped never measured that rate.

    Its percentiles come off the middle of a transient, so they read as whatever the
    ramp had reached by then and rise with the window rather than describing the rate
    on the row. One worker cannot absorb hundreds of tasks a second, so the offer here
    diverges on any machine — while the paced arm of the same pair is absorbed on any
    machine too, which is what says the guard has not simply flagged everything.
    """
    absorbed, diverging = (
        measurement.run_measurement(
            measurement.MeasurementSpec(
                name=f"smoke-offer-{rate_per_s:g}",
                mode="rate",
                task_path="tasks.noop_sync",
                rate_per_s=rate_per_s,
                duration_s=2.0,
                workers=1,
                worker=runner.WorkerSpec(concurrency=1, poll_interval=0.05),
                reps=1,
                timeout_s=120,
            )
        )
        for rate_per_s in (ABSORBABLE_RATE_PER_S, UNABSORBABLE_RATE_PER_S)
    )

    assert {
        "absorbed": {
            "backlog_grew": absorbed["reps"][0]["backlog_grew"],
            "invalid": absorbed["invalid"],
        },
        "diverging": {
            "backlog_grew": diverging["reps"][0]["backlog_grew"],
            "invalid": diverging["invalid"],
            "backlog_at_the_end_outgrew_the_midpoint": (
                diverging["reps"][0]["backlog_end"]
                > diverging["reps"][0]["backlog_mid"]
            ),
        },
    } == {
        "absorbed": {"backlog_grew": False, "invalid": False},
        "diverging": {
            "backlog_grew": True,
            "invalid": True,
            "backlog_at_the_end_outgrew_the_midpoint": True,
        },
    }


def test_rate_ramp_that_absorbed_nothing_measures_at_the_lowest_rate_it_probed(
    tmp_path: pathlib.Path,
) -> None:
    """A ramp anchored to a rate no fleet here could absorb still names a rate.

    Called directly because the fictional drain ceiling is the input: the stage reads
    that number back off `stage_process_scaling.json`, and no run of the driver
    produces one a single worker is hopeless against.

    The stage measures at the lowest rate it probed rather than refusing — a marked
    rung is a finding, an aborted stage is not — and records that the rate was never
    absorbed, so nothing reads those rungs as latency at a sustainable offer.
    """
    ramp = stages.measure_sustainable_rate(
        runner.WorkerSpec(concurrency=1, poll_interval=0.05),
        1,
        UNABSORBABLE_CEILING_PER_S,
        stages.StageOptions(results_dir=tmp_path, duration_s=1.0),
    )

    lowest = UNABSORBABLE_CEILING_PER_S * stages.RATE_RAMP_START_FRACTION
    assert {
        "probed": [probe["rate_per_s"] for probe in ramp["probes"]],
        "absorbed": [probe["sustained"] for probe in ramp["probes"]],
        "backlog_grew": [probe["rep"]["backlog_grew"] for probe in ramp["probes"]],
        "rate_per_s": ramp["rate_per_s"],
        "sustained": ramp["sustained"],
        "bracket_high_per_s": ramp["bracket_high_per_s"],
        "drain_ceiling_per_s": ramp["drain_ceiling_per_s"],
        "offer_seconds": ramp["offer_seconds"],
    } == {
        "probed": [lowest],
        "absorbed": [False],
        "backlog_grew": [True],
        "rate_per_s": lowest,
        "sustained": False,
        "bracket_high_per_s": lowest,
        "drain_ceiling_per_s": UNABSORBABLE_CEILING_PER_S,
        "offer_seconds": 1.0,
    }


def test_rate_ramp_measures_at_the_highest_offer_it_absorbed(
    tmp_path: pathlib.Path,
) -> None:
    """The working point is the last rate that came off cleanly, not the one that
    stopped the climb.

    The ramp stops at the first refusal, so the refused rate is the one nearest to
    hand and measuring there offers the rungs below a rate this fleet has just been
    shown not to absorb. It is kept as the bracket instead: the knee is between the
    two, which is only a bracket at all because the two are different numbers.

    Called directly, and at a ceiling this machine measures for itself the way the
    stage reads one off `stage_process_scaling.json`: a ramp that absorbs or refuses
    everything it probes cannot show which end it measured at, and where a fleet's
    capacity lies is the machine's to decide.
    """
    worker = runner.WorkerSpec(concurrency=1, poll_interval=0.05)
    drained = measurement.run_clean_rep(
        measurement.MeasurementSpec(
            name="smoke-ramp-drain",
            mode="saturation",
            task_path="tasks.noop_sync",
            tasks=RAMP_DRAIN_TASKS,
            workers=RAMP_WORKERS,
            worker=worker,
            reps=1,
            timeout_s=120,
        )
    )

    ramp = stages.measure_sustainable_rate(
        worker,
        RAMP_WORKERS,
        drained["throughput_per_s"] * RAMP_CEILING_OVER_DRAIN,
        stages.StageOptions(results_dir=tmp_path, duration_s=2.0),
    )

    absorbed = [probe["rate_per_s"] for probe in ramp["probes"] if probe["sustained"]]
    refused = [
        probe["rate_per_s"] for probe in ramp["probes"] if not probe["sustained"]
    ]
    assert {
        "climbed_before_it_refused": len(absorbed) >= 1,
        "stopped_at_the_first_refusal": [probe["sustained"] for probe in ramp["probes"]]
        == [True] * len(absorbed) + [False],
    } == {"climbed_before_it_refused": True, "stopped_at_the_first_refusal": True}
    assert {
        "rate_per_s": ramp["rate_per_s"],
        "bracket_high_per_s": ramp["bracket_high_per_s"],
        "sustained": ramp["sustained"],
        "measured_below_the_offer_it_refused": (
            ramp["rate_per_s"] < ramp["bracket_high_per_s"]
        ),
    } == {
        "rate_per_s": max(absorbed),
        "bracket_high_per_s": min(refused),
        "sustained": True,
        "measured_below_the_offer_it_refused": True,
    }


def test_backlog_growth_is_read_as_a_share_of_the_offer_it_grew_under() -> None:
    """The rule the guard turns on, at the two boundaries no real offer can hold still.

    A share rather than a count, so one threshold covers a five-task smoke offer and a
    hundred-thousand-task one; and a floor in tasks under the share, so a handful of
    scheduling jitter on a tiny offer is not read as a diverging queue.
    """
    assert {
        "kept_up": analysis.build_backlog_growth(500, 1000, 480, 960),
        "fell_behind": analysis.build_backlog_growth(500, 1000, 480, 700),
        "jittered_under_the_floor": analysis.build_backlog_growth(50, 100, 48, 90),
    } == {
        "kept_up": {
            "backlog_mid": 20,
            "backlog_end": 40,
            "offered_after_midpoint": 500,
            "backlog_grew": False,
        },
        "fell_behind": {
            "backlog_mid": 20,
            "backlog_end": 300,
            "offered_after_midpoint": 500,
            "backlog_grew": True,
        },
        "jittered_under_the_floor": {
            "backlog_mid": 2,
            "backlog_end": 10,
            "offered_after_midpoint": 50,
            "backlog_grew": False,
        },
    }


def test_rate_producer_marks_an_offer_it_could_not_keep_up_with() -> None:
    """A rate the producer never delivered is not a rate the fleet was offered.

    The enqueue side runs on the same box as the workers, so a rung can fall over
    because the producer ran out of thread rather than because the fleet ran out of
    capacity — and the two want opposite conclusions. The offer carries its own
    verdict so a measurement reading it is refused rather than published as a fleet
    result, and so a report can attribute the refusal to the right side.

    Each arm is assembled into a rep the way `measurement.run_rate_rep` does, because
    the flag only means anything through the rules that mark a rep.
    """
    delivered, starved = (
        producer.run_rate_producer(
            "tasks.noop_sync", rate_per_s, offer_seconds, threads=1
        )
        for rate_per_s, offer_seconds in (DELIVERABLE_OFFER, UNDELIVERABLE_OFFER)
    )

    assert [
        {
            "offered": offer.offered,
            "offered_ok": offer.offered_ok,
            "fell_short_of_the_rate_it_aimed_at": (
                offer.achieved_rate_per_s < rate_per_s
            ),
            "missed_every_deadline_after_the_first": (
                offer.missed_deadline_count == offer.offered - 1
            ),
            "rep_invalid": measurement.is_rep_invalid(
                {"valid": True, **dataclasses.asdict(offer)}
            ),
        }
        for offer, (rate_per_s, _) in (
            (delivered, DELIVERABLE_OFFER),
            (starved, UNDELIVERABLE_OFFER),
        )
    ] == [
        {
            "offered": 4,
            "offered_ok": True,
            "fell_short_of_the_rate_it_aimed_at": False,
            "missed_every_deadline_after_the_first": False,
            "rep_invalid": False,
        },
        {
            "offered": 900,
            "offered_ok": False,
            "fell_short_of_the_rate_it_aimed_at": True,
            "missed_every_deadline_after_the_first": True,
            "rep_invalid": True,
        },
    ]


def test_saturation_measurement_invalidates_a_task_that_outlived_its_claim_lease() -> (
    None
):
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
    assert result["invalid"] is True


def test_saturation_measurement_invalidates_tasks_that_never_completed() -> None:
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
    assert result["invalid"] is True


def test_saturation_measurement_refuses_a_backlog_that_never_drained() -> None:
    """A backlog still moving when the clock runs out is refused, not recorded.

    Its metrics would be read off whatever had finished by then, which is a smaller
    measurement wearing the size it was asked for.
    """
    spec = measurement.MeasurementSpec(
        name="smoke-stalled",
        mode="saturation",
        task_path="tests.benchmarks.utils.sleep_past_claim_lease",
        tasks=1,
        workers=1,
        worker=runner.WorkerSpec(concurrency=1, poll_interval=0.05),
        reps=1,
        timeout_s=1.0,
    )

    with pytest.raises(measurement.MeasurementTimeoutError) as error_info:
        measurement.run_measurement(spec)

    assert str(error_info.value) == (
        "Measurement 'smoke-stalled' still had unfinished tasks after 1s. A "
        "measurement that never drains is refused rather than recorded: raise "
        "timeout_s, cut the task count, or find out why the workers stalled."
    )


def test_saturation_measurement_refuses_a_rep_the_host_slept_through() -> None:
    """A napped rep leaves nothing behind: no median, no dispersion, no endpoints.

    The drain phase is wall time, so a host that suspends mid-drain would publish a
    throughput measured over a window it was unconscious for. It is invalid rather
    than unstable: nothing disagreed, there was simply nothing left to compare.
    """
    spec = measurement.MeasurementSpec(
        name="smoke-napped",
        mode="saturation",
        task_path="tasks.noop_sync",
        tasks=int(MEASURABLE_TASKS),
        workers=1,
        worker=runner.WorkerSpec(concurrency=1, poll_interval=0.05),
        reps=1,
        timeout_s=60,
    )

    with utils.nap_the_wall_clock():
        result = measurement.run_measurement(spec)

    assert {
        "reps": [utils.normalize_measured_durations(rep) for rep in result["reps"]],
        "median": result["median"],
        "spread": result["spread"],
        "absolute_spread": result["absolute_spread"],
        "cv": result["cv"],
        "range_low": result["range_low"],
        "range_high": result["range_high"],
        "invalid": result["invalid"],
        "unstable": result["unstable"],
    } == {
        "reps": [
            {
                "valid": False,
                "error": (
                    "Wall clock advanced Ns over a phase the monotonic clock measured "
                    "at Ns: the host suspended or stalled mid-phase, so every number "
                    "this phase produced is fiction. Re-run the measurement on a "
                    "machine that stays awake."
                ),
                "load_before": True,
                "load_after": True,
            }
        ],
        "median": {},
        "spread": None,
        "absolute_spread": None,
        "cv": None,
        "range_low": None,
        "range_high": None,
        "invalid": True,
        "unstable": False,
    }


def test_saturation_measurement_records_every_dispersion_of_its_reps() -> None:
    """All four are recorded once there are reps to compare, never some of them.

    They say different things about the same reps: the CV is what instability is
    thresholded on, the absolute spread is the floor that keeps a tiny difference from
    tripping it, and the endpoints are what a report prints instead of a percentage.
    Asserted as measured at all, never as small: stability is not something a test can
    demand of a real worker.
    """
    spec = measurement.MeasurementSpec(
        name="smoke-repeated",
        mode="saturation",
        task_path="tasks.noop_sync",
        tasks=int(MEASURABLE_TASKS),
        workers=1,
        worker=runner.WorkerSpec(concurrency=1, poll_interval=0.05),
        reps=2,
        timeout_s=60,
    )

    result = measurement.run_measurement(spec)

    assert {
        "reps": len(result["reps"]),
        "measured_a_spread": isinstance(result["spread"], float),
        "measured_an_absolute_spread": isinstance(result["absolute_spread"], float),
        "measured_a_cv": isinstance(result["cv"], float),
        "endpoints_bracket_the_median": (
            result["range_low"]
            <= result["median"]["throughput_per_s"]
            <= result["range_high"]
        ),
    } == {
        "reps": 2,
        "measured_a_spread": True,
        "measured_an_absolute_spread": True,
        "measured_a_cv": True,
        "endpoints_bracket_the_median": True,
    }


def test_saturation_measurement_of_two_reps_reports_the_slower_one() -> None:
    """An even rep count has two middles, and the summary takes the unlucky one.

    Whichever way two real reps land, the measurement reports the LOWER throughput of
    the pair — asserted as the low endpoint rather than as a level, since which rep is
    faster is not something a test can arrange.
    """
    spec = measurement.MeasurementSpec(
        name="smoke-two-reps-saturation",
        mode="saturation",
        task_path="tasks.noop_sync",
        tasks=int(MEASURABLE_TASKS),
        workers=1,
        worker=runner.WorkerSpec(concurrency=1, poll_interval=0.05),
        reps=2,
        timeout_s=60,
    )

    result = measurement.run_measurement(spec)

    assert (result["median"]["throughput_per_s"] == result["range_low"]) is True


def test_rate_measurement_of_two_reps_reports_the_slower_one() -> None:
    """The same rule, on the metric that runs the other way.

    A rate measurement ranks on end-to-end p50, where LOW is the lucky rep, so the
    unlucky middle is the high endpoint — the opposite index from a saturation
    measurement, and taking the low one both times published the better rep.
    """
    spec = measurement.MeasurementSpec(
        name="smoke-two-reps-rate",
        mode="rate",
        task_path="tasks.noop_sync",
        rate_per_s=ABSORBABLE_RATE_PER_S,
        duration_s=2.0,
        workers=1,
        worker=runner.WorkerSpec(concurrency=1, poll_interval=0.05),
        reps=2,
        timeout_s=120,
    )

    result = measurement.run_measurement(spec)

    assert (result["median"]["end_to_end_p50_s"] == result["range_high"]) is True


def test_saturation_rep_records_what_its_tasks_cost_in_commits() -> None:
    """A task rate is a commit rate in disguise, and this is the exchange rate.

    Counted as the database's own `xact_commit` across the measured phase over the
    runs that completed inside it: the claim, the completion and the driver's drain
    polling, and nothing the preload did, which is over before the phase opens. A
    report multiplies it by throughput to say whether a measurement was bound by its
    client or by the disk's fsync.

    Asserted as measured, and as at least the one commit each completion has to make;
    never at a number, which is the finding.
    """
    spec = measurement.MeasurementSpec(
        name="smoke-commits",
        mode="saturation",
        task_path="tasks.noop_sync",
        tasks=int(MEASURABLE_TASKS),
        workers=1,
        worker=runner.WorkerSpec(concurrency=1, poll_interval=0.05),
        reps=1,
        timeout_s=60,
    )

    rep = measurement.run_measurement(spec)["reps"][0]

    assert {
        "n_runs": rep["n_runs"],
        "cost_at_least_a_commit_a_task": rep["commits_per_task"] >= 1.0,
    } == {"n_runs": int(MEASURABLE_TASKS), "cost_at_least_a_commit_a_task": True}


def test_saturation_measurement_samples_the_load_on_each_side_of_every_rep() -> None:
    """The host block is collected once every rep has run, so the load average in it
    counts the load the harness itself just made and can never answer whether the
    machine was otherwise busy. Each rep carries its own pair instead.

    Asserted as sampled, never at a level: what else the machine was doing is the
    finding, and an idle host legitimately reads 0.00.
    """
    spec = measurement.MeasurementSpec(
        name="smoke-load",
        mode="saturation",
        task_path="tasks.noop_sync",
        tasks=int(MEASURABLE_TASKS),
        workers=1,
        worker=runner.WorkerSpec(concurrency=1, poll_interval=0.05),
        reps=2,
        timeout_s=60,
    )

    result = measurement.run_measurement(spec)

    assert [
        {
            "load_before": rep["load_before"] >= 0.0,
            "load_after": rep["load_after"] >= 0.0,
        }
        for rep in result["reps"]
    ] == [{"load_before": True, "load_after": True}] * 2


def test_saturation_rep_profiles_its_throughput_across_the_drain() -> None:
    """A rep drains a full queue to empty, so one number averages a moving quantity.

    The profile is what separates a cost that rises with queue depth from one that
    differs rep to rep: slice 0 drained the fullest queue and the last slice the
    emptiest. Sized at three full slices plus a leftover, so the partial slice the
    drain ends on has to be dropped rather than divided — its 100 completions over
    the same instants would read as a rate of their own.

    Asserted as measured and ordered, never as fast or as flat: a real worker's shape
    is the finding, not something a test can demand.
    """
    spec = measurement.MeasurementSpec(
        name="smoke-profile",
        mode="saturation",
        task_path="tasks.noop_sync",
        tasks=PROFILED_TASKS,
        workers=1,
        worker=runner.WorkerSpec(concurrency=8, poll_interval=0.05),
        reps=1,
        timeout_s=90,
    )

    rep = measurement.run_measurement(spec)["reps"][0]

    assert {
        "n_runs": rep["n_runs"],
        "full_slices": len(rep["profile_slices"]),
        "every_slice_measured_a_rate": all(rate > 0 for rate in rep["profile_slices"]),
        "median_is_the_middle_slice": (
            rep["profile_median_per_s"] == sorted(rep["profile_slices"])[1]
        ),
        "measured_a_cv": rep["profile_cv"] > 0,
    } == {
        "n_runs": PROFILED_TASKS,
        "full_slices": 3,
        "every_slice_measured_a_rate": True,
        "median_is_the_middle_slice": True,
        "measured_a_cv": True,
    }


def test_saturation_rep_too_small_to_slice_records_no_profile() -> None:
    """Two slices are a line whatever the drain did, so no profile is recorded.

    A smoke-sized backlog would otherwise publish a shape read off a handful of
    completions, and nothing downstream could tell it from a measured one.
    """
    spec = measurement.MeasurementSpec(
        name="smoke-unprofilable",
        mode="saturation",
        task_path="tasks.noop_sync",
        tasks=int(MEASURABLE_TASKS),
        workers=1,
        worker=runner.WorkerSpec(concurrency=8, poll_interval=0.05),
        reps=1,
        timeout_s=60,
    )

    rep = measurement.run_measurement(spec)["reps"][0]

    assert {
        "n_runs": rep["n_runs"],
        "profile_slices": rep["profile_slices"],
        "profile_median_per_s": rep["profile_median_per_s"],
        "profile_cv": rep["profile_cv"],
    } == {
        "n_runs": int(MEASURABLE_TASKS),
        "profile_slices": None,
        "profile_median_per_s": None,
        "profile_cv": None,
    }


def test_commit_ceiling_probe_times_a_warmed_session_not_a_cold_one() -> None:
    """A session's commit rate climbs for its first thousand or so commits.

    Consecutive 400-commit rounds on one connection read 519, 544, 1276, 1899, 1912
    and 1914/s, so a probe that timed the start of that would report a third of the
    rate the same connection sustains. That is not a cosmetic error — the report
    divides each measurement's per-connection commit rate by this ceiling to call the
    measurement connection-bound or client-bound, so a cold ceiling inverts the verdict
    for every row it labels. The rounds before the warm-up budget are therefore run and
    thrown away, and the rounds after it are what the recorded median comes from.

    Asserted as a shape — every round's commits made, only the kept rounds' time
    divided — never at a rate, which is this machine's to decide. Five timed rounds out
    of nine put the honest ratio at 0.52-0.54, and a probe keeping no warm-up budget at
    0.95-1.32, so the 0.90 allowed here is the loosest bound that still refuses one.
    """
    timed_commits = analysis.PROBE_TIMED_ROUNDS * analysis.DURABLE_PROBE_COMMITS
    committed_before = analysis.read_xact_commit()
    started = time.perf_counter()
    ceiling = analysis.measure_commit_ceiling(durable=True)
    elapsed_s = time.perf_counter() - started
    # Cumulative statistics are flushed at most once a second, so without this the
    # probe's own commits are still in the backend's local buffer when it is asked.
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute("select pg_stat_force_next_flush()")
    committed = analysis.read_xact_commit() - committed_before
    # An unmeasured ceiling has no timed window at all, which the dict below reports as
    # the whole call rather than dividing by None.
    timed_s = timed_commits / ceiling["median_per_s"] if ceiling["valid"] else elapsed_s

    assert {
        "measured": ceiling["valid"],
        "committed_its_warm_up_rounds_too": (
            committed >= analysis.PROBE_WARM_UP_COMMITS + timed_commits
        ),
        "timed_only_the_rounds_it_kept": timed_s < 0.90 * elapsed_s,
    } == {
        "measured": True,
        "committed_its_warm_up_rounds_too": True,
        "timed_only_the_rounds_it_kept": True,
    }


def test_an_interrupted_commit_probe_leaves_the_session_usable() -> None:
    """A probe abandoned mid-wait must cost the process nothing but the probe.

    A signal delivered while psycopg waits on the socket leaves the session stuck
    mid-command, where every later statement reads `another command is already in
    progress`. Issuing the probe's own cleanup there raises over the interruption and
    hands that session on, so one interrupted probe becomes every later failure in the
    process — which is how a single 120 s test alarm took 57 unrelated tests down with
    it. The session is dropped instead, and the interruption travels on untouched.

    The alarm is the real mechanism rather than a simulation of it: `pytest-timeout`
    raises from a SIGALRM handler exactly like this, and a BaseException that is not a
    KeyboardInterrupt is what psycopg declines to cancel the query for.
    """
    with pytest.raises(utils.ProbeInterrupted), utils.interrupt_after(0.2):
        analysis.measure_commit_ceiling(durable=True)

    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute("select 1")
        answered = cursor.fetchone()
        # The probe's own table outlives the interruption, and a later probe on this
        # database would read a name it does not own as a refusal.
        cursor.execute(f"drop table if exists {analysis.COMMIT_PROBE_TABLE}")

    assert answered == (1,)


def test_saturation_measurement_refuses_a_fleet_that_died_mid_drain() -> None:
    """A queue polled alone cannot tell a slow drain from an absent fleet.

    The task ends its own worker, so nothing is left to claim and the backlog never
    moves again. Waiting that out would burn the whole timeout — 900 s for the
    saturation stages — and then report the timeout as the failure, with the crash that
    caused it printed underneath. The children's exit is what the measurement is
    refused on, and the crash is what it is refused with.
    """
    spec = measurement.MeasurementSpec(
        name="smoke-dead-fleet",
        mode="saturation",
        task_path="tests.benchmarks.utils.kill_the_worker_that_claimed_it",
        tasks=1,
        workers=1,
        worker=runner.WorkerSpec(concurrency=1, poll_interval=0.05),
        reps=1,
        timeout_s=120.0,
    )
    started = time.monotonic()

    with pytest.raises(RuntimeError) as error_info:
        measurement.run_measurement(spec)

    assert str(error_info.value).startswith(
        f"1 absurd_worker child(ren) exited before the measurement finished (codes "
        f"[{utils.WORKER_EXIT_CODE}]); it measured a worker count it never had. "
        f"Last output:"
    )
    assert str(error_info.value.__context__) == (
        "Measurement 'smoke-dead-fleet' lost 1 of its 1 worker process(es) while the "
        "queue was still draining, so what is left cannot drain it. Refused here "
        "rather than at the drain timeout, which the fleet would otherwise burn in "
        "full before anything said the workers were gone."
    )
    assert (time.monotonic() - started < 30.0) is True


def test_saturation_rep_records_no_statement_stats_where_nothing_counted_them() -> None:
    """The instrument that itemises a task's cost needs an extension this server lacks.

    `pg_stat_statements` only counts anything when the server was started with it in
    `shared_preload_libraries`, and the suite's `db` service must not be: it is shared
    by every suite, and changing what Postgres preloads changes what all of them run
    against. So the rep records no statement stats and measures everything else, which
    is also what a managed database whose role cannot create the extension gets.

    Both ways a server can count nothing degrade the same, and both are real here: the
    extension unreadable because it was never preloaded, and the name occupied by
    something that is not its view at all.
    """
    spec = measurement.MeasurementSpec(
        name="smoke-statements",
        mode="saturation",
        task_path="tasks.noop_sync",
        tasks=int(MEASURABLE_TASKS),
        workers=1,
        worker=runner.WorkerSpec(concurrency=1, poll_interval=0.05),
        reps=1,
        timeout_s=60,
    )

    rep = measurement.run_measurement(spec)["reps"][0]
    with utils.hold_the_statement_stats_name():
        while_the_name_is_taken = analysis.read_statement_stats()

    assert {
        "n_runs": rep["n_runs"],
        "statement_stats": rep["statement_stats"],
        "unreadable_view": analysis.read_statement_stats(),
        "occupied_name": while_the_name_is_taken,
    } == {
        "n_runs": int(MEASURABLE_TASKS),
        "statement_stats": None,
        "unreadable_view": None,
        "occupied_name": None,
    }


def test_statement_stats_bill_every_statement_of_a_phase_to_one_task() -> None:
    """A phase's statement counters, diffed and divided by the tasks that ran in it.

    Counters are cumulative and never reset — `pg_stat_statements_reset()` cannot be
    undone and would take out anything else reading the same server — so a phase is
    two snapshots subtracted. A statement whose counters did not move belongs to
    whatever else the server was doing and is dropped; one the earlier snapshot never
    saw counts from zero.

    Only the top-level rows sum into the server side: under `track=all` a PL/pgSQL
    function's own row already counts the time of every statement it ran inside itself,
    so adding the nested rows would bill Absurd's claim path twice. What is left of the
    wall clock is the client side — the harness's Python, the SDK's, and the round
    trips — which is the number that separates "Absurd's SQL is expensive" from "our
    Python is expensive".
    """
    stats = analysis.build_statement_stats(STATEMENTS_BEFORE, STATEMENTS_AFTER, 8, 0.5)

    assert stats == {
        "statements": [
            {
                "query": "update absurd.t_bench set state = $1 where task_id = $2",
                "toplevel": False,
                "calls_per_task": 3.0,
                "total_exec_ms_per_task": 2.0,
                "rows_per_task": 3.0,
            },
            {
                "query": "select * from absurd.claim_task($1, $2)",
                "toplevel": True,
                "calls_per_task": 1.0,
                "total_exec_ms_per_task": 0.5,
                "rows_per_task": 1.0,
            },
            {
                "query": "insert into absurd.r_bench ($1, $2)",
                "toplevel": True,
                "calls_per_task": 1.0,
                "total_exec_ms_per_task": 0.25,
                "rows_per_task": 2.0,
            },
        ],
        "wall_ms_per_task": 62.5,
        "server_exec_ms_per_task": 0.75,
        "client_ms_per_task": 61.75,
    }


def test_statement_stats_keep_only_the_costliest_statements_of_a_phase() -> None:
    """A results file has to stay readable, and the tail of the list is microseconds.

    Every statement the phase issued is still summed into the server side; what the cap
    drops is the itemisation of the cheapest, ranked on the server time each cost.
    """
    counted = range(analysis.STATEMENT_STATS_LIMIT + 2)
    before: list[dict[str, t.Any]] = [
        {
            "queryid": index,
            "query": f"select {index}",
            "toplevel": True,
            "calls": 0,
            "total_exec_ms": 0.0,
            "rows": 0,
        }
        for index in counted
    ]
    # One statement per millisecond of server time, so the rank is the queryid and the
    # server side sums to `sum(counted) / 8` whatever the cap kept.
    after = [
        {**row, "calls": 8, "total_exec_ms": float(index)}
        for index, row in enumerate(before)
    ]

    assert analysis.build_statement_stats(before, after, 8, 0.5) == {
        "statements": [
            {
                "query": f"select {index}",
                "toplevel": True,
                "calls_per_task": 1.0,
                "total_exec_ms_per_task": index / 8,
                "rows_per_task": 0.0,
            }
            for index in range(analysis.STATEMENT_STATS_LIMIT + 1, 1, -1)
        ],
        "wall_ms_per_task": 62.5,
        "server_exec_ms_per_task": sum(counted) / 8,
        "client_ms_per_task": 62.5 - sum(counted) / 8,
    }


def test_statement_stats_of_a_phase_there_is_nothing_to_divide() -> None:
    """Three ways the itemisation has no answer, none of them an error.

    A snapshot is missing on any server that counts no statements, and a phase that
    completed no runs has no task to divide by — the same guard `commits_per_task`
    already refuses on, since a per-task figure over zero tasks is not a figure.
    """
    assert {
        "no_snapshot_before": analysis.build_statement_stats(
            None, STATEMENTS_AFTER, 8, 0.5
        ),
        "no_snapshot_after": analysis.build_statement_stats(
            STATEMENTS_BEFORE, None, 8, 0.5
        ),
        "no_runs_to_divide_by": analysis.build_statement_stats(
            STATEMENTS_BEFORE, STATEMENTS_AFTER, 0, 0.5
        ),
    } == {
        "no_snapshot_before": None,
        "no_snapshot_after": None,
        "no_runs_to_divide_by": None,
    }
