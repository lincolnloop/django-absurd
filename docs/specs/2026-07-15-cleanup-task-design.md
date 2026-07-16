# Shipped cleanup helper — design

## Intent

Absurd retains task + event history per queue (`cleanup_ttl` / `cleanup_limit` policy);
`absurd.cleanup_all_queues()` enforces it, but nothing runs it without `absurdctl cron`
/ external cron. Ship the cleanup **logic** (a plain function) + an on-demand management
command, so users enforce retention through django-absurd — no external tooling. For
scheduled enforcement, users register their own one-line `@task` wrapper (on whatever
backend + queue they choose) and put it in `SCHEDULE`.

**Why no shipped `@task`:** a shipped `@task` binds to a backend alias + queue at import
time. Django's task backend + queue defaults are the fixed string `"default"`
(`DEFAULT_TASK_QUEUE_NAME`, a hardcoded constant — not a setting), and
`BaseTaskBackend.validate_task` runs at decoration (import) time. A config whose Absurd
backend isn't at alias `"default"`, or that declares no `"default"` queue, would fail at
import. Shipping the function instead sidesteps both unknowns — the user's wrapper binds
to their own backend + queue. Matches how task libraries normally work (ship logic,
users decorate) + Absurd's own "wrap cleanup in a registered task" guidance. Multi-alias
tightening tracked in #63.

## Components (new)

One shared cleanup function + one on-demand command:

**Shared — `django_absurd/tasks.py`:**

```
def run_cleanup() -> list[dict]:
    # execute on connections[resolve_absurd_database()]:
    #   select queue_name, tasks_deleted, events_deleted from absurd.cleanup_all_queues()
    # return the rows as list[dict] (JSON-serializable) so a user's @task wrapper can
    # return them as its task result
```

**On-demand — `django_absurd/management/commands/absurd_cleanup.py`:** calls
`run_cleanup()` synchronously (in-process, no worker needed) and writes the per-queue
deleted counts to stdout — same pattern as `absurd_sync_queues`.

Policy-driven, no args: `cleanup_all_queues()` reads each queue's own `cleanup_ttl` /
`cleanup_limit` from `absurd.queues`, deleting terminal-state task + event history whose
age exceeds the ttl, batch-limited. Covers both storage modes for **row** retention. SDK
exposes no cleanup method (only get/set_queue_policy), so `run_cleanup` runs the SQL
directly via a cursor on the resolved Absurd database. Verb-named functions; no
leading-underscore helpers.

**Deletion boundary (Absurd semantics, verified):** eligibility is measured from a run's
**terminal** timestamp (`completed_at` / `failed_at` / `cancelled_at`), NOT
`enqueue_at`. Only terminal-state tasks are eligible; in-flight/pending tasks are never
deleted regardless of age. One `cleanup_all_queues()` call deletes at most
`cleanup_limit` rows **per queue** (batch cap) — a backlog beyond the limit drains over
successive runs.

**Transaction caveat:** `cleanup_all_queues()` has no own transaction control — it runs
in the caller's transaction. `run_cleanup` executes it in the ordinary Django connection
context (autocommit / request-less command context); no explicit `atomic()` wrapper
needed, and none that would hold a long lock.

## Command error handling

- **Schema absent** (migrations not run → `absurd` schema/functions missing): raise
  `ImproperlyConfigured("Absurd schema is not installed. Run: manage.py migrate")`.
- **No Absurd backend configured** (`TASKS` has no `AbsurdBackend`): write
  `"No Absurd task backends configured."` and exit without error (nothing to clean).

## Configuration — nothing new

Retention is the existing per-queue `OPTIONS["QUEUES"][<queue>]` knobs — `cleanup_ttl`
(str interval, default `30 days`) and `cleanup_limit` (int, default `1000`) — already
declared as `CreateQueueOptions`, synced by `sync_queues`/`reconcile_queue` (mutable;
drift re-syncs via `set_queue_policy`). The feature only _enforces_ them.

## Scheduling — user side, scheduler-agnostic

User registers a one-line wrapper `@task` on their backend + queue, then schedules it.
Works under beat or pg_cron:

```python
# myapp/tasks.py
from django.tasks import task
from django_absurd.tasks import run_cleanup

@task
def cleanup_queues():
    return run_cleanup()   # per-queue deleted counts become the task result
```

```python
# settings.py — one SCHEDULE entry
"SCHEDULE": {"absurd-cleanup": {
    "task": "myapp.tasks.cleanup_queues", "cron": "0 3 * * *"}}
```

