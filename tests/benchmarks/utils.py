import contextlib
import datetime as dt
import json
import os
import pathlib
import re
import signal
import threading
import time
import types
import typing as t
import uuid

import psycopg.sql
import time_machine
from absurd_sdk import RetryStrategy
from django.db import connections
from django.tasks import task

import analysis
from django_absurd import absurd_params
from django_absurd.queues import resolve_absurd_database

NAP_SECONDS = 30.0
NAP_INTERVAL_S = 0.02
# Exit code the fixture task below leaves behind, distinctive enough that a test can
# assert the harness reported the child's own status rather than a generic failure.
WORKER_EXIT_CODE = 9


class ProbeInterrupted(BaseException):
    """What `interrupt_after` raises: an interruption, not an error.

    A BaseException and deliberately NOT a KeyboardInterrupt, because that is the shape
    the harness actually meets. `pytest-timeout`'s alarm raises pytest's `Failed`, which
    derives from BaseException, and psycopg only cancels the running query for a
    KeyboardInterrupt — so anything else leaves the session mid-command, which is the
    state under test.
    """


def read_stage(results_dir: pathlib.Path, stage: str) -> dict[str, t.Any]:
    """Parse the results file a stage wrote, so a test can assert what is in it."""
    parsed: dict[str, t.Any] = json.loads(
        (results_dir / f"stage_{stage}.json").read_text()
    )
    return parsed


@contextlib.contextmanager
def hold_the_commit_probe_table() -> t.Iterator[None]:
    """Occupy the table name the commit-ceiling probe creates, so the probe refuses.

    The probe writes, so every way it can fail on a real server — no CREATE right, a
    read-only role, an unreachable database — is a way to break the harness's own
    connection too. A name already taken is the one failure a test can hand it that
    leaves everything else running, and it is a real one: a probe that dropped a table
    it had not created would take somebody's data with it.
    """
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute(f"create table {analysis.COMMIT_PROBE_TABLE} (n int)")
    try:
        yield
    finally:
        with connections[resolve_absurd_database()].cursor() as cursor:
            cursor.execute(f"drop table {analysis.COMMIT_PROBE_TABLE}")


@contextlib.contextmanager
def hold_the_statement_stats_name() -> t.Iterator[None]:
    """Occupy the view name the statement-stats extension installs, so it refuses.

    `create extension` builds a view named after the extension, so a relation already
    holding that name makes it fail with `duplicate_table` — the same shape as the
    managed-Postgres role that may not create extensions at all, and the one a test can
    hand it without a mock. The extension goes first because it owns that name itself
    whenever an earlier test in this database created it; nothing puts it back, and
    nothing needs to — a server with no extension and a server whose extension was
    never preloaded both read back as no statement stats.
    """
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute(f"drop extension if exists {analysis.STATEMENT_STATS_EXTENSION}")
        cursor.execute(
            f"create view {analysis.STATEMENT_STATS_EXTENSION} as select 1 as n"
        )
    try:
        yield
    finally:
        with connections[resolve_absurd_database()].cursor() as cursor:
            cursor.execute(f"drop view {analysis.STATEMENT_STATS_EXTENSION}")


@contextlib.contextmanager
def nap_the_wall_clock() -> t.Iterator[None]:
    """Run the body on a host whose WALL clock keeps outrunning its monotonic one.

    That disagreement is the only input the harness's
    `host.check_phase_uninterrupted`
    reads, and a test cannot suspend its own machine. time-machine moves `time.time`
    and leaves `perf_counter` alone, which is exactly the shape of a nap; the thread
    repeats the jump so a phase is suspended whenever inside the body it starts.

    Deliberately NOT the `dj_absurd` fixture, which is the wrong tool twice over: it
    moves Postgres too, erasing the very disagreement under test, and it writes a GUC
    on the parent's connection that the `absurd_worker` children never see. Nothing in
    the harness's own process derives a database deadline from Python's wall clock —
    every drain deadline is `time.monotonic` and every recorded timestamp is a Postgres
    column — so moving that clock reaches the guard and nothing else.
    """
    stopping = threading.Event()
    with time_machine.travel(dt.datetime.now(tz=dt.UTC), tick=True) as traveller:
        napping = threading.Thread(
            target=nap_until_stopped, args=(traveller, stopping), daemon=True
        )
        napping.start()
        try:
            yield
        finally:
            stopping.set()
            napping.join()


def nap_until_stopped(
    traveller: time_machine.Traveller, stopping: threading.Event
) -> None:
    while not stopping.wait(NAP_INTERVAL_S):
        traveller.shift(dt.timedelta(seconds=NAP_SECONDS))


def normalize_measured_numbers(output: str) -> str:
    """Console output with every measured value blanked, so it reads as a literal.

    Only a number carrying a unit is blanked: a measurement's own name has a decimal
    in it too (`poll_0.05`), and erasing that would erase what the line is about.
    """
    return re.sub(r"\d+\.\d+(?=[ %]|ms)", "N", output)


