import datetime as dt
import time
import typing as t

import psycopg.sql
from django.db import connections

from django_absurd.queues import resolve_absurd_database

# Below this the p10..p90 completion window is too short to divide by.
MIN_TRIMMED_SPAN_S = 0.01

LATENCY_KEYS = (
    "queue_wait_p50_s",
    "queue_wait_p90_s",
    "queue_wait_p99_s",
    "execution_p50_s",
    "execution_p90_s",
    "execution_p99_s",
    "end_to_end_p50_s",
    "end_to_end_p90_s",
    "end_to_end_p99_s",
)

# Every timing below is a Postgres column read on one clock: the driver contributes no
# timestamps of its own, so producer and workers cannot disagree.
COMPLETED_RUNS_SQL = """
select
  count(distinct r.task_id),
  count(*),
  percentile_cont(0.10) within group (order by extract(epoch from r.completed_at)),
  percentile_cont(0.90) within group (order by extract(epoch from r.completed_at)),
  percentile_cont(0.50) within group (
    order by extract(epoch from (r.started_at - t.enqueue_at))),
  percentile_cont(0.90) within group (
    order by extract(epoch from (r.started_at - t.enqueue_at))),
  percentile_cont(0.99) within group (
    order by extract(epoch from (r.started_at - t.enqueue_at))),
  percentile_cont(0.50) within group (
    order by extract(epoch from (r.completed_at - r.started_at))),
  percentile_cont(0.90) within group (
    order by extract(epoch from (r.completed_at - r.started_at))),
  percentile_cont(0.99) within group (
    order by extract(epoch from (r.completed_at - r.started_at))),
  percentile_cont(0.50) within group (
    order by extract(epoch from (r.completed_at - t.enqueue_at))),
  percentile_cont(0.90) within group (
    order by extract(epoch from (r.completed_at - t.enqueue_at))),
  percentile_cont(0.99) within group (
    order by extract(epoch from (r.completed_at - t.enqueue_at)))
from {runs} r
join {tasks} t on t.task_id = r.task_id
where r.state = 'completed' and {window}
"""

FAIRNESS_SQL = """
select r.claimed_by, count(*)
from {runs} r
join {tasks} t on t.task_id = r.task_id
where r.state = 'completed' and {window}
group by r.claimed_by
"""

# Deliberately UNFILTERED by state. Absurd's fail_run marks the failed run 'failed'
# and inserts a NEW row for the retry, so a completed-only count can never exceed one
# run per task and would report every redelivery as zero.
RUN_TOTALS_SQL = """
select count(*), count(distinct r.task_id), coalesce(max(r.attempt), 1)
from {runs} r
join {tasks} t on t.task_id = r.task_id
where {window}
"""


def analyze_saturation(queue: str = "bench") -> dict[str, t.Any]:
    """Every metric of one saturation rep, in a single round trip."""
    return read_completed_run_metrics(queue, psycopg.sql.SQL("true"))


def analyze_rate(
    queue: str, window_start: dt.datetime, window_end: dt.datetime
) -> dict[str, t.Any]:
    """Same metrics over the middle 80% of the offer window.

    In rate mode ARRIVAL defines the experiment, so the ramp and tail are trimmed on
    ``enqueue_at`` rather than on completion.
    """
    span = (window_end - window_start) / 10
    window = psycopg.sql.SQL("t.enqueue_at between {low} and {high}").format(
        low=psycopg.sql.Literal(window_start + span),
        high=psycopg.sql.Literal(window_end - span),
    )
    return read_completed_run_metrics(queue, window)


def count_unfinished_tasks(queue: str = "bench") -> int:
    statement = psycopg.sql.SQL(
        "select count(*) from {tasks} "
        "where state not in ('completed', 'failed', 'cancelled')"
    ).format(tasks=psycopg.sql.Identifier("absurd", f"t_{queue}"))
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute(statement)
        return int(cursor.fetchone()[0])


def capture_database_now() -> dt.datetime:
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute("select now()")
        return t.cast("dt.datetime", cursor.fetchone()[0])


def measure_idle_commit_rate(seconds: float) -> float:
    """Commits/s on this database while nothing but idle worker polls runs.

    Each idle claim poll is exactly one autocommit transaction here, so the delta is
    the polling tax; the driver deliberately issues no query inside the window.
    """
    before = read_xact_commit()
    time.sleep(seconds)
    return (read_xact_commit() - before) / seconds


def read_completed_run_metrics(
    queue: str, window: psycopg.sql.Composable
) -> dict[str, t.Any]:
    runs = psycopg.sql.Identifier("absurd", f"r_{queue}")
    tasks = psycopg.sql.Identifier("absurd", f"t_{queue}")
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute(
            psycopg.sql.SQL(COMPLETED_RUNS_SQL).format(
                runs=runs, tasks=tasks, window=window
            )
        )
        row = cursor.fetchone()
        cursor.execute(
            psycopg.sql.SQL(RUN_TOTALS_SQL).format(
                runs=runs, tasks=tasks, window=window
            )
        )
        totals = cursor.fetchone()
        cursor.execute(
            psycopg.sql.SQL(FAIRNESS_SQL).format(runs=runs, tasks=tasks, window=window)
        )
        fairness = {name: int(count) for name, count in cursor.fetchall()}
    return build_metrics(row, totals, fairness)


def build_metrics(
    row: tuple[t.Any, ...], totals: tuple[t.Any, ...], fairness: dict[str, int]
) -> dict[str, t.Any]:
    n_tasks, n_runs, completed_p10, completed_p90 = row[:4]
    total_runs, total_tasks, max_attempt = totals
    span = (completed_p90 - completed_p10) if completed_p10 is not None else 0.0
    # Too short a trimmed window makes 0.8*n/span an arbitrarily large number rather
    # than a rate, so it is refused outright instead of published.
    degenerate = span < MIN_TRIMMED_SPAN_S or not n_runs
    shares = sorted(fairness.values())
    metrics: dict[str, t.Any] = {
        "n_tasks": int(n_tasks),
        "n_runs": int(n_runs),
        "total_runs": int(total_runs),
        "total_tasks": int(total_tasks),
        "max_attempt": int(max_attempt),
        # More run rows than tasks means a redelivery: a retry after a failure or an
        # expired claim lease. Both pollute a cell, neither is visible in wall time.
        "extra_runs": int(total_runs) - int(total_tasks),
        "degenerate_window": degenerate,
        # 0.8 * n over the p10..p90 completion span: the trimmed window absorbs worker
        # start stagger and the driver's last drain poll.
        "throughput_per_s": 0.0 if degenerate else 0.8 * n_runs / span,
        "fairness": fairness,
        "fairness_ratio": (shares[0] / shares[-1]) if shares else 0.0,
    }
    metrics.update(
        dict(zip(LATENCY_KEYS, [value or 0.0 for value in row[4:]], strict=True))
    )
    return metrics


def read_xact_commit() -> int:
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute(
            "select xact_commit from pg_stat_database "
            "where datname = current_database()"
        )
        return int(cursor.fetchone()[0])
