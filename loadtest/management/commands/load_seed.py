"""Fill the load-harness queues with rows Absurd itself wrote, then multiply them.

Three phases per queue: template (real ``enqueue`` + a burst worker, so every row is
in a shape the engine actually produces), clone (server-side ``INSERT ... SELECT`` off
those templates, chunked), and ``ANALYZE`` (stale planner stats would make every
downstream ``EXPLAIN`` a lie).
"""

import datetime as dt
import typing as t
import uuid

import absurd_sdk
import psycopg.sql
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction

from django_absurd import absurd_params, queues
from django_absurd.exceptions import QueueNotDeclaredError
from django_absurd.flush import truncate_queue_tables
from loadtest import schema, tasks
from loadtest.models import ExecutionLog

if t.TYPE_CHECKING:
    from django.core.management.base import CommandParser

BULK_QUEUE = "bulk"
DEFAULT_BULK_TASKS = 1_000_000
TOKEN_QUEUE_TASKS = 1_000
DEFAULT_WINDOW_DAYS = 30
CHUNK_SIZE = 100_000

COMPLETED_TEMPLATE_COUNT = 4
FAILED_TEMPLATE_COUNT = 2
PENDING_TEMPLATE_COUNT = 2
FAILED_TEMPLATE_ATTEMPTS = 3

# Zero backoff so the same burst exhausts every attempt: the template then carries a
# genuine retry history (several failed runs) instead of one lonely failure.
RETRY_IMMEDIATELY: absurd_sdk.RetryStrategy = {"kind": "fixed", "base_seconds": 0}