The wrapper's queue (decorator `queue=` or the `SCHEDULE` entry's `queue`, which takes
runtime precedence) is where the cleanup run executes. Its return value is stored as the
task result and visible in Runs — this is the "wire up the task result table" outcome.
The `absurd_cleanup` command is the on-demand escape hatch (and works for any config,
including one with no `"default"` queue).

## Testing (behavioral, real DB, no mocks)

- **Command happy path:** seed old terminal task + event history on an (unpartitioned)
  queue with a short `cleanup_ttl`, `call_command("absurd_cleanup")` with `capsys` →
  assert the full emitted per-queue summary text AND that the aged rows are gone (first
  coverage of cleanup _execution_).
- **Deletion boundary:** a task past `cleanup_ttl` measured from `enqueue_at` but NOT
  yet terminal is NOT deleted; the same task once terminal (and aged) IS deleted.
- **Batch limit:** with `cleanup_limit` = N and > N aged rows, one command run deletes
  exactly N; a second run drains the rest.
- **User-wrapper task via worker:** register a wrapper `@task` calling `run_cleanup()`,
  enqueue, run the worker in burst → assert the result is the deleted-counts list
  (`queue_name` / `tasks_deleted` / `events_deleted`).
- **Schema absent:** drop the `absurd` schema → `call_command("absurd_cleanup")` raises
  `ImproperlyConfigured` with the exact message.
- **No backend:** `override_settings` `TASKS = {}` → command writes the exact "No Absurd
  task backends configured." line, exits clean.
- Tests use the default unpartitioned path only.

## Out of scope

- Partition lifecycle (`ensure_partitions` / detach) and partitioned-queue _behavior_ —
  tracked in #61 (implement-or-disable), including the false "automatic partition
  lifecycle" doc claim. Cleanup does row retention for partitioned queues but not their
  partition lifecycle.
- The **dangerous drop-all-queues** management mode — stays in #26 (this delivers only
  #26's manual on-demand _cleanup_ half, via `absurd_cleanup`).
- A **shipped `@task`** and multi-backend-alias tightening — #63.
- Per-queue / ttl-override arguments — retention lives in queue policy.

## Deviations from this spec (as-built, 2026-07-16)

Bookkeeping — where the shipped branch diverged from the design above:

- **Function renamed + relocated.** `run_cleanup()` in `django_absurd/tasks.py` →
  `cleanup_queues()` in `django_absurd/cleanup.py`. `tasks.py` misread as a Django
  tasks-autodiscovery module; `cleanup.py` clearer. `cleanup_queues` mirrors the SQL
  `absurd.cleanup_all_queues()`.
- **Return type refined.** `list[dict]` → `list[QueueCleanup]` (a `TypedDict`) — same
  runtime dict (JSON-serializable), fields now named/typed. NamedTuple/dataclass
  rejected (serialize as arrays / not JSON).
- **Schema-absent guard DROPPED.** Spec mandated catching `ProgrammingError` →
  `ImproperlyConfigured("Absurd schema is not installed. Run: manage.py migrate")`.
  Removed by decision — raw error bubbles ("too bad if you're there"); the schema-absent
  test went with it.
- **Per-queue targeting ADDED** (was out of scope). `cleanup_queues(queues=None)` +
  `absurd_cleanup [queue …]` positional args. Delivers #26's per-queue half.
  (ttl-override args still out of scope.)
- **Drop-all-queues ADDED** (spec left it in #26). Shipped `absurd_flush` — drops every
  queue + data, keeps schema; Django `flush` UX (interactive confirm + `--noinput`). #26
  now fully delivered here.
- **Wrapper example renamed.** Documented user wrapper `cleanup_queues` → `cleanup` (the
  util took the `cleanup_queues` name).
- **Docs page.** User-facing cleanup docs live at `docs/web/cleanup.md` (nav "Cleanup"),
  not the scheduling page.
- **Tests.** Behavioral tests parametrized over both entrypoints (command + direct) via
  a `cleanup` fixture; `absurd_flush` tested both interactively (real `sys.stdin`) and
  `--noinput`.
- **Scheduling scoped to beat / application-level.** Spec called the wrapper
  "scheduler-agnostic (beat or pg_cron)"; user docs now present it as the beat path
  only. pg_cron deployments get Absurd's native maintenance surface (`enable_cron`:
  partitions/cleanup/detach) instead — deferred to #64 (django-absurd currently exposes
  no such surface).
