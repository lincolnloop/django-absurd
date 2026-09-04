import datetime as dt
import json
import math
import statistics
import time
import typing as t

import psycopg.sql
from django import db
from django.db import connections

from django_absurd.queues import resolve_absurd_database

# Below this the p10..p90 completion window is too short to divide by.
MIN_TRIMMED_SPAN_S = 0.01

# Backlog growth over the back half of an offer window, as a share of the tasks
# offered into it, above which the queue never reached steady state. In steady state
# the backlog is one Little's-law-worth of in-flight work at each end and the growth
# cancels to a fraction of a percent; a fleet absorbing 95% of its offer reads 5%.
BACKLOG_GROWTH_LIMIT = 0.05
# Tasks of growth under which the share above is not consulted, so a handful of
# scheduling jitter on a tiny offer is not read as a diverging queue.
BACKLOG_GROWTH_FLOOR_TASKS = 8

# The table the commit-ceiling probe commits into. A REAL table, not a temporary one:
# a temp table is not WAL-logged, so the probe would report memory speed as durable.
COMMIT_PROBE_TABLE = "benchmark_commit_ceiling_probe"
# Commits per timed round. Sized on the volume, where ~2,000 commits/s made this a
# 0.15 s round; on tmpfs a warmed session commits 204,000-241,000/s, so the same round
# times 1.2-1.5 ms — the regime the count below exists to stay out of. Unchanged
# anyway: every ceiling in `CLAUDE.md` was measured at 300, and re-sizing it makes none
# of them comparable.
DURABLE_PROBE_COMMITS = 300
# The same window with fsync out of the way, where the durable count above would
# be timing under a millisecond.
NONDURABLE_PROBE_COMMITS = 5000
# Commits a session must already have made before its rate settles; every round before
# this one is thrown away.
PROBE_WARM_UP_COMMITS = 1200
# Timed rounds kept. The durable rate is a distribution and not a constant, so the
# ceiling is recorded as a median with its own dispersion beside it.
PROBE_TIMED_ROUNDS = 5
# What a ceiling block holds until a probe replaces it, so a run cut short — killed,
# interrupted mid-wait, or dead at a stage — records that it never took one. A server
# that refused a probe is a different fact and gets a different block.
UNPROBED_COMMIT_CEILING = {
    "valid": False,
    "error": "the run ended before this probe was taken",
}

# Completions per profile slice. Equal-COUNT rather than equal-time, so a slow
# measurement's slices are not noisier for purely statistical reasons.
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

# Every timing is a Postgres column on one clock, so producer and workers cannot
# disagree; the driver contributes no timestamps of its own.
COMPLETED_RUNS_SQL = """
select
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

# UNFILTERED by state, since `fail_run` inserts a NEW row for the retry and a
# completed-only count would report every redelivery as zero — `n_tasks` excepted.
RUN_TOTALS_SQL = """
select
  count(*),
  count(distinct r.task_id),
  coalesce(max(r.attempt), 1),
  count(distinct r.task_id) filter (where r.state = 'completed')
from {runs} r
join {tasks} t on t.task_id = r.task_id
where {window}
"""


# A paced rep's backlog at the midpoint of its offer window and at the end of it:
# enqueued minus completed, off the same columns every other metric comes from, so
# the growth between them is exact rather than sampled while the offer ran.
BACKLOG_SQL = """
select
  (select count(*) from {tasks} t where t.enqueue_at <= {mid}),
  (select count(*) from {tasks} t where t.enqueue_at <= {high}),
  (select count(*) from {runs} r
     where r.state = 'completed' and r.completed_at <= {mid}),
  (select count(*) from {runs} r
     where r.state = 'completed' and r.completed_at <= {high})
