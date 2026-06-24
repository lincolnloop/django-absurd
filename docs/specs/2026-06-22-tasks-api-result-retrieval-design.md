# django-absurd — Spec: result retrieval (get_result) (SP6)

Date: 2026-06-22 Status: approved-for-planning

Implement `AbsurdBackend.get_result` / `aget_result`; flip `supports_get_result = True`.
SP2 shipped `supports_get_result = False` (both raise `NotImplementedError`) because
Absurd's read API returns only `{state, result, failure}` while Django's `TaskResult` is
a fat, self-describing object. SP6 closes the gap by reading Absurd's own durable task
rows.

## Why it's non-trivial (impedance mismatch)

Direct Absurd SDK: `fetch_task_result(task_id, queue_name)` — caller passes the queue,
gets a thin `{state, result, failure}`, holds any other context themselves. Django:
`get_result(result_id)` takes ONE opaque id and must return a full `TaskResult` (task,
args, kwargs, status, timestamps, return_value, errors). Two consequences:

1. One opaque id → nowhere for the queue → we ENCODE it in the id (we know it at
   enqueue).
2. Django wants the rich object → SDK snapshot is thin → we READ Absurd's task/run
   tables.

`TaskResult.refresh()` (verified) calls `get_backend().get_result(self.id)` with ONLY
`self.id`, and copies back just 8 VOLATILE attrs: `status`, `_return_value`, `errors`,
`enqueued_at`, `started_at`, `finished_at`, `last_attempted_at`, `worker_ids`.
`task`/`args`/ `kwargs` are NOT overwritten by refresh — they matter only for a cold
`get_result(id)`.

`TaskResult.id` is contract-free (`id: str`, no format/length validation; reference
`ImmediateBackend`/`DummyBackend` use `get_random_string(32)`), so we may mint any
opaque handle. We already mint it (`str(task_id)` since SP2).

## Storage: nothing new

No Django model, no migration, no extra write at enqueue. Absurd already persists every
task in `absurd.t_<queue>` (written by `spawn_task` at enqueue, UPDATEd by the worker)
and runs in `absurd.r_<queue>`. `get_result` reads those rows. The `queue:task_id`
handle is a pure RUNTIME string on `TaskResult.id` — never persisted by us.

## Id encoding (enqueue change)

`enqueue` sets `result.id = f"{task.queue_name}:{spawn_result['task_id']}"` (was
`str(task_id)`). Decode in `get_result` with **`result_id.rsplit(":", 1)`** →
`(queue, task_id)`: Absurd queue names are validated only for non-empty + ≤57 bytes (NO
character restriction — a colon is legal), but the trailing segment is always the
36-char uuid, which never contains `:`, so right-split is unambiguous. Max size ≤ 57 +
1 + 36 = 94 bytes; not persisted, so no `max_length` concern. (Minor breaking change to
a young API: `result.id` was the raw uuid. Idempotency dedup test still holds —
`r1.id == r2.id`.)

## `get_result(result_id)` (raw SQL, `supports_get_result = True`)

`queue` is parsed from a CALLER-SUPPLIED `result_id`, so it is UNTRUSTED. TWO guards:

- **Whitelist (early reject):** if the parsed `queue` is not in the backend's declared
  queues (`self.queues`, when set), raise `TaskResultDoesNotExist` before touching SQL —
  a real id we minted always targets a declared queue.
- **Safe identifier quoting:** build table names with `psycopg.sql.Identifier` (NOT
  Django's `connection.ops.quote_name`, which does `'"%s"' % name` and does NOT escape
  embedded quotes — unsafe for untrusted input).
  `sql.Identifier("absurd", f"t_{queue}")` → `"absurd"."t_…"` with embedded quotes
  doubled.

1. `queue, task_id = result_id.rsplit(":", 1)`; malformed (no `:`) →
   `TaskResultDoesNotExist`. Whitelist-check `queue` (above).
2. `connection = connections[self.database]`. `connection.ensure_connection()`, then
   `register_jsonb_loader(connection.connection)` (existing helper, on the underlying
   psycopg conn — REQUIRED: verified live that Django's psycopg3 backend does NOT decode
   these jsonb columns to Python on its own; without it `params`/`completed_payload`/
   `failure_reason` come back as raw strings). Do NOTHING between register and execute
   that could reconnect (a new psycopg conn loses the loader). The worker calls
   `close_old_connections()`, so a test must survive a worker-induced reconnect.