def strip_measurement_marks(output: str) -> str:
    """Console output with the bracketed row marks removed.

    Whether a smoke-sized measurement comes out invalid is a property of the run
    rather than of the driver — a one-second offer at a one-second poll interval
    sometimes lands too few completions to divide by — so a test about anything else
    must not assert it. The stage that measures nothing on purpose asserts them.
    """
    return re.sub(r" \[[A-Z ]+\]", "", output)


def normalize_measured_durations(rep: dict[str, t.Any]) -> dict[str, t.Any]:
    """A refused rep with everything unpinnable blanked, so it reads as a literal.

    Both numbers in the message are real elapsed times and both load samples are the
    machine's real load; what a test can pin is that the guard named the durations and
    that the rep was bracketed by a sample either side of it.
    """
    return {
        **rep,
        "error": re.sub(r"\d+\.\d+s", "Ns", rep["error"]),
        "load_before": rep["load_before"] >= 0.0,
        "load_after": rep["load_after"] >= 0.0,
    }


@task(queue_name="bench")
@absurd_params(
    max_attempts=2, retry_strategy=RetryStrategy(kind="fixed", base_seconds=0)
)
def sleep_past_claim_lease(seconds: float = 2.0) -> int:
    """Outlives its claim lease so Absurd's expired-lease sweep redelivers it.

    Capped at two attempts: with the default five this would loop for half a minute.
    """
    time.sleep(seconds)
    return 0


@task(queue_name="bench")
@absurd_params(max_attempts=1)
def fail_on_its_only_attempt() -> t.Never:
    """Terminally fails without redelivery, so the task ends 'failed' with no run of
    its own ever completing — the shape that shrinks a measurement's sample silently."""
    msg = "benchmark fixture: this task always fails"
    raise RuntimeError(msg)


@contextlib.contextmanager
def interrupt_after(seconds: float) -> t.Iterator[None]:
    """Interrupt this thread mid-body with a real signal, the way a timeout alarm does.

    A test cannot ask psycopg to abandon a wait; the only thing that does is a signal
    delivered while it is waiting, which is exactly what fires here. The suite's own
    timeout uses the same alarm, so both the handler and the pending timer are put
    back on the way out.
    """
    previous_handler = signal.signal(signal.SIGALRM, raise_probe_interrupted)
    previous_delay, previous_interval = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, previous_delay, previous_interval)
        signal.signal(signal.SIGALRM, previous_handler)


def raise_probe_interrupted(signum: int, frame: types.FrameType | None) -> t.Never:
    msg = "the wait was interrupted from outside the process"
    raise ProbeInterrupted(msg)


@task(queue_name="bench")
@absurd_params(max_attempts=1)
def kill_the_worker_that_claimed_it() -> t.Never:
    """Ends its own worker process mid-task, leaving the queue with nobody to drain it.

    `os._exit` rather than an exception or a signal: a task that raises is retried and
    a SIGTERM is what a stopping worker gets anyway, while this is the shape a fleet
    dies in — a child gone, its claim never completed, and a queue that no longer moves.
    """
    os._exit(WORKER_EXIT_CODE)


def insert_hand_timed_task(
    queue: str,
    enqueue_at: dt.datetime,
    started_at: dt.datetime,
    completed_at: dt.datetime,
    claimed_by: str,
    *,
    redelivered: bool = False,
) -> None:
    """One task and its completed run, at timestamps the caller chose itself.

    Written straight into Absurd's own tables: every metric is defined on these
    columns, and a real drain only produces timestamps nothing can predict, so a
    number a test can compute by hand has to be timed by hand.

    ``redelivered`` adds the failed first attempt in front of the completed one, which
    is what a run count that no state filters is there to notice.
    """
    task_id = uuid.uuid4()
    attempt = 2 if redelivered else 1
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute(
            psycopg.sql.SQL(
                "insert into {tasks} "
                "(task_id, task_name, params, enqueue_at, state, attempts) "
                "values (%s, 'tasks.noop_sync', '{{}}', %s, 'completed', %s)"
            ).format(tasks=psycopg.sql.Identifier("absurd", f"t_{queue}")),
            [task_id, enqueue_at, attempt],
        )
        if redelivered:
            cursor.execute(
                psycopg.sql.SQL(
                    "insert into {runs} "
                    "(run_id, task_id, attempt, state, claimed_by, available_at) "
                    "values (%s, %s, 1, 'failed', %s, %s)"
                ).format(runs=psycopg.sql.Identifier("absurd", f"r_{queue}")),
                [uuid.uuid4(), task_id, claimed_by, enqueue_at],
            )
        cursor.execute(
            psycopg.sql.SQL(
                "insert into {runs} "
                "(run_id, task_id, attempt, state, claimed_by, available_at, "
                "started_at, completed_at) "
                "values (%s, %s, %s, 'completed', %s, %s, %s, %s)"
            ).format(runs=psycopg.sql.Identifier("absurd", f"r_{queue}")),
            [
                uuid.uuid4(),
                task_id,
                attempt,
                claimed_by,
                enqueue_at,
                started_at,
                completed_at,
            ],
        )