"""

# `extract(epoch ...)` is numeric from Postgres 14 on, which reaches Python as a
# Decimal that will not divide against floats; the `::float8` is what stops that.
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

# Timed server-side: a client loop would measure its own round trips too. A DO block,
# not a function — `commit` inside plpgsql raises `invalid transaction termination`.
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

# Extension and view share the one name, because the extension names its view
# after itself.
STATEMENT_STATS_EXTENSION = "pg_stat_statements"

# Statements kept per rep, ranked by server time: past the top few the entries are
# microsecond bookkeeping and the results file stops being readable.
STATEMENT_STATS_LIMIT = 15

# The counters a statement the earlier snapshot never saw is diffed against.
UNSEEN_STATEMENT = {"calls": 0, "total_exec_ms": 0.0, "rows": 0}

# Read through a function of the harness's own so that a server missing the extension
# and one missing the preloaded library are both classified server-side, in one path.
STATEMENT_STATS_READER = "benchmark_read_statement_stats"
# Dropped before it is created, because `create or replace` REFUSES a changed return
# type, and `text` rather than `jsonb` because psycopg3 hands jsonb back undecoded.
STATEMENT_STATS_READER_SQL = """
create or replace function {reader}() returns text language plpgsql as $$
begin
  return (
    select coalesce(
             jsonb_agg(jsonb_build_object(
               'queryid', queryid,
               'query', query,
               'toplevel', toplevel,
               'calls', calls,
               'total_exec_ms', total_exec_time,
               'rows', rows)),
             '[]'::jsonb)::text
    from {stats}
    where dbid = (select oid from pg_database where datname = current_database())
      -- Two patterns, because reading the instrument is billed too: this query names
      -- the view, while the drop and create that installed it name the function.
      and query not like {stats_text}
      and query not like {reader_text}
  );
exception
  -- A null DOCUMENT rather than SQL NULL, so the caller always parses one JSON string.
  when undefined_table or object_not_in_prerequisite_state then return 'null';
end $$;
"""


# Live rows counted, dead rows as the statistics collector last estimated them, and a
# size that is exact. Counted rather than read off `pg_stat_get_live_tuples`, which an
# ANALYZE sets to the truth and any backend still holding unflushed insert counters
# then adds its arrears to — measured at 285 live tuples on a table holding 240 rows,
# one preload thread's 45 landing after the ANALYZE. Dead rows have no exact source.
TABLE_STATE_SQL = """
select
  (select count(*) from {table}),
  pg_stat_get_dead_tuples(oid),
  pg_total_relation_size(oid)
from pg_class
where oid = %s::regclass
"""


def analyze_saturation(
    queue: str, since: dt.datetime | None, completed_since: dt.datetime
) -> dict[str, t.Any]:
    """Every metric of one saturation rep, plus how it varied during the drain.

    ``since`` is what keeps a rep that laid ballast from measuring it: the ballast is
    enqueued and drained before that mark and the measured tasks after it.

    ``completed_since`` is where the rep starts counting commits, and a run that
    finished ahead of it was paid for by commits nobody counted — so the rates and the
    percentiles are read over that interval and the totals over the whole drain.
    Filtering it on ``enqueue_at`` would select nothing at all: a saturation rep
    enqueues every task before its fleet exists. The profile keeps the whole drain —
    its slices are equal-count and trimmed already, so a second window moves the
    boundaries and nothing else.
    """
    drained = (
        psycopg.sql.SQL("true")
        if since is None
        else psycopg.sql.SQL("t.enqueue_at > {since}").format(
            since=psycopg.sql.Literal(since)
        )
    )
    measured = psycopg.sql.SQL("{drained} and r.completed_at > {mark}").format(
        drained=drained, mark=psycopg.sql.Literal(completed_since)
    )
    return {
        **read_completed_run_metrics(queue, measured, drained),
        **read_throughput_profile(queue, drained),
    }


def analyze_rate(
    queue: str, window_start: dt.datetime, window_end: dt.datetime
) -> dict[str, t.Any]:
    """Same metrics over the middle 80% of the offer window, and whether it kept up.

    Trimmed on ``enqueue_at`` rather than on completion because arrival is what defines
    a rate experiment, and carrying no profile because an imposed offer rate has no
    shape to discover.

    One window does for both the rates and the totals: the offer starts once the fleet
    is already up, so there is no start-up interval for the totals to reach back over.
    """
    span = (window_end - window_start) / 10
    window = psycopg.sql.SQL("t.enqueue_at between {low} and {high}").format(
        low=psycopg.sql.Literal(window_start + span),
        high=psycopg.sql.Literal(window_end - span),
    )
    return {
        **read_completed_run_metrics(queue, window, window),
        **read_backlog_growth(queue, window_start, window_end),
    }


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

    Counted rather than matched on `application_name`, which nothing here sets, so a
    fleet's backends are only separable as a delta across starting it.
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

    Each idle claim poll is one autocommit transaction, so the delta is the polling
    tax; the driver issues no query of its own inside the window.
    """
    before = read_xact_commit()
    time.sleep(seconds)
    return (read_xact_commit() - before) / seconds