3. Run ONE query, wrapped in a NESTED savepoint so a DB error can't poison an enclosing
   `transaction.atomic()` (REQUIRED — verified: through Django's cursor a missing table
   aborts the outer transaction):
   `with transaction.atomic(using=self.database, savepoint=True): cursor.execute(...)`.
   Build the statement with
   `psycopg.sql.SQL(template).format(t=sql.Identifier("absurd", f"t_{queue}"), r=sql.Identifier("absurd", f"r_{queue}"))`
   — the TWO-arg `Identifier` supplies the `absurd` schema, so the template must NOT
   also write a literal `absurd.` prefix (that yields `absurd."absurd"."t_…"` →
   `cross-database references` error):

   ```
   SELECT t.task_name, t.params, t.enqueue_at, t.first_started_at, t.state,
          t.completed_payload, t.cancelled_at,
          lr.started_at AS run_started, lr.completed_at, lr.failed_at, lr.failure_reason,
          (SELECT array_agg(r.claimed_by ORDER BY r.attempt)
             FROM {r} r
            WHERE r.task_id = t.task_id AND r.claimed_by IS NOT NULL) AS worker_ids
     FROM {t} t
     LEFT JOIN {r} lr ON lr.run_id = t.last_attempt_run
    WHERE t.task_id = %s
   ```

   Render with `.as_string(connection.connection)`, then
   `cursor.execute(rendered, [task_id])` (`task_id` stays a `%s` bound param).

4. No row → `TaskResultDoesNotExist`. Through Django's cursor wrapper, a missing table /
   absent schema surface as `django.db.utils.ProgrammingError` (NOT the raw
   `psycopg.errors.UndefinedTable` — verified; the raw classes never match here). Catch
   `django.db.utils.ProgrammingError`: unknown-queue / garbage-id table-missing →
   `TaskResultDoesNotExist`; absent absurd schema → `ImproperlyConfigured` (consistent
   with `enqueue`). Inspect `pgcode`/`__cause__` to distinguish if needed
   (`UndefinedTable` 42P01 vs `InvalidSchemaName` 3F000 / `UndefinedFunction` 42883).
   The savepoint (step 3) means the outer transaction stays usable after the catch.
5. Assemble `TaskResult` (see mapping).

### Field mapping

