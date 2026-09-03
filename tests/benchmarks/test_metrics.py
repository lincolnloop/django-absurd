import datetime as dt

import pytest

import analysis
from django_absurd.flush import truncate_queue_tables
from tests.benchmarks import utils

# Where the hand-timed rows below are placed. Arbitrary, and fixed: every metric is a
# difference between two of these columns, so only the offsets matter.
EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

# Completion second, queue wait, execution, and the worker that claimed it. The three
# latency families are deliberately three different distributions, and the completion
# seconds are a fourth, so reading a percentile off the wrong column cannot land on
# the right number by accident.
SATURATION_ROWS = (
    (0, 2.0, 0.1, "bench-0"),
    (1, 0.4, 0.2, "bench-0"),
    (2, 1.2, 0.3, "bench-0"),
    (3, 0.6, 0.4, "bench-0"),
    (4, 1.8, 0.5, "bench-0"),
    (5, 0.2, 0.6, "bench-0"),
    (6, 1.4, 0.7, "bench-0"),
    (7, 0.8, 0.8, "bench-1"),
    (8, 1.6, 0.9, "bench-1"),
    (9, 1.0, 1.0, "bench-1"),
    (10, 2.2, 1.1, "bench-1"),
)
# The one task that was redelivered, which puts a second run row in the table without
# putting a second completion in it.
REDELIVERED_COMPLETION_SECOND = 3

# Arrival second, queue wait, execution, and the worker that claimed it, offered over
# a hundred-second window. The two rows claimed by `bench-2` arrive outside the
# trimmed middle of it, and the three slowest rows complete outside it — so trimming
# on the wrong column keeps a different set of rows, not a different slice of one.
RATE_ROWS = (
    (5, 8.0, 5.0, "bench-2"),
    (15, 3.0, 10.0, "bench-0"),
    (25, 1.0, 12.0, "bench-0"),
    (35, 5.0, 8.0, "bench-0"),
    (45, 2.0, 11.0, "bench-0"),
    (55, 6.0, 7.0, "bench-0"),
    (65, 4.0, 36.0, "bench-1"),
    (75, 9.0, 28.0, "bench-1"),
    (85, 7.0, 30.0, "bench-1"),
    (95, 10.0, 30.0, "bench-2"),
)
RATE_WINDOW_SECONDS = 100.0


def test_saturation_metrics_are_the_columns_the_rows_were_timed_on() -> None:
    """Every number a drain reports, against rows whose every timestamp was chosen.

    Queue wait is `started_at - enqueue_at`, execution is `completed_at - started_at`
    and end-to-end is the pair of them, so a swapped column reads a different
    distribution; throughput is 0.8 tasks over the p10..p90 COMPLETION span, so a
    dropped trim factor or a span taken off the wrong column reads a different rate.
    """
    truncate_queue_tables("bench")
    for completed_second, queue_wait, execution, claimed_by in SATURATION_ROWS:
        completed_at = EPOCH + dt.timedelta(seconds=completed_second)
        started_at = completed_at - dt.timedelta(seconds=execution)
        utils.insert_hand_timed_task(
            "bench",
            started_at - dt.timedelta(seconds=queue_wait),
            started_at,
            completed_at,
            claimed_by,
            redelivered=completed_second == REDELIVERED_COMPLETION_SECOND,
        )

    assert analysis.analyze_saturation("bench") == {
        "n_tasks": 11,
        "n_runs": 11,
        # The failed first attempt of the redelivered task, which no state filters out.
        "total_runs": 12,
        "total_tasks": 11,
        "max_attempt": 2,
        "extra_runs": 1,
        "degenerate_window": False,
        # 0.8 * 11 completions over the 1 s..9 s trimmed completion span.
        "throughput_per_s": pytest.approx(1.1),
        "fairness": {"bench-0": 7, "bench-1": 4},
        "fairness_ratio": pytest.approx(4 / 7),
        "queue_wait_p50_s": pytest.approx(1.2),
        "queue_wait_p90_s": pytest.approx(2.0),
        "queue_wait_p99_s": pytest.approx(2.18),
        "execution_p50_s": pytest.approx(0.6),
        "execution_p90_s": pytest.approx(1.0),
        "execution_p99_s": pytest.approx(1.09),
        "end_to_end_p50_s": pytest.approx(2.0),
        "end_to_end_p90_s": pytest.approx(2.5),
        "end_to_end_p99_s": pytest.approx(3.22),
        # Eleven completions is a twentieth of one profile slice, so the drain has no
        # shape to report.
        "profile_slices": None,
        "profile_median_per_s": None,
        "profile_cv": None,
    }


def test_rate_metrics_trim_the_offer_window_on_when_a_task_arrived() -> None:
    """A rate experiment is defined by arrival, so its middle 80% is 80% of the offer.

    Trimmed on `enqueue_at`, the rows measured are the eight that ARRIVED between 10 s
    and 90 s; trimmed on completion instead it would be the six that FINISHED in that
    span, which is a different sample with different percentiles and a different
    count. The backlog either side of the midpoint is counted over the whole window,
    untrimmed, because it is a level rather than a rate.
    """
    truncate_queue_tables("bench")
    for enqueue_second, queue_wait, execution, claimed_by in RATE_ROWS:
        enqueue_at = EPOCH + dt.timedelta(seconds=enqueue_second)
        started_at = enqueue_at + dt.timedelta(seconds=queue_wait)
        utils.insert_hand_timed_task(
            "bench",
            enqueue_at,
            started_at,
            started_at + dt.timedelta(seconds=execution),
            claimed_by,
        )

    assert analysis.analyze_rate(
        "bench", EPOCH, EPOCH + dt.timedelta(seconds=RATE_WINDOW_SECONDS)
    ) == {
        "n_tasks": 8,
        "n_runs": 8,
        "total_runs": 8,
        "total_tasks": 8,
        "max_attempt": 1,
        "extra_runs": 0,
        "degenerate_window": False,
        # 0.8 * 8 completions over the 35 s..115 s trimmed completion span.
        "throughput_per_s": pytest.approx(0.08),
        "fairness": {"bench-0": 5, "bench-1": 3},
        "fairness_ratio": pytest.approx(3 / 5),
        "queue_wait_p50_s": pytest.approx(4.5),
        "queue_wait_p90_s": pytest.approx(7.6),
        "queue_wait_p99_s": pytest.approx(8.86),
        "execution_p50_s": pytest.approx(11.5),
        "execution_p90_s": pytest.approx(31.8),
        "execution_p99_s": pytest.approx(35.58),
        "end_to_end_p50_s": pytest.approx(13.0),
        "end_to_end_p90_s": pytest.approx(37.9),
        "end_to_end_p99_s": pytest.approx(39.79),
        # Five arrivals against four completions at the midpoint, ten against six at
        # the end.
        "backlog_mid": 1,
        "backlog_end": 4,
        "offered_after_midpoint": 5,
        "backlog_grew": False,
    }
