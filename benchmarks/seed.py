"""Fill a queue's tables with a synthetic corpus, by cloning drained template rows.

Nothing here measures anything. It exists so a person can put millions of rows in
front of django-absurd's admin and page through them; every number taken on the result
is a number about this corpus, not about a workload.
"""

import argparse
import dataclasses
import os
import time
import uuid

import django
import psycopg.sql
from django.db import connections
from django.utils.module_loading import import_string

import analysis
import measurement
import runner
from django_absurd.exceptions import QueueNotProvisionedError
from django_absurd.flush import truncate_queue_tables
from django_absurd.queues import resolve_absurd_database

DEFAULT_QUEUE = "bench"
DEFAULT_ROWS = 1_000_000
# The rows every clone is copied from: mostly the ordinary completed case, plus one
# task that exhausts its attempts so failed and retried rows reach the corpus too.
TEMPLATE_TASKS: tuple[tuple[str, int], ...] = (
    ("tasks.noop_sync", 5),
    ("tasks.fail_on_every_attempt", 1),
)
TEMPLATE_DRAIN_TIMEOUT_S = 120.0
# Worker identities the cloned runs are spread over. Synthetic, and the corpus says so:
# one worker drains the templates, so every run would otherwise carry one claimed_by.
CLAIMED_BY_SPREAD = 8
# Clones per statement. One statement for millions would build every generated key and
# every cloned run before writing any of them.
CLONE_CHUNK_ROWS = 100_000

# Unique, so a clone has to leave it null: the one live column the guard tolerates the
# clone not writing.
UNCLONED_TASK_COLUMNS = frozenset({"idempotency_key"})

# What the clone writes into `t_<queue>`, and therefore what the shape guard demands.
TASK_CLONE_COLUMNS: tuple[tuple[str, psycopg.sql.Composable], ...] = (
    ("task_id", psycopg.sql.SQL("cloned.task_id")),
    ("task_name", psycopg.sql.SQL("template.task_name")),
    ("params", psycopg.sql.SQL("template.params")),
    ("headers", psycopg.sql.SQL("template.headers")),
    ("retry_strategy", psycopg.sql.SQL("template.retry_strategy")),
    ("max_attempts", psycopg.sql.SQL("template.max_attempts")),
    ("cancellation", psycopg.sql.SQL("template.cancellation")),
    ("enqueue_at", psycopg.sql.SQL("template.enqueue_at")),
    ("first_started_at", psycopg.sql.SQL("template.first_started_at")),
    ("state", psycopg.sql.SQL("template.state")),
    ("attempts", psycopg.sql.SQL("template.attempts")),
    # The clone's own last run, not the template's: a task pointing at another task's
    # run is a dangling link the admin renders as a real one.
    ("last_attempt_run", psycopg.sql.SQL("cloned_run.run_id")),
    ("completed_payload", psycopg.sql.SQL("template.completed_payload")),
    ("cancelled_at", psycopg.sql.SQL("template.cancelled_at")),
)

# What the clone writes into `r_<queue>`.
RUN_CLONE_COLUMNS: tuple[tuple[str, psycopg.sql.Composable], ...] = (
    ("run_id", psycopg.sql.SQL("cloned_run.run_id")),
    ("task_id", psycopg.sql.SQL("cloned_run.task_id")),
    ("attempt", psycopg.sql.SQL("template.attempt")),
    ("state", psycopg.sql.SQL("template.state")),
    (
        "claimed_by",
        psycopg.sql.SQL("'seed-worker-' || mod(cloned_run.copy, {spread})").format(
            spread=psycopg.sql.Literal(CLAIMED_BY_SPREAD)
        ),
    ),
    ("claim_expires_at", psycopg.sql.SQL("template.claim_expires_at")),
    ("available_at", psycopg.sql.SQL("template.available_at")),
    ("wake_event", psycopg.sql.SQL("template.wake_event")),
    ("event_payload", psycopg.sql.SQL("template.event_payload")),
    ("started_at", psycopg.sql.SQL("template.started_at")),
    ("completed_at", psycopg.sql.SQL("template.completed_at")),
    ("failed_at", psycopg.sql.SQL("template.failed_at")),
    ("result", psycopg.sql.SQL("template.result")),
    ("failure_reason", psycopg.sql.SQL("template.failure_reason")),
    ("created_at", psycopg.sql.SQL("template.created_at")),
)

