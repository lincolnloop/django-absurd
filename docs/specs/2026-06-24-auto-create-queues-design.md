# Auto-create queues — design

## Problem

Queues only exist after running `absurd_sync_queues`. Mandatory manual step before
enqueue or worker works. Clumsy. Forgetting it → `ImproperlyConfigured` /
`UndefinedTable`. Goal: declared queues materialize automatically on first use. Command
no longer required.

## Goal

Declared queues auto-create on demand at two seams (enqueue, worker start). No manual
command needed for normal use. Settings stays source of truth — only queues declared in
`TASKS[...]['QUEUES']` auto-create. Always on, no opt-out.

## Constraints

- Floor Django 6.0 / Python 3.12; psycopg3 backend.
- `absurd.create_queue` already idempotent:
  `INSERT ... ON CONFLICT (queue_name) DO NOTHING` + `ensure_queue_tables`
  (`CREATE TABLE IF NOT EXISTS`). Calling on existing queue = no-op. Concurrent creators
  race harmlessly.
- Enqueue hot path MUST stay fast — zero added cost when queue exists.
- Single source of truth for "declared queues + opts": `get_declared_queues(backend)`.
  Both seams read it. No duplicated validity notion.

## Decisions (locked)

- Both seams auto-create. Always on, no setting.
- Bounded to declared queues. Undeclared queue name → error (typo protection).
- `absurd_sync_queues` command: UNCHANGED. No longer required; optional/explicit.
- Worker start: create + reconcile (mutable policy + storage_mode warning), served queue
  only. Reconcile runs in the `absurd_worker` COMMAND and REPORTS to stdout/stderr
  (Created/Reconciled + warnings), like `absurd_sync_queues`.
- Enqueue: create-only, failure-driven, retry spawn once. NO reconcile (would add DB
  hits + Python on hot path for no gain — create path only ever sees a missing queue).
- System checks: CLEANED UP (see below). W001 dropped (schema-absent is a runtime error,
  not a deploy warning). W002 narrowed to STORAGE_MODE-drift-only (the one condition
  that never self-heals); missing queues + mutable-option drift no longer warn.

## Architecture

Shared knowledge already centralized: `get_declared_queues(backend)`. New work factors
the per-queue create+reconcile out of `sync_queues`'s loop body into a reusable
function.

### New: `reconcile_queue(backend, queue_name) -> SyncResult`

In `queues.py`. Does for ONE queue what `sync_queues` loop body does today:

- `validate_backend(backend.database)` first (clean psycopg3 error standalone;
  schema-absent DB errors below map to `ImproperlyConfigured("run migrate")`).
- `declared = get_declared_queues(backend)`; if `queue_name not in declared` → raise
  `ImproperlyConfigured` (undeclared). Message names queue + points at `TASKS QUEUES`.
- opts = `declared[queue_name]`.
- if queue absent in DB (`Queue.objects.using(db)`): `client.create_queue(name, **opts)`
  → record created.
- else: apply mutable opts via `set_queue_policy`; storage_mode drift → warning. (same
  as today.)
- returns `SyncResult` (created / reconciled / storage_warnings) scoped to one queue.

`sync_queues(backend)` refactors to loop `reconcile_queue` over declared queues, merging
results. External behavior identical — command output unchanged.

### Seam 1 — worker start (create + reconcile, served queue only)

Reconcile lives in the `absurd_worker` COMMAND (not `run_worker`) so it can REPORT to
stdout/stderr. `run_worker` blocks on the event loop and never returns in blocking mode,
so it can't hand a result back — the command must own reconcile + reporting. Bonus:
`run_worker` stays exactly as today (`validate_backend` + `asyncio.run`), so worker.py
barely changes.

Command `handle()` flow (after existing backend/queue resolution at `absurd_worker.py`):

- `reconcile_queue(backend, queue)` wrapped in
  `except ImproperlyConfigured → CommandError` (mapping already in place at
  `absurd_worker.py:97`).
- Report the returned `SyncResult` to stdout (`Created: q` / `Reconciled: q`) and stderr
  (storage_warnings, `style.WARNING`) — REUSE `sync_queues`'s reporting. Extract the
  command's `report_result(prefix, result)` into a shared helper both commands call
  (DRY).
- Then `run_worker(backend, queue, burst=..., options=...)` — UNCHANGED.

`reconcile_queue` itself: calls `validate_backend(backend.database)` first (clean
psycopg3 error standalone), then create-or-reconcile per the Architecture section.

- `aworker_client`: DELETE the `if queue not in provisioned: raise ImproperlyConfigured`
  block. Queue guaranteed to exist by the time the async client runs (command reconciled
  first). Keep aworker_client's schema-absent path as harmless defense — but the
  command's `reconcile_queue` now hits schema-absent first (see below), so it's no
  longer the primary guard.
- **Schema-absent (gap fix):** `reconcile_queue` reads `Queue.objects` + calls
  `create_queue`, both touching the `absurd` schema. Absent schema → Django
  `ProgrammingError` (UndefinedTable/UndefinedFunction). `reconcile_queue` MUST catch
  this and raise
  `ImproperlyConfigured("Absurd schema is not installed. Run: manage.py migrate")`.
  Command maps it → `CommandError`.
