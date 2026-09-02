import datetime as dt
import statistics
import time
import typing as t

import psycopg.sql
from django import db
from django.db import connections

from django_absurd.queues import resolve_absurd_database

# Below this the p10..p90 completion window is too short to divide by.
MIN_TRIMMED_SPAN_S = 0.01

# The table the commit-ceiling probe commits into. A REAL table, not a temporary one:
# a temp table is not WAL-logged, so its commits never reach the disk and the probe
# would report the machine's memory speed as its durable ceiling (84,000/s against
# 953/s measured side by side on the same server).
COMMIT_PROBE_TABLE = "benchmark_commit_ceiling_probe"
# ~0.1 s at the ~1,900 commits/s a quiet SSD sustains, and the probe runs three times
# a run — never during a measurement.
DURABLE_PROBE_COMMITS = 200
# The same window's worth with the fsync taken out: at ~400,000 commits/s the durable
# count would be timing half a millisecond.
NONDURABLE_PROBE_COMMITS = 5000

# Completions per profile slice. Equal-COUNT slices rather than equal-time ones: a slow
# measurement puts fewer completions into a fixed time slice, so its slices would read
# as noisier for purely statistical reasons and would not compare across settings.
THROUGHPUT_SLICE_COMPLETIONS = 200
# Two points are a line whatever the drain did, so below three the shape is invented.
MIN_PROFILE_SLICES = 3

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


# `extract(epoch ...)` is numeric from Postgres 14 on, which reaches Python as a Decimal
# that will not divide against the floats every other rate here is made of; the other
# queries dodge it only because percentile_cont has no numeric form to resolve to.
#
# The having clause drops the two slices that have no rate to report: the last one,
# which holds whatever completions were left over, and any whose completions all landed
# on one instant, which is what would otherwise divide by zero.
THROUGHPUT_PROFILE_SQL = """
select count(*), min(ts), max(ts)
from (
  select extract(epoch from r.completed_at)::float8 as ts,
         (row_number() over (order by r.completed_at) - 1) / {size} as bucket
  from {runs} r
  join {tasks} t on t.task_id = r.task_id
  where r.state = 'completed' and {window}
) s
group by bucket
having count(*) = {size} and max(ts) > min(ts)
order by bucket
"""

# Timed and looped SERVER-side: a client loop measures its own round trip plus the
# fsync, and the constant wanted here is the disk's alone. A procedural block rather
# than a function — `commit` inside a plpgsql FUNCTION called from a query raises
# `invalid transaction termination` — and the elapsed time comes back through the
# probe's own table because a DO block returns nothing.
COMMIT_PROBE_SQL = """
do $$
declare
  started timestamptz := clock_timestamp();
begin
  for i in 1..{commits} loop
    insert into {table} (n) values (i);
    commit;
  end loop;
  insert into {table} (elapsed_s)
  values (extract(epoch from clock_timestamp() - started));
end $$;
"""


def analyze_saturation(queue: str = "bench") -> dict[str, t.Any]:
    """Every metric of one saturation rep, plus how it varied during the drain."""
    window = psycopg.sql.SQL("true")
    return {
        **read_completed_run_metrics(queue, window),
        **read_throughput_profile(queue, window),
    }


def analyze_rate(
    queue: str, window_start: dt.datetime, window_end: dt.datetime
) -> dict[str, t.Any]:
    """Same metrics over the middle 80% of the offer window.

    In rate mode ARRIVAL defines the experiment, so the ramp and tail are trimmed on
    ``enqueue_at`` rather than on completion. No within-run profile either: the offer
    rate is imposed rather than discovered, so slicing it would plot the producer's
    pacing back at the reader.
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


def count_client_backends() -> int:
    """Client backends on this database other than the connection doing the asking.

    Counted rather than matched on `application_name`: neither the worker children nor
    the harness sets one, so the only thing that separates a fleet's backends from
    everything else already connected is a count taken on each side of starting it.
    """
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute(
            "select count(*) from pg_stat_activity "
            "where datname = current_database() "
            "and backend_type = 'client backend' and pid <> pg_backend_pid()"
        )
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


def measure_commit_ceiling(*, durable: bool) -> float | None:
    """Commits/s this server sustains in one session, or ``None`` if it refused.

    The number every throughput in a run has to be read against: 71% of a run's active
    backend time is WAL durability, so a task rate near this ceiling is a property of
    the disk rather than of Absurd, and the same code on another machine reports a
    different one.

    Calibration, not measurement — a run that cannot have its ceiling records that it
    has none and carries on, because an uncalibrated number that says so beats no run.
    """
    commits = DURABLE_PROBE_COMMITS if durable else NONDURABLE_PROBE_COMMITS
    table = psycopg.sql.Identifier(COMMIT_PROBE_TABLE)
    try:
        with connections[resolve_absurd_database()].cursor() as cursor:
            # Outside the try/finally on purpose: a name already taken belongs to
            # someone else, and the cleanup below must never drop a table this probe
            # did not create.
            cursor.execute(
                psycopg.sql.SQL(
                    "create table {table} (n int, elapsed_s float8)"
                ).format(table=table)
            )
            try:
                if not durable:
                    cursor.execute("set synchronous_commit = off")
                cursor.execute(
                    psycopg.sql.SQL(COMMIT_PROBE_SQL).format(
                        commits=psycopg.sql.Literal(commits), table=table
                    )
                )
                cursor.execute(
                    psycopg.sql.SQL("select max(elapsed_s) from {table}").format(
                        table=table
                    )
                )
                return commits / float(cursor.fetchone()[0])
            finally:
                # Reset unconditionally: it is a no-op for the durable probe, and a
                # branch here would be one more way to leave the session altered.
                cursor.execute("reset synchronous_commit")
                cursor.execute(
                    psycopg.sql.SQL("drop table {table}").format(table=table)
                )
    except db.Error:
        return None


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


def read_throughput_profile(
    queue: str, window: psycopg.sql.Composable
) -> dict[str, t.Any]:
    """Throughput over successive equal-count slices of one drain, oldest first.

    A saturation rep drains a full queue to empty, so its single throughput number
    averages whatever the per-task cost did as depth fell. Slice 0 is the fullest
    queue and the last slice the emptiest, which is what tells a rising cost apart
    from a rep-to-rep one.
    """
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute(
            psycopg.sql.SQL(THROUGHPUT_PROFILE_SQL).format(
                runs=psycopg.sql.Identifier("absurd", f"r_{queue}"),
                tasks=psycopg.sql.Identifier("absurd", f"t_{queue}"),
                window=window,
                size=psycopg.sql.Literal(THROUGHPUT_SLICE_COMPLETIONS),
            )
        )
        return build_throughput_profile(cursor.fetchall())


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
        # expired claim lease. Both pollute a measurement; neither shows in wall time.
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


def build_throughput_profile(rows: list[tuple[t.Any, ...]]) -> dict[str, t.Any]:
    slices = [count / (last - first) for count, first, last in rows]
    if len(slices) < MIN_PROFILE_SLICES:
        return {
            "profile_slices": None,
            "profile_median_per_s": None,
            "profile_cv": None,
        }
    return {
        "profile_slices": slices,
        "profile_median_per_s": statistics.median(slices),
        "profile_cv": statistics.stdev(slices) / statistics.fmean(slices),
    }


def read_xact_commit() -> int:
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute(
            "select xact_commit from pg_stat_database "
            "where datname = current_database()"
        )
        return int(cursor.fetchone()[0])