# Keys generated up front, in `cloned`/`cloned_runs`, so the task insert can point each
# clone at the run about to be written for it. `absurd.portable_uuidv7` because the
# migration reaches for `pg_catalog.uuidv7` only where the server has it.
CLONE_SQL = psycopg.sql.SQL("""
with template_tasks as (
    select template_id, ordinal
    from unnest(%(template_ids)s::uuid[])
        with ordinality as templates(template_id, ordinal)
),
cloned as materialized (
    select
        template_tasks.template_id as from_task_id,
        absurd.portable_uuidv7() as task_id,
        copies.n as copy
    from generate_series(1, %(clones)s::bigint) as copies(n)
    join template_tasks
        on template_tasks.ordinal = 1 + mod(copies.n - 1, %(template_count)s::bigint)
),
cloned_runs as materialized (
    select
        cloned.task_id as task_id,
        cloned.copy as copy,
        run.run_id as from_run_id,
        absurd.portable_uuidv7() as run_id
    from cloned
    join {runs} run on run.task_id = cloned.from_task_id
),
cloned_tasks as (
    insert into {tasks} ({task_columns})
    select {task_values}
    from cloned
    join {tasks} template on template.task_id = cloned.from_task_id
    left join cloned_runs cloned_run
        on cloned_run.task_id = cloned.task_id
        and cloned_run.from_run_id = template.last_attempt_run
    returning 1
)
insert into {runs} ({run_columns})
select {run_values}
from cloned_runs cloned_run
join {runs} template on template.run_id = cloned_run.from_run_id
""")


@dataclasses.dataclass(frozen=True)
class ColumnDrift:
    missing: tuple[str, ...]
    unexpected: tuple[str, ...]


class QueueTableShapeError(Exception):
    def __init__(self, drift: dict[str, ColumnDrift]) -> None:
        clauses = []
        for table, columns in drift.items():
            if columns.missing:
                clauses.append(f"{table} has no {', '.join(columns.missing)}")
            if columns.unexpected:
                clauses.append(f"{table} has unknown {', '.join(columns.unexpected)}")
        super().__init__(
            f"The queue tables are not the shape this seeder clones: "
            f"{'; '.join(clauses)}. Upstream Absurd has moved them, so the clone would "
            f"write rows that look right and are not — a column it has never heard of "
            f"stays at its default on every cloned row. Reconcile TASK_CLONE_COLUMNS "
            f"and RUN_CLONE_COLUMNS in benchmarks/seed.py against django_absurd's "
            f"migration, then seed again."
        )


@dataclasses.dataclass(frozen=True)
class SeedSummary:
    tasks: int
    runs: int
    elapsed_s: float


def seed_queue_tables(rows: int, *, queue: str = DEFAULT_QUEUE) -> SeedSummary:
    """Leave ``queue``'s tables holding ``rows`` tasks and the runs behind them.

    The templates are enqueued through the real API and drained by a real worker, so a
    clone carries whatever django-absurd stores rather than a shape written by hand.
    Every count returned is read back off the tables: a seeder reporting what it meant
    to write reports success for a clone that wrote nothing.

    The queue is truncated first, so ``rows`` is what the corpus holds afterwards
    rather than what this run added to it.
    """
    check_queue_table_shape(queue)
    started = time.monotonic()
    truncate_queue_tables(queue)
    enqueue_templates(queue)
    drain_templates(queue)
    clone_templates(queue, rows)
    # A bulk-loaded table carries the statistics of the empty one it grew from, and
    # every plan the admin's queries get on it is then a plan for a different table.
    analysis.refresh_table_state(queue)
    return SeedSummary(
        tasks=count_table_rows(f"t_{queue}"),
        runs=count_table_rows(f"r_{queue}"),
        elapsed_s=time.monotonic() - started,
    )


def check_queue_table_shape(queue: str = DEFAULT_QUEUE) -> None:
    """Refuse a queue whose tables are not the ones the clone knows how to write.

    Both directions, because both write rows that look right and are not: a column the
    clone names and the table no longer has, and a column the table has grown that the
    clone never sets — the second one real on the templates a worker drained and left
    at its default on every row copied from them.
    """
    drift: dict[str, ColumnDrift] = {}
    for prefix, columns, tolerated in (
        ("t", TASK_CLONE_COLUMNS, UNCLONED_TASK_COLUMNS),
        ("r", RUN_CLONE_COLUMNS, frozenset[str]()),
    ):
        live = read_live_columns(f"{prefix}_{queue}")
        # No columns at all is a queue nobody provisioned, not a queue that drifted.
        if not live:
            raise QueueNotProvisionedError(queue)
        expected = {name for name, _ in columns}
        found = ColumnDrift(
            missing=tuple(name for name, _ in columns if name not in live),
            unexpected=tuple(sorted(live - expected - tolerated)),
        )
        if found.missing or found.unexpected:
            drift[f"absurd.{prefix}_{queue}"] = found
    if drift:
        raise QueueTableShapeError(drift)