- **Undeclared `--queue`:** already rejected by the command at `absurd_worker.py:80`
  (`queue not in backend.queues` → `CommandError` listing valid queues), BEFORE
  reconcile. UNCHANGED. `reconcile_queue`'s own undeclared-raise is defense-in-depth /
  for programmatic callers; not the worker UX path.

### Seam 2 — enqueue (create-only, failure-driven, retry once)

`backends.py` `enqueue`. Happy path untouched — auto-create lives only in `except`.

```
try:
    with atomic(savepoint=True):
        spawn_result = client.spawn(...)
except (UndefinedTable, UndefinedFunction, InvalidSchemaName):
    declared = get_declared_queues(self)            # NB self is the backend
    if task.queue_name not in declared:
        # "Queue 'X' is not declared in this backend's TASKS QUEUES. Add it to QUEUES
        #  (or fix the queue name) — only declared queues are auto-created."
        raise ImproperlyConfigured(undeclared_msg) from None
    try:
        client.create_queue(task.queue_name, **declared[task.queue_name])
    except (UndefinedFunction, InvalidSchemaName):  # schema absent — create can't help
        # "Absurd schema is not installed. Run: manage.py migrate"
        raise ImproperlyConfigured(schema_absent_msg) from None
    with atomic(savepoint=True):                    # retry ONCE
        spawn_result = client.spawn(...)
```

- Failed `spawn` inside savepoint → savepoint rollback on except → outer `atomic()`
  still usable. Then create (DDL; participates in current txn — fine, transactional DDL,
  idempotent). Then retry spawn in fresh savepoint.
- Create uses declared opts → `storage_mode` correct from birth.
- Retry once only. Second failure propagates (not swallowed).
- `create_queue` uses the existing sync client (`build_absurd_client(self.database)`,
  Django connection). No new connection.

Note: `get_declared_queues` takes a backend; in `enqueue`, `self` is the backend.

## Errors removed / changed

- Enqueue old `ImproperlyConfigured("... run absurd_sync_queues ...")` → REPLACED by:
  undeclared-queue error, OR schema-absent error (`run migrate`). No "run sync_queues".
- Worker `ImproperlyConfigured("Queue X not provisioned. Run absurd_sync_queues")` →
  GONE (auto-reconciled).

## System checks cleanup

`check_absurd_config` (E001–E005) — UNTOUCHED. Only the DB-level
`check_absurd_queue_state` → `query_queue_state` changes.

Why now: auto-create makes two of the queue-state warnings obsolete.

- **W001 (schema not migrated) — DROP entirely.** Remove the check branch + `W001_MSG`/
  `W001_HINT`. Rationale: schema-absent is an ERROR, not a warning — it's now surfaced
  loudly at runtime (enqueue + worker → `ImproperlyConfigured("run migrate")`). The
  warning's only non-noisy surface was `check --database`; during `migrate` it nagged
  you to run the command in progress. `query_queue_state` folds `ProgrammingError`
  (schema-absent) into the SAME silent path as `OperationalError` → returns `[]`.
- **W002 — NARROW to STORAGE_MODE-drift-only.** The only condition that never self-heals
  (storage_mode is immutable; reconcile/worker/command can only warn). Drop the
  missing-queue trigger (self-heals) AND the mutable-option-drift trigger (self-heals on
  next worker boot / command). W002 now fires ONLY when an EXISTING queue's declared
  `storage_mode` differs from the DB.
  - New `drift` computation:
    `[name for name in declared if name in actual and declared[name].get("storage_mode") and declared[name]["storage_mode"] != actual[name].storage_mode]`.
  - **Reword** msg/hint (the W002 tests get rewritten anyway, so favor clarity): msg
    `"django-absurd: a queue's declared storage_mode differs from the database (storage_mode is immutable)."`;
    hint `"Recreate the queue, or revert the declared storage_mode. Affected: <names>"`.
    id stays `absurd.W002`.
  - **Dead code to DELETE** (now unused): `has_option_drifted()`, `parse_interval()`
    (removes a per-interval DB round-trip), `DURATION_OPTION_KEYS`,
    `SCALAR_OPTION_KEYS`. NOTE: `MUTABLE_OPTION_KEYS` in `queues.py` STAYS — that's
    `set_queue_policy`'s reconcile list, unrelated to the check.
  - `query_queue_state` collapses:
    `except (OperationalError, ProgrammingError): return []` (schema-absent now silent,
    folded with unreachable); compute storage_mode drift; W002 if any.

Resulting `query_queue_state` cases: DB unreachable → `[]`; schema absent → `[]`; queue
missing → `[]` (was W002); queue exists + mutable drift → `[]` (was W002); queue
exists + storage_mode drift → W002; all match → `[]`.

## Testing (real DB, no mocks)