class Command(BaseCommand):
    help = (
        "Seed the load-harness queues: enqueue and drain real template tasks, then "
        "clone them server-side up to the requested row count."
    )

    def add_arguments(self, parser: "CommandParser") -> None:
        parser.add_argument(
            "--queue",
            action="append",
            default=None,
            help="Queue to seed; repeatable (default: every declared queue).",
        )
        parser.add_argument(
            "--tasks",
            type=int,
            default=DEFAULT_BULK_TASKS,
            help=(
                f"Total task rows to leave on the '{BULK_QUEUE}' queue "
                f"(default: {DEFAULT_BULK_TASKS}). Every other queue gets "
                f"{TOKEN_QUEUE_TASKS}, so the admin's union views have arms of "
                "realistically mismatched size."
            ),
        )
        parser.add_argument(
            "--window",
            type=int,
            default=DEFAULT_WINDOW_DAYS,
            help=(
                "Days of history the cloned timestamps spread over "
                f"(default: {DEFAULT_WINDOW_DAYS})."
            ),
        )
        parser.add_argument(
            "--truncate",
            action="store_true",
            help=(
                "Empty the selected queues' tables before seeding, and the execution "
                "log in full. The log is cleared for EVERY queue, not just the ones "
                "--queue names — it has no queue column to narrow the delete by."
            ),
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        backend = queues.get_absurd_backend()
        if backend is None:
            msg = (
                "No Absurd backend configured. loadtest.settings should declare one "
                "in TASKS."
            )
            raise CommandError(msg)
        selected: list[str] = options["queue"] or sorted(backend.queues)
        for queue in selected:
            if queue not in backend.queues:
                raise CommandError(
                    str(QueueNotDeclaredError(queue, backend.alias, backend.queues))
                )
        if options["tasks"] < 1:
            msg = "--tasks must be at least 1."
            raise CommandError(msg)
        if options["window"] < 1:
            msg = "--window must be at least 1 day."
            raise CommandError(msg)

        using = queues.resolve_absurd_database()
        if options["truncate"]:
            deleted, _ = ExecutionLog.objects.all().delete()
            self.stdout.write(f"execution log: deleted {deleted} rows")
        for queue in selected:
            self.seed_queue(
                queue,
                target=options["tasks"] if queue == BULK_QUEUE else TOKEN_QUEUE_TASKS,
                window_days=options["window"],
                truncate=options["truncate"],
                using=using,
            )

    def seed_queue(
        self,
        queue: str,
        *,
        target: int,
        window_days: int,
        truncate: bool,
        using: str,
    ) -> None:
        # First, before a single row is written or destroyed: a column the clone
        # doesn't know about would otherwise be silently defaulted across the whole
        # target count, and provisioning a queue whose tables have drifted fails in
        # far less legible ways.
        schema.check_columns_match(
            f"t_{queue}", set(schema.TASK_CLONE_COLUMNS), using=using
        )
        schema.check_columns_match(
            f"r_{queue}", set(schema.RUN_CLONE_COLUMNS), using=using
        )
        if truncate:
            truncate_queue_tables(queue)
            self.stdout.write(f"{queue}: truncated")
        template_ids = self.seed_templates(queue)
        self.clone_templates(
            queue,
            template_ids=template_ids,
            target=target,
            window_days=window_days,
            using=using,
        )
        analyze_queue_tables(queue, using)
        self.stdout.write(f"{queue}: analyzed")

    def seed_templates(self, queue: str) -> list[uuid.UUID]:
        """Enqueue and drain real tasks, returning the ids the clone may copy from.

        The mix is the clone's state distribution — every clone inherits some
        template's shape — so it deliberately spans completed, failed-with-retries and
        still-pending.
        """
        template_ids = [
            parse_task_uuid(
                tasks.burn_sync.using(queue_name=queue).enqueue({"n": index}).id
            )
            for index in range(COMPLETED_TEMPLATE_COUNT)
        ]
        # A payload with no "n" makes burn_sync raise KeyError — the failure is the
        # point, and it is the task's own real failure path, not a staged one.
        template_ids += [
            parse_task_uuid(
                absurd_params(
                    max_attempts=FAILED_TEMPLATE_ATTEMPTS,
                    retry_strategy=RETRY_IMMEDIATELY,
                )
                .bind(tasks.burn_sync.using(queue_name=queue))
                .enqueue({})
                .id
            )
            for _ in range(FAILED_TEMPLATE_COUNT)
        ]
        self.stdout.write(
            f"{queue}: draining templates — {FAILED_TEMPLATE_COUNT} of them are "
            f"supposed to fail ({FAILED_TEMPLATE_ATTEMPTS} attempts each), so the "
            "tracebacks the worker logs next are the seed working, not breaking."
        )
        self.drain(queue)

        # Not a clone template: its checkpoint, event and wait rows are what the three
        # small admin entities get, and cloning a task whose durable state stays behind
        # would only produce incoherent rows.
        tasks.burn_workflow.using(queue_name=queue).enqueue({"n": 0})
        self.drain(queue)

        # After the last drain, so these stay pending.
        template_ids += [
            parse_task_uuid(
                tasks.burn_sync.using(queue_name=queue).enqueue({"n": index}).id
            )
            for index in range(PENDING_TEMPLATE_COUNT)
        ]
        self.stdout.write(f"{queue}: {len(template_ids)} templates written")
        return template_ids

    def clone_templates(
        self,
        queue: str,
        *,
        template_ids: list[uuid.UUID],
        target: int,
        window_days: int,
        using: str,
    ) -> None:
        existing = count_queue_tasks(queue, using)
        remaining = target - existing
        if remaining <= 0:
            self.stdout.write(
                f"{queue}: {existing} task rows already meet the target of {target}; "
                "nothing cloned"
            )
            return
        statement = build_clone_statement(queue)
        window_seconds = dt.timedelta(days=window_days).total_seconds()
        written = 0
        while written < remaining:
            chunk = min(CHUNK_SIZE, remaining - written)
            with (
                transaction.atomic(using=using),
                connections[using].cursor() as cur,
            ):
                cur.execute(
                    statement,
                    {
                        "template_ids": template_ids,
                        "template_count": len(template_ids),
                        "chunk": chunk,
                        "window_seconds": window_seconds,
                    },
                )
            written += chunk
            self.stdout.write(f"{queue}: cloned {written}/{remaining}")

    def drain(self, queue: str) -> None:
        call_command("absurd_worker", queue=queue, burst=True, stdout=self.stdout)


def parse_task_uuid(task_result_id: str) -> uuid.UUID:
    """Pull the bare task uuid out of a Django ``TaskResult.id`` (``"queue:uuid"``)."""
    return uuid.UUID(task_result_id.rpartition(":")[2])


def build_clone_statement(queue: str) -> str:
    """One statement that clones tasks and their runs together.

    Both halves read the pre-statement snapshot, so a chunk can never clone a clone —
    and `tmpl` is restricted to the template ids anyway. `MATERIALIZED` is not
    decoration: `mapped` and `cloned_runs` are each read twice, and the generated
    `clone_task_id` / `clone_run_id` must be the same values in both readings.
    """
    return (
        psycopg.sql.SQL("""
            WITH tmpl AS MATERIALIZED (
                SELECT row_number() OVER (ORDER BY task_id) - 1 AS template_index, t.*
                FROM {task_table} AS t
                WHERE t.task_id = ANY(%(template_ids)s::uuid[])
            ),
            mapped AS MATERIALIZED (
                SELECT
                    uuidv7() AS clone_task_id,
                    make_interval(secs => random() * %(window_seconds)s)
                        AS clone_shift,
                    tmpl.*
                FROM generate_series(1, %(chunk)s) AS series(clone_index)
                JOIN tmpl
                  ON tmpl.template_index
                     = mod(series.clone_index, %(template_count)s)
            ),
            cloned_runs AS MATERIALIZED (
                SELECT
                    uuidv7() AS clone_run_id,
                    mapped.clone_task_id,
                    mapped.clone_shift,
                    r.*
                FROM mapped
                JOIN {run_table} AS r ON r.task_id = mapped.task_id
            ),
            inserted_runs AS (
                INSERT INTO {run_table} ({run_columns})
                SELECT {run_values}
                FROM cloned_runs
                RETURNING 1
            )
            INSERT INTO {task_table} ({task_columns})
            SELECT {task_values}
            FROM mapped
            LEFT JOIN cloned_runs
                   ON cloned_runs.clone_task_id = mapped.clone_task_id
                  AND cloned_runs.run_id = mapped.last_attempt_run
        """)
        .format(
            task_table=psycopg.sql.Identifier(schema.ABSURD_SCHEMA, f"t_{queue}"),
            run_table=psycopg.sql.Identifier(schema.ABSURD_SCHEMA, f"r_{queue}"),
            task_columns=schema.compose_column_list(schema.TASK_CLONE_COLUMNS),
            run_columns=schema.compose_column_list(schema.RUN_CLONE_COLUMNS),
            task_values=schema.compose_clone_select(
                schema.TASK_CLONE_COLUMNS, schema.TASK_CLONE_OVERRIDES, "mapped"
            ),
            run_values=schema.compose_clone_select(
                schema.RUN_CLONE_COLUMNS, schema.RUN_CLONE_OVERRIDES, "cloned_runs"
            ),
        )
        .as_string(None)
    )


def count_queue_tasks(queue: str, using: str) -> int:
    statement = (
        psycopg.sql.SQL("SELECT count(*) FROM {table}")
        .format(table=psycopg.sql.Identifier(schema.ABSURD_SCHEMA, f"t_{queue}"))
        .as_string(None)
    )
    with connections[using].cursor() as cur:
        cur.execute(statement)
        return int(cur.fetchone()[0])


def analyze_queue_tables(queue: str, using: str) -> None:
    with connections[using].cursor() as cur:
        for prefix in queues.QUEUE_TABLE_PREFIXES:
            cur.execute(
                psycopg.sql.SQL("ANALYZE {table}")
                .format(
                    table=psycopg.sql.Identifier(
                        schema.ABSURD_SCHEMA, f"{prefix}_{queue}"
                    )
                )
                .as_string(None)
            )
