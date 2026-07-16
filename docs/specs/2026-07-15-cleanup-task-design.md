# Shipped cleanup task — design

## Intent

Absurd retains task + event history per queue (`cleanup_ttl` / `cleanup_limit` policy);
`absurd.cleanup_all_queues()` enforces it, but nothing runs it on a schedule without
`absurdctl cron` / external cron. Ship a library `@task` that calls it, so users
schedule retention through django-absurd's own `SCHEDULE` (beat or pg_cron) — no
external tooling. The task returns the per-queue deleted counts, recorded as its task
result (visibility).

## Components (new)

One shared cleanup function, two entrypoints (scheduled task + on-demand command):

**Shared — `django_absurd/tasks.py`:**

```
def run_cleanup() -> list[dict]:
    # execute on connections[resolve_absurd_database()]:
    #   select queue_name, tasks_deleted, events_deleted from absurd.cleanup_all_queues()
    # return the rows as list[dict] (JSON-serializable)

@task
def cleanup_queues() -> list[dict]:          # the library's first shipped @task
    # call run_cleanup(); log a one-line summary; return the rows (the task result)
```

**On-demand — `django_absurd/management/commands/absurd_cleanup.py`:** calls
`run_cleanup()` synchronously (in-process, no worker needed) and writes the per-queue
deleted counts to stdout — same pattern as `absurd_sync_queues`.

Policy-driven, no args: `cleanup_all_queues()` reads each queue's own `cleanup_ttl` /
`cleanup_limit` from `absurd.queues`, deleting task + event history older than the ttl,
batch-limited. Covers both storage modes for **row** retention. The SDK exposes no
cleanup method, so `run_cleanup` runs the SQL directly via a cursor on the resolved
Absurd database. Verb-named functions; no leading-underscore helpers.

## Configuration — nothing new

Retention is the existing per-queue `OPTIONS["QUEUES"][<queue>]` knobs — `cleanup_ttl`
(str interval, default `30 days`) and `cleanup_limit` (int, default `1000`) — already
declared as `CreateQueueOptions`, synced by `sync_queues`/`reconcile_queue` (mutable;
drift re-syncs via `set_queue_policy`). The feature only _enforces_ them on a schedule.

## Scheduling — user side, scheduler-agnostic

One `SCHEDULE` entry works under beat or pg_cron:

```python
"SCHEDULE": {"absurd-cleanup": {
    "task": "django_absurd.tasks.cleanup_queues", "cron": "0 3 * * *"}}
```

## Result

The return value (per-queue deleted counts) is stored as the task result and logged —
this is the "wire up the task result table" outcome: the cleanup run's effect is
inspectable in Runs / results.

## Testing (behavioral, real DB + worker, no mocks)

- **Task via worker:** enqueue `cleanup_queues`, run the worker in burst → assert the
  result is the deleted-counts list (`queue_name` / `tasks_deleted` / `events_deleted`).
- **Command:** `call_command("absurd_cleanup")` with `capsys` → assert the full emitted
  per-queue summary text.
- **Execution:** seed old task history on an (unpartitioned) queue with a short
  `cleanup_ttl`, run cleanup (via either entrypoint) → assert the aged rows are deleted
  (first coverage of cleanup _execution_).
- Tests use the default unpartitioned path only.

## Out of scope

- Partition lifecycle (`ensure_partitions` / detach) and partitioned-queue _behavior_ —
  tracked in #61 (implement-or-disable), including the false "automatic partition
  lifecycle" doc claim. Cleanup does row retention for partitioned queues but not their
  partition lifecycle.
- The **dangerous drop-all-queues** management mode — stays in #26 (this delivers only
  #26's manual on-demand _cleanup_ half, via `absurd_cleanup`).
- Per-queue / ttl-override arguments — retention lives in queue policy.