- Enqueue to declared-but-unprovisioned queue → task runs end-to-end (auto-created).
- Enqueue to UNDECLARED queue → `ImproperlyConfigured`, message names queue.
- Enqueue with schema absent (drop schema) → schema-absent error (`migrate`), not
  silent.
- Enqueue inside outer `atomic()` that auto-creates then retries → outer txn survives,
  task persists.
- Worker start on declared-but-unprovisioned queue → drains end-to-end (no command run).
- Worker command on unprovisioned queue → stdout reports `Created: <queue>` (capsys); on
  already-provisioned queue → stdout reports `Reconciled: <queue>`.
- Worker command, storage_mode drift on existing queue → stderr emits the storage
  warning (capsys), worker still starts.
- Worker command schema-absent (drop schema) → `CommandError` mentioning migrate.
- Worker start, undeclared `--queue` → `CommandError`.
- `reconcile_queue` idempotency: call twice → second is no-op (created once).
- `reconcile_queue` policy reconcile: change mutable opt → applied; change storage_mode
  on existing queue → storage_warning surfaced (worker logs it).
- `sync_queues` regression: existing command tests still pass (refactor
  behavior-neutral).
- Enqueue happy path (queue exists) makes NO extra queue-existence query — assert via
  existing fast-path tests still green (no new round-trip added).

### Existing tests that INVERT or change (audit + repoint)

Auto-create flips current "unprovisioned → error" assertions. Plan must address:

- `test_worker.py::test_command_maps_improperly_configured_to_commanderror` — premise
  (declared-but-unsynced queue → CommandError) now AUTO-CREATES → drains. Repoint the
  ImproperlyConfigured→CommandError mapping test to a SCHEMA-ABSENT trigger (drop
  schema, declared queue → CommandError mentioning migrate).
- `test_worker.py::test_worker_client_unprovisioned_queue_errors` — asserts
  `aworker_client` raises on an unprovisioned queue; that block is deleted. Remove or
  repoint (e.g. fold into the run_worker auto-create-drains test).
- `test_worker.py::test_worker_client_absent_schema_errors` — calls `aworker_client`
  directly; STAYS valid (aworker_client defense block kept). Add a parallel run_worker /
  command-level schema-absent test for the reconcile_queue path.
- `test_enqueue.py::test_enqueue_to_unprovisioned_queue_raises_clear_error` — uses
  declared `default` → now auto-creates → INVERTS. Repoint to an UNDECLARED queue (e.g.
  `add.using(queue_name="ghost").enqueue(...)` where ghost ∉ QUEUES) →
  ImproperlyConfigured naming the queue. Add a sibling: declared-unprovisioned `default`
  → enqueue succeeds.
- `test_enqueue.py::test_enqueue_error_does_not_poison_atomic_block` — premise (spawn
  raises on unprovisioned declared queue) no longer holds; REFRAME to: enqueue inside
  `atomic()` that auto-creates-then-retries → outer txn usable + task persists
  (savepoint still the thing under test, now guarding the retry path).
- `test_enqueue.py::test_enqueue_with_absent_schema_raises_clear_error` — STAYS GREEN:
  spawn fails → declared → `create_queue` also fails (schema absent) → mapped to
  `ImproperlyConfigured("migrate")`. Same assertion, new code path. Keep.

Check tests (`tests/test_checks.py`) — W002 is now STORAGE_MODE-drift-only:

- `test_schema_absent_warns_migrate_first` — W001 dropped → REMOVE (or repoint to assert
  the check is SILENT on schema-absent: no W001/W002 emitted).
- `test_drift_warns_run_sync` — misnamed; declares a MISSING queue, expects W002.
  Missing no longer warns → INVERTS. Repoint to assert a declared-but-missing queue
  emits NO W002 (rename, e.g. `test_missing_declared_queue_no_longer_warns`).
- `test_option_drift_warns` (cleanup_limit) — mutable drift no longer warns → INVERTS.
  Repoint to assert NO W002 on mutable drift.
- `test_duration_drift_warns` (cleanup_ttl) — mutable drift no longer warns → INVERTS.
  Repoint to assert NO W002 on mutable drift.
- `test_mixed_missing_and_drifted_hint_names_both` — missing + mutable drift; NEITHER
  fires now → INVERTS fully. Remove or repoint to NO-W002.
- **NEW** `test_storage_mode_drift_warns` — create queue unpartitioned, then declare
  `storage_mode="partitioned"` → W002 with the new storage_mode message + queue name.
- `test_db_unreachable_is_silent` — STAYS; drop its now-dead reference to the W001
  message string (assert no W002 only).
- config-check tests (E001/E002/E003/E004/E005) — UNTOUCHED.

Task-type note: auto-create is task-type-AGNOSTIC (create fires on missing queue tables
/ worker boot, never sees sync-vs-async). Do NOT parametrize auto-create tests
sync×async — use a sync task. One async task through a worker-start test is optional
belt-and-braces, not required.

## Out of scope

- Auto-create for undeclared queues.
- Per-backend opt-out setting.
- Reconcile at enqueue.
