"""What ``load_seed``'s clone knows about the per-queue Absurd tables.

Two things live here together because they must move together: the column list the
clone copies, and the override map naming the columns it must NOT copy verbatim.
:func:`check_columns_match` compares the list against ``information_schema`` before a
single row is written, so a pinned-``absurdctl`` bump that adds or drops a column
fails loudly here instead of silently cloning a default into millions of rows.

The override expressions are SQL fragments, so they name the CTE aliases that
``load_seed``'s clone statement defines: ``mapped`` (one row per clone, carrying the
template task's columns plus ``clone_task_id`` and ``clone_shift``) and ``cloned_runs``
(one row per clone run, carrying the template run's columns plus ``clone_run_id``).
"""

import psycopg.sql
from django.core.management.base import CommandError
from django.db import connections

ABSURD_SCHEMA = "absurd"

TASK_CLONE_COLUMNS: tuple[str, ...] = (
    "task_id",
    "task_name",
    "params",
    "headers",
    "retry_strategy",
    "max_attempts",
    "cancellation",
    "enqueue_at",
    "first_started_at",
    "state",
    "attempts",
    "last_attempt_run",
    "completed_payload",
    "cancelled_at",
    "idempotency_key",
)

RUN_CLONE_COLUMNS: tuple[str, ...] = (
    "run_id",
    "task_id",
    "attempt",
    "state",
    "claimed_by",
    "claim_expires_at",
    "available_at",
    "wake_event",
    "event_payload",
    "started_at",
    "completed_at",
    "failed_at",
    "result",
    "failure_reason",
    "created_at",
)

# Every timestamp of a clone moves by the SAME `clone_shift`, so a cloned task and its
# runs stay coherent relative to each other while landing somewhere inside `--window`.
TASK_CLONE_OVERRIDES: dict[str, psycopg.sql.Composable] = {
    "task_id": psycopg.sql.SQL("mapped.clone_task_id"),
    "enqueue_at": psycopg.sql.SQL("mapped.enqueue_at - mapped.clone_shift"),
    "first_started_at": psycopg.sql.SQL("mapped.first_started_at - mapped.clone_shift"),
    # The clone's own last run, not the template's — the LEFT JOIN in the clone
    # statement resolves it, and stays NULL for a template that never ran.
    "last_attempt_run": psycopg.sql.SQL("cloned_runs.clone_run_id"),
    "cancelled_at": psycopg.sql.SQL("mapped.cancelled_at - mapped.clone_shift"),
    # `text unique` on an unpartitioned queue: NULLs never collide, a copied value
    # would on the second clone.
    "idempotency_key": psycopg.sql.SQL("NULL::text"),
}

RUN_CLONE_OVERRIDES: dict[str, psycopg.sql.Composable] = {
    "run_id": psycopg.sql.SQL("cloned_runs.clone_run_id"),
    "task_id": psycopg.sql.SQL("cloned_runs.clone_task_id"),
    "claim_expires_at": psycopg.sql.SQL(
        "cloned_runs.claim_expires_at - cloned_runs.clone_shift"
    ),
    "available_at": psycopg.sql.SQL(
        "cloned_runs.available_at - cloned_runs.clone_shift"
    ),
    "started_at": psycopg.sql.SQL("cloned_runs.started_at - cloned_runs.clone_shift"),
    "completed_at": psycopg.sql.SQL(
        "cloned_runs.completed_at - cloned_runs.clone_shift"
    ),
    "failed_at": psycopg.sql.SQL("cloned_runs.failed_at - cloned_runs.clone_shift"),
    "created_at": psycopg.sql.SQL("cloned_runs.created_at - cloned_runs.clone_shift"),
}


def check_columns_match(table: str, expected: set[str], using: str) -> None:
    """Refuse to clone ``absurd.<table>`` unless its columns are exactly
    ``expected``."""
    actual = fetch_table_columns(table, using)
    unknown = sorted(actual - expected)
    missing = sorted(expected - actual)
    if not unknown and not missing:
        return
    problems = []
    if unknown:
        problems.append(f"unknown column(s) {', '.join(unknown)}")
    if missing:
        problems.append(f"missing column(s) {', '.join(missing)}")
    msg = (
        f"{ABSURD_SCHEMA}.{table} is not the table loadtest knows how to clone: "
        f"{'; '.join(problems)}. Update the clone column list and override map in "
        f"loadtest/schema.py to match the current Absurd schema."
    )
    raise CommandError(msg)


def fetch_table_columns(table: str, using: str) -> set[str]:
    with connections[using].cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s",
            [ABSURD_SCHEMA, table],
        )
        return {name for (name,) in cur.fetchall()}


def compose_clone_select(
    columns: tuple[str, ...], overrides: dict[str, psycopg.sql.Composable], source: str
) -> psycopg.sql.Composable:
    """The SELECT list feeding one clone INSERT: each column overridden or copied."""
    return psycopg.sql.SQL(", ").join(
        overrides.get(column, psycopg.sql.Identifier(source, column))
        for column in columns
    )


def compose_column_list(columns: tuple[str, ...]) -> psycopg.sql.Composable:
    return psycopg.sql.SQL(", ").join(
        psycopg.sql.Identifier(column) for column in columns
    )