def enqueue_templates(queue: str) -> None:
    for task_path, count in TEMPLATE_TASKS:
        task_object = import_string(task_path)
        for _ in range(count):
            task_object.using(queue_name=queue).enqueue()


def drain_templates(queue: str) -> None:
    """Run the templates to completion, so the corpus has finished runs to clone.

    Enqueueing alone leaves the runs table empty, and a corpus with no runs cannot
    answer anything about the admin's runs changelist.
    """
    spec = measurement.MeasurementSpec(
        name="seed templates",
        mode="saturation",
        task_path=TEMPLATE_TASKS[0][0],
        worker=runner.WorkerSpec(queue=queue),
        timeout_s=TEMPLATE_DRAIN_TIMEOUT_S,
    )
    workers = runner.start_workers(spec.worker, spec.workers)
    try:
        measurement.wait_until_drained(spec, workers)
    finally:
        runner.stop_workers(workers)


def clone_templates(queue: str, rows: int) -> None:
    """Copy the drained templates, round-robin, until the tasks table holds ``rows``."""
    template_ids = read_template_task_ids(queue)
    statement = CLONE_SQL.format(
        tasks=psycopg.sql.Identifier("absurd", f"t_{queue}"),
        runs=psycopg.sql.Identifier("absurd", f"r_{queue}"),
        task_columns=compose_column_names(TASK_CLONE_COLUMNS),
        task_values=compose_column_values(TASK_CLONE_COLUMNS),
        run_columns=compose_column_names(RUN_CLONE_COLUMNS),
        run_values=compose_column_values(RUN_CLONE_COLUMNS),
    )
    remaining = max(0, rows - len(template_ids))
    with connections[resolve_absurd_database()].cursor() as cursor:
        while remaining > 0:
            chunk = min(remaining, CLONE_CHUNK_ROWS)
            cursor.execute(
                statement,
                {
                    "template_ids": template_ids,
                    "clones": chunk,
                    "template_count": len(template_ids),
                },
            )
            remaining -= chunk


def read_template_task_ids(queue: str) -> list[uuid.UUID]:
    statement = psycopg.sql.SQL("select task_id from {tasks} order by task_id").format(
        tasks=psycopg.sql.Identifier("absurd", f"t_{queue}")
    )
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute(statement)
        return [row[0] for row in cursor.fetchall()]


def compose_column_names(
    columns: tuple[tuple[str, psycopg.sql.Composable], ...],
) -> psycopg.sql.Composable:
    return psycopg.sql.SQL(", ").join(
        psycopg.sql.Identifier(name) for name, _ in columns
    )


def compose_column_values(
    columns: tuple[tuple[str, psycopg.sql.Composable], ...],
) -> psycopg.sql.Composable:
    return psycopg.sql.SQL(", ").join(value for _, value in columns)


def read_live_columns(table: str) -> set[str]:
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute(
            "select column_name from information_schema.columns "
            "where table_schema = 'absurd' and table_name = %s",
            [table],
        )
        return {str(row[0]) for row in cursor.fetchall()}


def count_table_rows(table: str) -> int:
    statement = psycopg.sql.SQL("select count(*) from {table}").format(
        table=psycopg.sql.Identifier("absurd", table)
    )
    with connections[resolve_absurd_database()].cursor() as cursor:
        cursor.execute(statement)
        return int(cursor.fetchone()[0])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            f"Fill the '{DEFAULT_QUEUE}' queue's tables with a synthetic corpus, so "
            f"the admin has something to page through."
        )
    )
    parser.add_argument(
        "--rows",
        default=DEFAULT_ROWS,
        type=int,
        help=(
            f"Tasks the queue holds afterwards, the queue having been emptied first "
            f"(default: {DEFAULT_ROWS})."
        ),
    )
    args = parser.parse_args(argv)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
    django.setup()
    summary = seed_queue_tables(args.rows)
    print(
        f"seeded absurd.t_{DEFAULT_QUEUE}: {summary.tasks} tasks, {summary.runs} runs "
        f"in {summary.elapsed_s:.1f}s"
    )


if __name__ == "__main__":
    main()