| TaskResult field    | Source                                                                                                            |
| ------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `id`                | the original `result_id`                                                                                          |
| `backend`           | `self.alias`                                                                                                      |
| `task`              | `import_string(task_name)`, then `.using(queue_name=queue)` if it differs (see ImportError note)                  |
| `args` / `kwargs`   | `params["args"]` / `params["kwargs"]`                                                                             |
| `status`            | map `state` (below)                                                                                               |
| `enqueued_at`       | `t.enqueue_at`                                                                                                    |
| `started_at`        | `t.first_started_at`                                                                                              |
| `last_attempted_at` | `lr.run_started` (last_attempt_run's `started_at`)                                                                |
| `finished_at`       | terminal time: `lr.completed_at` ?? `lr.failed_at` ?? `t.cancelled_at` (else `None`)                              |
| `_return_value`     | `t.completed_payload` when `state == 'completed'` (set via `object.__setattr__`, it's `init=False`)               |
| `errors`            | `failure_reason` → one `TaskError` when `state == 'failed'` (below); else `[]`                                    |
| `worker_ids`        | `worker_ids` array — `array_agg(r.claimed_by ORDER BY r.attempt)` (one entry per run, NOT distinct); `[]` if none |

**`worker_ids` / `.attempts` consistency (I3):**
`TaskResult.attempts == len(worker_ids)` (Django). The live SP3 worker sets the
in-process `TaskContext.worker_ids = ["absurd"] * attempt`, but what's PERSISTED is
`r.claimed_by` (value `'worker'`, the SDK default), one run row per attempt. So
`get_result` aggregates the run rows WITHOUT `DISTINCT`
(`array_agg(r.claimed_by ORDER BY r.attempt)`) → one entry per attempt, so
`len(worker_ids)` tracks attempt count; `DISTINCT` would collapse retries to 1. The
values differ from the live context (`'worker'` vs `'absurd'`) — only the length/meaning
is consistent. Tests assert `worker_ids` non-empty, not exact values.

**Removed/renamed task (I2):** `import_string(task_name)` raises `ImportError` if the
task's module/attr no longer exists (deploy skew). `get_result` catches it →
`ImproperlyConfigured` ("task '<name>' is no longer importable"). We cannot build
`TaskResult.task` without the function, so this is the honest failure (NOT
`TaskResultDoesNotExist` — the result DOES exist in Absurd).

### State → status

`pending → READY`, `running → RUNNING`, `sleeping → RUNNING`, `completed → SUCCESSFUL`,
`failed → FAILED`, `cancelled → FAILED` (Django `TaskResultStatus` has no CANCELLED).

### errors → TaskError

Absurd `failure_reason` jsonb (from the SDK's `_serialize_error`) =
`{"name": <class name>, "message": <str>, "traceback": <formatted str | null>}` (or just
`{"message": ...}`). Map to
`TaskError(exception_class_path=fr.get("name", ""), traceback=fr.get("traceback") or fr.get("message", ""))`.
LIMITATION: Absurd stores the exception CLASS NAME, not a dotted import path, so
`exception_class_path` is the bare name — documented, best-effort.

### `_return_value`

`TaskResult._return_value` is `field(init=False)`, so it can't be passed to the
constructor; set it after construction with
`object.__setattr__(result, "_return_value", payload)` (the dataclass is frozen — same
pattern Django uses).

## `aget_result`

`aget_result = sync_to_async(self.get_result)` (matches the SP2 `aenqueue` pattern).
Native async read (`AsyncAbsurd` / `await_task_result` blocking poll) is out of scope.

## Coupling note

`get_result` reads Absurd's INTERNAL schema (`t_<queue>`/`r_<queue>` column names) —
chosen over a Django read-model to avoid a new table + a write per enqueue. The schema
is pinned: we ship the `absurdctl` 0.4.0 SQL as our migration. If the pinned Absurd
version changes the task table shape, `get_result` must be revisited (a focused,
single-function blast radius).

## Testing (pytest, function-based, real Postgres; integration-led)

Drive `enqueue` / the worker, then `get_result` / `result.refresh()`; assert on the real
reconstructed `TaskResult`. Reuse `tests/tasks.py` (`add`, `boom`, `make_group`) +
`run_absurd_worker` (SP3 burst).

- **pending result:** `r = add.enqueue(2, 3)`; `got = backend.get_result(r.id)`; assert
  `got.status == READY`, `got.args == [2, 3]`, `got.kwargs == {}`, `got.enqueued_at`
  set, `got.task.module_path == "tests.tasks.add"`, `got.id == r.id`.
- **successful result + return_value:** enqueue `add`, `run_absurd_worker()`,
  `get_result`; assert `status == SUCCESSFUL`, `_return_value == 5`,
  `finished_at`/`last_attempted_at` set, `worker_ids` non-empty.
- **refresh() round-trip:** `r = add.enqueue(2, 3)`; `run_absurd_worker()`;
  `r.refresh()`; assert `r.status == SUCCESSFUL` and `r.return_value == 5` (the volatile
  attrs copied back).
- **failed result → errors:** enqueue `boom`, run worker (allow attempts to exhaust),
  `get_result`; assert `status == FAILED`, `errors` has one `TaskError` with
  `exception_class_path` containing `"ValueError"` and a non-empty `traceback`.
- **unknown / malformed id:** `get_result("default:<random-uuid>")` and
  `get_result("nocolon")` both raise `TaskResultDoesNotExist`.
- **queue-with-colon parse:** unit-level — a `result_id` like `"a:b:<uuid>"` decodes to
  `queue == "a:b"`, `task_id == "<uuid>"` via `rsplit(":", 1)`.
- **aget_result:** `asyncio.run(backend.aget_result(r.id))` returns the same as
  `get_result`.
- **injection safety:** a `result_id` whose queue segment contains SQL metacharacters
  does not execute it (identifier-quoted) — surfaces as `TaskResultDoesNotExist`, not an
  error or injection.
- **get_result inside `atomic()` doesn't poison the txn:** call `get_result` with a
  garbage id inside `with transaction.atomic():`, catch the `TaskResultDoesNotExist`,
  then run another ORM query in the SAME block — it must succeed (proves the savepoint
  works).
- **removed-task → ImproperlyConfigured (I2):** a `result_id` for a task whose
  `module_path` no longer imports raises `ImproperlyConfigured` (simulate with a
  `result_id` whose stored `task_name` is a now-missing dotted path, or a task module
  that isn't importable).
- **via Task.get_result (M6):** `r = add.enqueue(2, 3)`; `add.get_result(r.id)` (the
  public path) returns without tripping Django's `TaskResultMismatch` — i.e. the
  reconstructed `task.func` matches `add.func`.
- **C3 — id-change blast radius (MUST fix in this work):** changing `result.id` to
  `queue:uuid` breaks `tests/test_worker.py` — its `snapshot(task_id)` helper feeds
  `result.id` straight into `client.fetch_task_result(task_id)` (typed `uuid`). Update
  the `snapshot` helper to accept the composite id and `rsplit(":", 1)` the uuid (or
  update the 6 call sites: `test_worker.py:94,103,110,117,154,210`). Also
  `tests/test_enqueue.py` asserts on `result.id` — update those.
  `examples/.../enqueue_demo.py` only prints `.id` (cosmetic, leave). The plan MUST
  enumerate these, not hand-wave "update any that asserted...".

No mocks; real DB: `PGPORT=5433 docker compose up -d db`, run with `PGPORT=5433`.

## Out of scope (deferred)

Native async read (`AsyncAbsurd`/`await_task_result`); blocking/await-until-terminal; a
Django read-model; multi-DB; the admin-browse-via-unmanaged-models sub-project; the
other deferred follow-ups (json-loader home, ALWAYS_EAGER, public register hook). NOTE:
the enqueue-savepoint follow-up (deferred item 3) is NO LONGER fully deferred — SP6 adds
a savepoint around the `get_result` read (C2); `enqueue`'s own savepoint remains
separate.