def measure_commit_ceiling(*, durable: bool) -> dict[str, t.Any]:
    """What one WARM session commits per second, as a distribution, or why it is not.

    The ceiling every throughput in a run is read against: near it, a per-connection
    task rate is a property of Postgres on this disk rather than of Absurd.

    A median and its spread rather than a scalar, because the durable rate is a wide
    distribution nothing here selects within, and the endpoints are what a report's
    bound-verdict widens on. ONE session, because a worker opens one claim connection
    whatever its ``--concurrency`` — see `benchmarks/CLAUDE.md`.

    Calibration, not measurement: a run whose server refuses the probe records the
    refusal in a rep's own ``valid``/``error`` vocabulary and carries on.
    """
    commits = DURABLE_PROBE_COMMITS if durable else NONDURABLE_PROBE_COMMITS
    table = psycopg.sql.Identifier(COMMIT_PROBE_TABLE)
    probe = psycopg.sql.SQL(COMMIT_PROBE_SQL).format(
        commits=psycopg.sql.Literal(commits), table=table
    )
    warm_up_rounds = math.ceil(PROBE_WARM_UP_COMMITS / commits)
    connection = connections[resolve_absurd_database()]
    try:
        with connection.cursor() as cursor:
            # A name already taken belongs to someone else, and the cleanup below must
            # never drop a table it did not create.
            cursor.execute(
                psycopg.sql.SQL(
                    "create table {table} (n int, elapsed_s float8)"
                ).format(table=table)
            )
            if not durable:
                cursor.execute("set synchronous_commit = off")
            rates = []
            for _ in range(warm_up_rounds + PROBE_TIMED_ROUNDS):
                # Emptied first so the row read back is this round's own elapsed
                # and never a slower earlier round's.
                cursor.execute(psycopg.sql.SQL("truncate {table}").format(table=table))
                cursor.execute(probe)
                cursor.execute(
                    psycopg.sql.SQL("select max(elapsed_s) from {table}").format(
                        table=table
                    )
                )
                rates.append(commits / float(cursor.fetchone()[0]))
            # Cleanup on the way out of a probe that finished, never from a `finally`:
            # a statement issued after an interrupted wait raises over the interruption
            # and hands the rest of the process a session stuck mid-command.
            cursor.execute("reset synchronous_commit")
            cursor.execute(psycopg.sql.SQL("drop table {table}").format(table=table))
    except db.Error as exc:
        return describe_refused_probe(exc)
    except BaseException:
        # An interrupted wait leaves the session mid-command, where every later
        # statement reads `another command is already in progress`. Nothing can be
        # issued on it, so it is dropped rather than reset, and the interruption goes
        # on untouched.
        connection.close()
        raise
    # Warmed on that session itself: the climb is per connection, so a session warmed
    # anywhere else leaves this one paying for it.
    return summarize_commit_rates(rates[warm_up_rounds:])


def describe_refused_probe(exc: db.Error) -> dict[str, t.Any]:
    """A probe the server was asked for and would not give, and what it said.

    Distinct from `UNPROBED_COMMIT_CEILING`: one is a server that answered no, the
    other is a run that never asked, and a reader deciding whether to trust a rate
    needs to tell them apart.
    """
    return {"valid": False, "error": f"the server refused the probe: {exc}"}


def summarize_commit_rates(rates: list[float]) -> dict[str, t.Any]:
    """One probe's timed rounds, in the vocabulary a measurement's reps already use.

    The endpoints ride along with the CV because a percentage is what a reader has to
    un-reduce to see what was measured.
    """
    return {
        "valid": True,
        "median_per_s": statistics.median(rates),
        "cv": statistics.stdev(rates) / statistics.fmean(rates),
        "range_low": min(rates),
        "range_high": max(rates),
    }


def read_completed_run_metrics(
    queue: str, measured: psycopg.sql.Composable, drained: psycopg.sql.Composable
) -> dict[str, t.Any]:
    """Percentiles, throughput and fairness over ``measured``; totals over ``drained``.

    Two windows because they answer different questions. A rate is only a rate over the
    interval it was taken in, while a redelivery and a task nobody completed are facts
    about the whole drain that have to stay countable outside that interval.
    """
    runs = psycopg.sql.Identifier("absurd", f"r_{queue}")
    tasks = psycopg.sql.Identifier("absurd", f"t_{queue}")
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute(
            psycopg.sql.SQL(COMPLETED_RUNS_SQL).format(
                runs=runs, tasks=tasks, window=measured
            )
        )
        row = cursor.fetchone()
        cursor.execute(
            psycopg.sql.SQL(RUN_TOTALS_SQL).format(
                runs=runs, tasks=tasks, window=drained
            )
        )
        totals = cursor.fetchone()
        cursor.execute(
            psycopg.sql.SQL(FAIRNESS_SQL).format(
                runs=runs, tasks=tasks, window=measured
            )
        )
        fairness = {name: int(count) for name, count in cursor.fetchall()}
    return build_metrics(row, totals, fairness)


def read_backlog_growth(
    queue: str, window_start: dt.datetime, window_end: dt.datetime
) -> dict[str, t.Any]:
    """Whether the fleet kept up with the offer, or was still falling behind at the end.

    A paced measurement's percentiles only describe the rate they were taken at if the
    queue reached steady state. A backlog still growing when the offer stopped means
    the fleet never absorbed that rate, so its p50 is a snapshot of a transient and
    rises with the window length rather than describing anything.
    """
    mid = window_start + (window_end - window_start) / 2
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute(
            psycopg.sql.SQL(BACKLOG_SQL).format(
                runs=psycopg.sql.Identifier("absurd", f"r_{queue}"),
                tasks=psycopg.sql.Identifier("absurd", f"t_{queue}"),
                mid=psycopg.sql.Literal(mid),
                high=psycopg.sql.Literal(window_end),
            )
        )
        enqueued_mid, enqueued_end, completed_mid, completed_end = cursor.fetchone()
    return build_backlog_growth(
        int(enqueued_mid), int(enqueued_end), int(completed_mid), int(completed_end)
    )


def build_backlog_growth(
    enqueued_mid: int, enqueued_end: int, completed_mid: int, completed_end: int
) -> dict[str, t.Any]:
    """The two backlogs, and the verdict read off the growth between them.

    Judged as a SHARE of what was offered in between rather than as a count, so one
    threshold covers a five-task smoke offer and a hundred-thousand-task one.
    """
    backlog_mid = enqueued_mid - completed_mid
    backlog_end = enqueued_end - completed_end
    growth = backlog_end - backlog_mid
    offered_after_midpoint = enqueued_end - enqueued_mid
    return {
        "backlog_mid": backlog_mid,
        "backlog_end": backlog_end,
        "offered_after_midpoint": offered_after_midpoint,
        "backlog_grew": (
            growth > BACKLOG_GROWTH_FLOOR_TASKS
            and growth > BACKLOG_GROWTH_LIMIT * offered_after_midpoint
        ),
    }


def read_throughput_profile(
    queue: str, window: psycopg.sql.Composable
) -> dict[str, t.Any]:
    """Throughput over successive equal-count slices of one drain, oldest first.

    Slice 0 is the fullest queue and the last the emptiest, which is what tells a cost
    rising with depth apart from a rep-to-rep difference.
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
    n_runs, completed_p10, completed_p90 = row[:3]
    total_runs, total_tasks, max_attempt, n_tasks = totals
    span = (completed_p90 - completed_p10) if completed_p10 is not None else 0.0
    # Too short a trimmed window makes 0.8*n/span an arbitrarily large number rather
    # than a rate, so it is refused rather than published.
    degenerate = span < MIN_TRIMMED_SPAN_S or not n_runs
    shares = sorted(fairness.values())
    metrics: dict[str, t.Any] = {
        # Tasks over the whole drain, runs over the measured window: a completion
        # can land ahead of that window and the task is finished either way.
        "n_tasks": int(n_tasks),
        "n_runs": int(n_runs),
        "total_runs": int(total_runs),
        "total_tasks": int(total_tasks),
        "max_attempt": int(max_attempt),
        # More run rows than tasks means a redelivery: a retry after a failure or an
        # expired claim lease. Both pollute a measurement; neither shows in wall time.
        "extra_runs": int(total_runs) - int(total_tasks),
        "degenerate_window": degenerate,
        # 0.8 * n over the p10..p90 completion span: the trimmed window absorbs a
        # multi-process fleet's short-handed opening and the driver's last drain poll.
        "throughput_per_s": 0.0 if degenerate else 0.8 * n_runs / span,
        "fairness": fairness,
        "fairness_ratio": (shares[0] / shares[-1]) if shares else 0.0,
    }
    metrics.update(
        dict(zip(LATENCY_KEYS, [value or 0.0 for value in row[3:]], strict=True))
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


def vacuum_queue_tables(queue: str) -> None:
    """Reclaim the dead rows a drain left in the queue tables.

    Plain VACUUM, not FULL: it takes the dead versions out of the heap and the indexes
    a claim reads, which is the thing under test, and leaves the relation the size the
    drain grew it to — so an arm that stays slow after this still holds every page it
    held before, and `read_table_state` records that beside the row counts.
    """
    with connections[resolve_absurd_database()].cursor() as cursor:
        for table in (f"t_{queue}", f"r_{queue}"):
            cursor.execute(
                psycopg.sql.SQL("vacuum {table}").format(
                    table=psycopg.sql.Identifier("absurd", table)
                )
            )


def refresh_table_state(queue: str) -> dict[str, dict[str, int]]:
    """ANALYZE the queue tables, then read back what each of them now holds.

    ANALYZE first because the dead rows below and the plans a claim gets are read off
    the same statistics, and a table just bulk-loaded and one that has churned carry
    different staleness — which would otherwise be a second difference between two
    arms meant to differ only in size. Costed separately and found to move no drain
    number (`benchmarks/CLAUDE.md`), so equalising it is free.
    """
    with connections[resolve_absurd_database()].cursor() as cursor:
        for table in (f"t_{queue}", f"r_{queue}"):
            cursor.execute(
                psycopg.sql.SQL("analyze {table}").format(
                    table=psycopg.sql.Identifier("absurd", table)
                )
            )
    return {
        role: read_table_state("absurd", f"{prefix}_{queue}")
        for role, prefix in (("tasks", "t"), ("runs", "r"))
    }


def read_table_state(schema: str, table: str) -> dict[str, int]:
    """One table's live rows, dead rows and total bytes, indexes and TOAST included.

    Dead rows through `pg_stat_get_dead_tuples` on the relation rather than
    `pg_stat_user_tables`, which has no row for a table the statistics collector has
    never seen and would leave the caller a missing key where the honest answer is
    zero. The live rows are counted instead (see `TABLE_STATE_SQL`).
    """
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute(
            psycopg.sql.SQL(TABLE_STATE_SQL).format(
                table=psycopg.sql.Identifier(schema, table)
            ),
            [f"{schema}.{table}"],
        )
        live, dead, total_bytes = cursor.fetchone()
    return {
        "live_tuples": int(live),
        "dead_tuples": int(dead),
        "total_bytes": int(total_bytes),
    }


def read_xact_commit() -> int:
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute(
            "select xact_commit from pg_stat_database "
            "where datname = current_database()"
        )
        return int(cursor.fetchone()[0])


def install_statement_stats() -> None:
    """Create the statement view on this database, or leave the run without one.

    A bootstrap step rather than a setup instruction because the extension is
    per-database and a RAM data directory loses it on every restart. A role that may
    not create extensions costs the run its itemisation and nothing else.
    """
    try:
        with connections[resolve_absurd_database()].cursor() as cursor:
            cursor.execute(
                psycopg.sql.SQL("create extension if not exists {stats}").format(
                    stats=psycopg.sql.Identifier(STATEMENT_STATS_EXTENSION)
                )
            )
    except db.Error:
        return


def read_statement_stats() -> list[dict[str, t.Any]] | None:
    """Every statement this database has run, or ``None`` where nothing counted them.

    A snapshot, never a reset: `pg_stat_statements_reset()` cannot be undone and would
    take out whatever else reads the same server, so a phase is two of these diffed.
    """
    try:
        with connections[resolve_absurd_database()].cursor() as cursor:
            cursor.execute(
                psycopg.sql.SQL("drop function if exists {reader}()").format(
                    reader=psycopg.sql.Identifier(STATEMENT_STATS_READER)
                )
            )
            cursor.execute(
                psycopg.sql.SQL(STATEMENT_STATS_READER_SQL).format(
                    reader=psycopg.sql.Identifier(STATEMENT_STATS_READER),
                    stats=psycopg.sql.Identifier(STATEMENT_STATS_EXTENSION),
                    stats_text=psycopg.sql.Literal(f"%{STATEMENT_STATS_EXTENSION}%"),
                    reader_text=psycopg.sql.Literal(f"%{STATEMENT_STATS_READER}%"),
                )
            )
            cursor.execute(
                psycopg.sql.SQL("select {reader}()").format(
                    reader=psycopg.sql.Identifier(STATEMENT_STATS_READER)
                )
            )
            return t.cast(
                "list[dict[str, t.Any]] | None", json.loads(cursor.fetchone()[0])
            )
    except db.Error:
        return None


def build_statement_stats(
    before: list[dict[str, t.Any]] | None,
    after: list[dict[str, t.Any]] | None,
    n_runs: int,
    phase_s: float,
) -> dict[str, t.Any] | None:
    """What one task cost statement by statement, and what was left over client-side.

    `client_ms_per_task` is the remainder — our Python, the SDK's, and the round trips.
    Server time is summed over every backend the phase used, so above one worker it
    counts concurrent work against one wall clock and the remainder stops being one.
    """
    if before is None or after is None or not n_runs:
        return None
    baseline = {row["queryid"]: row for row in before}
    issued = [
        delta
        for delta in (describe_statement_delta(row, baseline, n_runs) for row in after)
        if delta["calls_per_task"]
    ]
    # Top-level only: under `track=all` a PL/pgSQL function's own row already counts
    # what it ran inside itself, so summing both levels bills the claim path twice.
    server_ms = sum(
        delta["total_exec_ms_per_task"] for delta in issued if delta["toplevel"]
    )
    wall_ms = 1000.0 * phase_s / n_runs
    return {
        "statements": sorted(
            issued, key=lambda delta: delta["total_exec_ms_per_task"], reverse=True
        )[:STATEMENT_STATS_LIMIT],
        "wall_ms_per_task": wall_ms,
        "server_exec_ms_per_task": server_ms,
        "client_ms_per_task": wall_ms - server_ms,
    }


def describe_statement_delta(
    row: dict[str, t.Any], baseline: dict[int, dict[str, t.Any]], n_runs: int
) -> dict[str, t.Any]:
    """One statement's counters over the phase, divided by the tasks that ran in it."""
    was = baseline.get(row["queryid"], UNSEEN_STATEMENT)
    return {
        "query": row["query"],
        "toplevel": row["toplevel"],
        "calls_per_task": (row["calls"] - was["calls"]) / n_runs,
        "total_exec_ms_per_task": (
            (row["total_exec_ms"] - was["total_exec_ms"]) / n_runs
        ),
        "rows_per_task": (row["rows"] - was["rows"]) / n_runs,
    }
