# Durable test clock — `absurd` fixture (advance time, emit, drain, inspect)

Issue: <https://github.com/lincolnloop/django-absurd/issues/108>

## Problem

Durable primitives untestable deterministically. Today `tests/core/test_durable.py` pins
a wall-clock recipe: task sleeps ~1.5s, test does `time.sleep(2)`, drain again. Slow
(~8s across 4 tests), flake-prone, and can't express a 7-day sleep at all. `await_event`
timeouts worse — nothing to wait on but wall clock.

## Two clocks, not one

A sleeping run is gated twice:

1. **Postgres** — claim predicate `r_<q>.available_at <= absurd.current_time()`.
   `absurd.current_time()` (shipped in
   `django_absurd/migrations/0001_initial_0_4_0.sql:38`, Absurd's own function, used in
   21 places) returns `clock_timestamp()` unless session GUC `absurd.fake_now` is set.
   Upstream built it for tests.
2. **Python SDK** — `sleep_until` recomputes on replay and re-suspends when
   `_get_current_time() < actual_wake_at` (`absurd_sdk/__init__.py:787`).

Both must move together. Moving only the DB clock does NOT benignly fail: run gets
claimed, SDK re-suspends, `schedule_run` re-arms same `wake_at`, drain never ends.

Measured during review: with a SYNC sleeping task the drain spins and, when
pytest-timeout fires, deadlocks permanently in the executor's thread-join — unkillable
except by SIGKILL, with zero test output. (An async task IS rescued by pytest-timeout,
so the blast radius depends on task flavor.) The SIGKILL then strands both the database
GUC and the sleeping rows, and the poisoned reused DB hangs the next run's first
draining test.

Hence three design consequences: no DB-only knob; advancing is one operation over both
clocks; and recovery cannot rely on per-test teardown alone — a session-start sweep is
required, because pytest-randomly can schedule the poisoned test before any resetting
test runs (see [Isolation](#isolation)).

Python-ahead-of-DB is the safe direction (run merely not claimed yet).
DB-ahead-of-Python is the deadlock. Design must never produce the latter — which also
fixes the apply order: **Python clock first, then the DB literal**. A failure between
the two lands Python-ahead (benign); DB-first would land in the deadlock direction.

## Mechanism

- DB side: `absurd.fake_now` set at **database** level
  (`ALTER DATABASE <test_db> SET absurd.fake_now = '<iso>'`). Session-level `SET` on
  Django's connection is not enough — worker opens its OWN connection per drain
  (`django_absurd/worker.py:171`). DB-level default is inherited by every new session,
  and the worker connects fresh each drain. Proven: 7-day sleep resumes after one
  advance. Outlives the test if not undone — reset is per-test and unconditional, see
  [Isolation](#isolation).
- `ALTER DATABASE` rejects bind params (`syntax error at or near "$1"`) → compose with
  `psycopg.sql.Identifier`/`Literal` on a dedicated autocommit connection (same shape as
  `django_absurd/connection.py:open_central_connection`).
- Gap: DB-level default only reaches NEW sessions. Django's live connection would stamp
  real time on a post-advance `enqueue()`. So advancing also issues a session-level
  `SET` on the Absurd alias' open connection.
- Python side: **time-machine**, `tick=False`, driven via
  `move_to(<absolute datetime>)`.
- `absurd.fake_now` is a static literal → Postgres is FROZEN once set, never ticks.
  Therefore Python must be frozen too. `tick=False` is a consistency requirement, not a
  preference: both sides pinned to the same instant, durable time moves only when the
  test says so.
- Before any freeze/advance, nothing is set — both sides run real clocks and agree.

### The literal must be aware UTC (deadlock path if not)

`absurd.current_time()` casts the GUC text using the **reading session's** `TimeZone`.
The worker's connection is raw `psycopg.connect(**params)`
(`django_absurd/worker.py:171`), so its `TimeZone` is the **server default**, not
Django's `UTC`. time-machine treats a naive datetime as UTC. On a server defaulting west
of UTC, a naive `freeze_at` therefore puts the DB AHEAD of Python — the deadlock
direction. Today it only works because the container defaults to `Etc/UTC`.

Proven live, `absurd.current_time()` against a naive literal vs the same instant written
with an offset:

| Literal             | Session zone     | Reads as                 | Correct instant |
| ------------------- | ---------------- | ------------------------ | --------------- |
| `2030-01-01T12:00Z` | America/New_York | `2030-01-01 07:00:00-05` | yes             |
| `2030-01-01T12:00Z` | Asia/Tokyo       | `2030-01-01 21:00:00+09` | yes             |
| `2030-01-01T12:00`  | America/New_York | `2030-01-01 12:00:00-05` | NO              |
| `2030-01-01T12:00`  | Asia/Tokyo       | `2030-01-01 12:00:00+09` | NO              |

With the offset the session zone changes only the rendering; without it, the same text
is a different instant per session.

Rules, non-negotiable:

- `freeze_at` takes an aware `datetime`; a naive one raises a curated error telling the
  caller to attach `tzinfo` (wrong-door case: the value is ambiguous and the failure
  would land far from its cause). No guessing from Django's current timezone — a wrong
  guess is the deadlock direction, and `tzinfo=dt.UTC` is a one-token fix.
- On the WRITE side, normalize with `astimezone(dt.UTC)` and always write the literal
  WITH its offset, so the cast can never depend on the reading session's `TimeZone`.
  Reads are pinned to UTC for the same reason — see [Reads](#reads).
- Any aware zone is accepted; a Chicago-zoned datetime is just an instant. Cron math is
  unaffected: `get_next_datetime` interprets schedules in Django's `TIME_ZONE`
  regardless of how the instant was spelled.
- Never accept a string. A malformed literal is accepted by `ALTER DATABASE` and then
  fails inside `absurd."current_time"()` on every NEW session
  (`invalid input syntax for type timestamp with time zone`) — i.e. far from the call
  that caused it. `datetime`-only makes that unreachable.

### What a freeze does NOT reach: the two schedulers

Three time interpreters exist in this project; the freeze moves two of them.

- **Durable deadlines** (`absurd.current_time()`) — moved. The feature.
- **beat's slot math** — moved incidentally: `get_next_datetime` works from
  `timezone.localtime(timezone.now())` (`django_absurd/scheduler.py:37`), i.e. Python's
  clock, which time-machine freezes. Cron expressions are interpreted in Django's
  `TIME_ZONE`. Existing beat tests inject `run_beat`'s `now`/`wait` seams, so they are
  unaffected either way.
- **pg_cron's launcher** — NOT moved. It runs in the central `cron.database_name`
  database on its own clock and interprets schedules in the `cron.timezone` GUC (default
  `GMT`, independent of the server's `TimeZone`; see
  [Timezone](../web/cron-jobs.md#timezone)). A database-level GUC on the TEST database
  cannot reach it.

Consequence to document: advancing durable time cannot make a pg_cron schedule fire.
Testing that stays "reconcile, then inspect `cron.job`", as `tests/pg_cron` already
does.

### Subprocess workers are only half-frozen

`ALTER DATABASE` reaches a `manage.py absurd_worker` subprocess, but its Python clock is
real. Frozen-time-ahead-of-real then IS the deadlock condition. The fixture drives the
in-process burst worker only; a subprocess worker under a freeze is out of scope and
must be called out in the docs.

### Dependency

time-machine is NOT bundled and NOT an extra. `drain`/`emit`/`get_result`/`now` work
without it.

Detection = a lazy `import time_machine` in the apply path, i.e. on the first
`freeze_at`/`advance` call, with `ImportError` translated to `ImproperlyConfigured`
naming the install command (same shape as the guards in `django_absurd/events.py`). Not
`importlib.util.find_spec` — a module can be found and still fail to import, and the
real import is the thing we need to succeed. Not a fixture-setup check either: a
`drain`-only test must not require the package. Python caches the module, so only the
first advance pays the import.

## Surface

> **Superseded in part.** Sections below that describe `freeze_at`/`advance`, the
> "advance before any freeze" rule, `run_burst_worker`'s tuple return, or the
> `absurd_drain_queue` delegate record the FIRST build. The fixture also shipped as
> `dj_absurd`, not `absurd`, reserving the `dj_` prefix the way `DjangoAbsurdError` does
> — read every `absurd` fixture reference below as `dj_absurd`. The
> [post-review redesign](#post-review-redesign-human-revdiff-2026-07-29) below
> supersedes them; where the two disagree, that section wins.

One facade fixture, `absurd`, returning a test-runtime object:

| Member                                      | Behavior                                       |
| ------------------------------------------- | ---------------------------------------------- |
| `freeze_time(dt=None)`                      | context manager; pins both clocks (None = now) |
| `clock.move_to(dt)` / `clock.shift(Δ)`      | on the yielded handle; the only movers         |
| `drain(queue="default")`                    | burst-drain; returns `list[RunSnapshot]`       |
| (`absurd_drain_queue` is deleted, not kept) | one way in                                     |
| `emit(name, payload=None, queue="default")` | delegate to `emit_event`                       |
| `get_result(task_id, queue="default")`      | `TaskSnapshot \| None`                         |
| `now`                                       | virtual now, aware, as Postgres reports it     |

### Two typed records, ours — not the SDK's and not Django's

`drain()` reports what RUNS did; `get_result()` reports where a TASK stands. Conflating
the two is what makes a snapshot lie (see the caveats below), so they are separate
frozen, fully annotated dataclasses.

`RunSnapshot` — one per run executed, in claim order:

| Field       | Source                                          |
| ----------- | ----------------------------------------------- |
| `queue`     | the drained queue                               |
| `run_id`    | claim row                                       |
| `task_id`   | claim row                                       |
| `task_name` | claim row                                       |
| `args`      | decoded from the claim row's `params["args"]`   |
| `kwargs`    | decoded from the claim row's `params["kwargs"]` |
| `attempt`   | claim row — THIS run's attempt number           |
| `state`     | THIS run's own `r_<q>.state` after execution    |
| `result`    | `r_<q>.result` of this run                      |
| `failure`   | `r_<q>.failure_reason` of this run              |

Per-run state is what makes the record honest: a run that suspended on a durable sleep
reads `sleeping`; a run that raised reads `failed` WITH its own `failure_reason`; a
retry sequence reads attempt-by-attempt instead of collapsing to one final verdict.
Identity and `attempt` ride along free from the claim select (the SDK's `ClaimedTask`
carries `run_id`/`task_id`/`task_name`/`attempt`/`params`); only each run's outcome
costs a read.

`TaskSnapshot` — where one task stands, returned by `get_result`:

| Field         | Source                                                                             |
| ------------- | ---------------------------------------------------------------------------------- |
| `queue`       | the queried queue                                                                  |
| `task_id`     | `t_<q>.task_id`                                                                    |
| `task_name`   | `t_<q>.task_name`                                                                  |
| `args`        | decoded from `t_<q>.params["args"]`                                                |
| `kwargs`      | decoded from `t_<q>.params["kwargs"]`                                              |
| `state`       | raw durable state: `pending`/`running`/`sleeping`/`completed`/`failed`/`cancelled` |
| `attempts`    | `t_<q>.attempts`                                                                   |
| `enqueued_at` | `t_<q>.enqueue_at`, UTC-aware (see [Reads](#reads))                                |
| `result`      | `t_<q>.completed_payload`                                                          |
| `failure`     | `r_<q>.failure_reason` of `last_attempt_run`                                       |

`enqueued_at` earns its place: "did `enqueue()` actually stamp fake time?" is
load-bearing for this design, and without the field it is assertable only by reaching
into raw SQL — the practice `get_result` exists to end.

Populated by one query per task against `t_<q>`, left-joining `r_<q>` on
`last_attempt_run` for `failure_reason`, on a fresh UTC-pinned connection with
`register_jsonb_loader` applied (see [Reads](#reads)). That constructs per-queue table
names — the same coupling `django_absurd/flush.py:truncate_queue_tables` already
carries.

#### `TaskSnapshot` caveats, documented on the dataclass

A task-level view cannot express an in-flight retry. Measured: one failed attempt with
an hour's backoff pending reads `state=sleeping attempts=2 failure=None`, and all three
mislead.

- **`attempts` counts attempts CREATED, not completed.** `fail_run` records the next
  attempt immediately, so it reads N+1 before attempt N+1 has run. A never-run pending
  or cancelled task reads `1`.
- **`state="sleeping"` covers BOTH a durable sleep and a retry backoff.** A test
  asserting "my workflow is asleep" would pass on a task that actually crashed. The
  drain's `RunSnapshot` distinguishes them.
- **`failure` is the last attempt's `failure_reason` only when no newer run exists**
  (exhausted retries, terminal failure). Mid-backoff, `last_attempt_run` already points
  at the fresh pending run, so `failure` is `None` and attempt N's error is invisible
  here.
- `max_delay`-cancelled tasks carry no `failure_reason` at all, so `failure=None` —
  which matches the SDK.

#### Decoding rules

- The snapshot query runs on a connection with `register_jsonb_loader` applied. Proven:
  on a plain psycopg connection every jsonb column comes back as a raw **string**
  (`'{"args": [], "kwargs": {}}'`), which would silently type `result` as `str`.
  `tests/utils.py:get_task_result` already does this.
- `params` decoding is defensive. Our `enqueue()` always writes both keys, even for a
  zero-arg task, and schedules and task-spawned children take the same path — but a
  queue shared with raw-SDK producers can hold anything (proven: `params` as a bare
  list). Fall back to `args=[]`/`kwargs={}` rather than raising, or one foreign row
  takes down the entire `drain()` call.

Why our own types rather than Django's `TaskResult`, the SDK's `TaskResultSnapshot`, or
the admin pseudo-models: Django's status vocabulary folds `sleeping → RUNNING` and
`cancelled → FAILED`, the SDK type has no identity, and the pseudo-models type as `Any`
and need `rebuild_views()`. Full reasoning in the
[appendix](#appendix-rejected-alternatives).

#### Clock operations

- `advance` takes `timedelta` only. No seconds/float overload, no datetime union —
  `freeze_at` owns absolute.
- `advance` before any `freeze_at` freezes at real-now-plus-Δ. It does NOT error: the
  first advance is the freeze, and every existing row's deadline is still interpreted
  against the same instant.
- Both funnel into one internal apply: set virtual now → `move_to` + DB literal +
  session `SET`. `advance` = apply(virtual_now + Δ).
- No jump-to-next-deadline. Test supplies the amount. Multi-wait workflows are driven
  explicitly (drain → advance → emit → drain), so every wait is visible in the test and
  a wrong advance count fails loudly.

#### Reads

`now` reads `select absurd.current_time()`; the snapshot queries read `t_<q>`/`r_<q>`.
All of them use a FRESH connection built from `get_connection_params()` with
`cursor_factory` AND `context` removed, `SET TIME ZONE 'UTC'` issued immediately after
connect, and `register_jsonb_loader` applied. Every facade datetime is therefore
UTC-aware by construction — no `astimezone` on values, no server-dependent expression.

The UTC pin is not cosmetic. Django's connection params carry its psycopg adapters
(`context`), whose timestamptz loader does `replace(tzinfo=...)` — a relabel that is
only correct when the session TimeZone is already UTC. Inherited unpinned, a non-UTC
server default hands back wall-clock digits mislabeled UTC: proven, the instant
`12:00 UTC` read back as `07:00+00:00` on an `America/Chicago` session, for `now` and
`enqueued_at` alike. Dropping `context` also sidesteps `USE_TZ=False`, where Django's
loader strips tzinfo and returns naive datetimes.

Fresh, not Django's session: that session sees only the session-level `SET`, never the
database-level default (applied after it opened), and a savepoint rollback reverts the
`SET` — it could report real time while the worker's next session sees frozen time. Not
a connection held by the fixture either: one opened before the first `freeze_at` never
sees the later database-level default (proven — a held connection read real time while a
fresh one read the frozen instant), and compensating with a session `SET` would turn
`now` into Python-side bookkeeping. Which is the third rejected option: reporting what
we intended to apply could never reveal a Python/DB desync, the failure this design most
needs visible.

Cost ~7 ms per read. Irrelevant in test code.

#### The transaction guard

- Requires `django_db(transaction=True)`. Under a plain `db` test the enqueued row is
  invisible to the worker's own connection, so a drain no-ops and `get_result` returns
  `None` — proven, and silently. `drain`/`get_result` therefore detect it and raise a
  curated error instead of letting a confusing `None` land far from its cause.
  Mechanism:
  - `connections[<alias>].in_atomic_block` — the behavioral truth. Proven to separate
    every case: plain `db` → `True`; `django_db(transaction=True)` marker → `False`;
    `transactional_db` fixture → `False`; `TestCase` → `True`; `TransactionTestCase` →
    `False`. Marker/fixture-name introspection is strictly worse — it misses the
    `transactional_db`-only and class-based paths.
  - Checked at **call time**, not fixture setup. At setup the check only happens to work
    here because our autouse `_enable_db` has already opened the atomic; a user project
    with no autouse db fixture has no such ordering guarantee, and a setup-time check
    would false-negative. A method body always runs after every db fixture.
  - Error wording must cover the legitimate-marker-but-inside-`atomic()` case (rows are
    equally invisible there): say "ran inside an open transaction — use
    `django_db(transaction=True)` and call outside `atomic()`", never "the marker is
    missing".
- The guard covers TWO blind surfaces, not one: reads happen on fresh connections too,
  so an uncommitted enqueue is invisible to `get_result` for the same reason it is
  invisible to the worker.

#### Drain semantics

- `get_result` fills a real gap — today our own tests reach raw psycopg
  (`tests/utils.py`); users writing durable tests have nothing to assert on.
- `drain` returns one `RunSnapshot` per run executed, in claim order. A run that
  SUSPENDED was still executed, so it appears with `state="sleeping"` — the honest
  reading, and what makes a chain assertable. The primary reason to return anything: a
  workflow that spawns children gives the test no ids, and queue introspection is cut,
  so `drain` is the only place those surface.
- **The same task can appear several times in one drain**, and that is the default, not
  an edge case: with no retry strategy the backoff is 0s and `AbsurdBackend`'s default
  max attempts is 5, so one drain burns all five attempts under a frozen clock —
  measured `[('boom', 1), … ('boom', 5)]`. Per-run records make that legible (each
  carries its own `attempt` and outcome) instead of five entries repeating one final
  verdict. With a real backoff the next attempt is scheduled at
  `current_time() + interval`, so it is NOT claimable and the drain ends after one run —
  advance past the backoff, then drain again.
- **The same RUN can appear twice too.** An `await_event` waiter re-arms its own run, so
  a same-drain emit makes the identical `run_id` claimable again — measured
  `[('sawait_event_once', 'sleeping'), ('semit_event_once', 'completed'), ('sawait_event_once', 'completed')]`.
  This is why each entry is read right after its own execution and never in one batch at
  drain end: a final-state batch read would rewrite history for the earlier appearance.
- **`drain() == []` does not mean "nothing happened".** Cancellation rules and
  `$ClaimTimeout` sweeps run INSIDE `claim_task` before anything is claimed, so a drain
  that just cancelled a task produces no claim rows and returns `[]`. Assert task state
  via `get_result` for those.
- Spawned children run in the SAME drain, not the next one. The burst loop claims until
  nothing is claimable, so a child enqueued from a running task body is picked up
  immediately — measured `DRAIN-1: ['spawn_child_then_return', 'run_child']`,
  `DRAIN-2: []`.
- No `concurrency` on `drain`: `drain_queue` runs lockstep batches, not a rolling
  window; `batch_size or concurrency` meant one argument set two things; nothing in the
  repo passed it; worker concurrency stays covered via the CLI path. Add an
  honestly-named `batch_size` later if a test needs multi-claim.
- Plumbing this needs: `drain_queue` counts runs and returns an `int`, which
  `run_burst_worker` discards (it returns the provisioning `SyncResult`). Widen that
  path to hand back the claim rows. Identity and `attempt` cost no extra query — the
  SDK's `ClaimedTask` TypedDict already carries
  `run_id`/`task_id`/`task_name`/`attempt`/`params`. Each run's
  `state`/`result`/`failure` is NOT in the claim row and costs one post-execution read
  per run. Additive change to `worker.py` — production code, not test-only.
- `absurd_drain_queue` stays, reimplemented as a delegate to `absurd.drain` — also
  without `concurrency`, so both spellings agree. Docs show the facade; alias gets one
  legacy line.
- Sync only. Facade is unusable from an async test (`drain` calls `asyncio.run`
  internally). Async twins are a follow-up, not this spec.

#### Naming and errors

- Fixture `absurd`, shipped from `django_absurd/pytest_plugin.py` alongside the existing
  `absurd_drain_queue`.
- `RunSnapshot` and `TaskSnapshot` live in `django_absurd/test.py` (which already hosts
  the public `install_absurd_cleanup`) and are importable — users need them to annotate
  helpers.
- Exception types: naive `freeze_at` → `TypeError`; the transaction guard →
  `RuntimeError`; missing time-machine → `ImproperlyConfigured`. All three carry the
  full rule + fix in the message.
- `get_result` accepts whatever `enqueue()` handed back — a Django `TaskResult.id` in
  `queue:uuid` form or a bare uuid — matching the existing `tests/utils.py` precedent.
- `run_id`/`task_id` are `uuid.UUID` at runtime even though the SDK's stubs type them
  `str` (psycopg deserializes the column); the dataclass fields must say `uuid.UUID`.

### Post-review redesign (human revdiff, 2026-07-29)

The branch shipped, then a human review reshaped three things. Recorded here because the
reasoning is not recoverable from the diff.

**The clock is a context manager, not two imperative calls.** `freeze_at`/`advance` are
replaced by `absurd.freeze_time(instant=None)` yielding a handle whose `move_to(dest)`
and `shift(delta)` are the only movers — time-machine's `Coordinates` vocabulary,
deliberately NOT called `travel` because travelling implies ticking and we freeze.
`None` means real now. Nesting raises rather than stacking: two frozen instants cannot
both be "now". Benefits beyond taste: the release point becomes lexical instead of
depending on fixture teardown (which stays as the crash net), and the "advance before
any freeze" special case stops existing, along with its rule and its test. `shift` also
names the semantics better than `advance` did — it is absolute elapsed time, the
distinction that made the first DST assertion wrong.

**`run_burst_worker` is gone; `worker.py` stops speaking management-command.** It had
been doing three jobs and returning `tuple[SyncResult, list[DrainedRun]]` purely because
the command wanted the first half and the fixture the second. The command now inlines
validate → provision → report → `run_worker(burst=True)`, which is what its blocking
path already open-coded, so the change deleted duplication rather than moving it. The
report and the "Started worker" line now print BEFORE the burst drain, matching the
blocking path. Test-facing entry point is `drain_queue(queue) -> list[DrainedRun]` with
no `WorkerOptions` (the fixture exposes no knobs), and the async internal became
`adrain_queue` to match the module's existing `arun_worker`/`aworker_client` convention.

**Error taxonomy on the drain path**, chosen so each failure names the right door:

| Condition                            | Raises                                                                                     |
| ------------------------------------ | ------------------------------------------------------------------------------------------ |
| Queue not declared in `TASKS QUEUES` | `ValueError` — a bad argument value, naming the valid queues                               |
| Declared but table missing           | `ImproperlyConfigured`, reusing `events.emit_event`'s exact wording for the same condition |
| Called inside an open transaction    | `RuntimeError` from the guard                                                              |

**`drain` provisions nothing.** `apps.provision_queues_after_migrate` already provisions
declared queues during `migrate`, so a test database arrives ready; the real gap is a
queue declared mid-test through changed settings, and that must surface as the curated
error above rather than as schema mutation from a call tests read results through.

**`absurd_drain_queue` is deleted outright**, not kept as a delegate — alpha owes
nothing, and pytest prints the available fixtures (including `absurd`) when a fixture is
missing. Its call sites, including four `examples/` suites, moved to `absurd.drain()`.

### Our own exception types (human revdiff, 2026-07-29)

The error taxonomy landed as two conditions mapped onto stdlib/Django types, then a
second pass made them ours. `django_absurd/exceptions.py` gains `DjangoAbsurdError` as
the base and `QueueNotDeclaredError` / `QueueNotProvisionedError` under it, with
`QueueReadOnlyError` and `ViewNotProvisionedError` rebased onto the same root;
`resolve_backend` raises a typed `BackendNotConfiguredError` and the three commands
catch that, still translating to `CommandError` so CLI text is unchanged.

Three reasons, in order of how much they mattered:

1. **The exception owns its message.** The first shape had
   `format_undeclared_queue_message` imported wherever a raise site needed text —
   including a lazy import in `backends.py` purely to reach it, because the builder sat
   in `queues.py`, which chains into `models.py`. Encapsulating the message in the class
   removed the helper AND the lazy import: the raise site already holds the data, so
   there is nothing to fetch.
2. **A specific type for a specific edge.** `QueueNotDeclaredError` says what went wrong
   where `ValueError` only said someone passed something bad. `except DjangoAbsurdError`
   then means "this package rejected something".
3. **Named for the distributing package.** `DjangoAbsurdError`, not `AbsurdError`:
   modules import from both `absurd_sdk` and `django_absurd`, and the SDK's own
   exceptions (`SuspendTask`, `CancelledTask`, `FailedTask`, a `TimeoutError` shadowing
   the builtin) share no base, so nothing anchors the short name to the SDK.

Accepted costs, recorded so they are not rediscovered as bugs:

- The Django/stdlib bases are GONE, so an external `except ValueError` around `enqueue`
  or `except ImproperlyConfigured` around `emit_event` stops catching. Alpha, no
  CHANGELOG — belongs in the next release notes.
- `except DjangoAbsurdError` is NOT universal: `checks.py`, `connection.py`,
  `queues.py`, and `test.py`'s transaction guard and clock validation still raise plain
  `ImproperlyConfigured`/`RuntimeError`/`TypeError`. Sweeping those is a follow-up; the
  docs must not overpromise in the meantime. `events.py` IS fully typed — a follow-up
  pass moved its no-backend case to `BackendNotConfiguredError`, since the same
  condition reached through `drain_queue` already raised that and one condition gets one
  type.
- Nothing in-package caught the two queue errors, so retyping them broke no handler —
  verified before the change, not after.

### Ordering rule (documented)

Freeze BEFORE enqueueing. Freezing to a past instant after rows exist leaves their
`available_at` in the DB's future — nothing claimable until an advance passes it.

## Isolation

Real time is restored after EVERY test, not at session end:

- The fixture is function-scoped, so teardown runs per test. Teardown stops travel and
  issues `ALTER DATABASE <test_db> RESET absurd.fake_now`.
- Unconditional: teardown runs whether the test passed, failed, or raised mid-`advance`.
  Both halves are released even if the first raises — a stopped time-machine with a live
  DB GUC is the livelock direction (DB ahead of Python), so releasing the DB GUC must
  not depend on the Python half succeeding.
- A test that never advanced resets nothing — the fixture only touches the clock once
  `freeze_at`/`advance` is called, so a plain `drain`-only test pays nothing and cannot
  leak.
- Targeted `RESET absurd.fake_now`, never `RESET ALL` — the latter would clobber
  unrelated database-level settings on the test DB.
- Also `RESET absurd.fake_now` at session level on Django's connection. Proven not
  strictly required — a session `SET` is reverted by the test's teardown rollback, and
  Django closes the connection between `transaction=True` tests — but it's one
  statement, and relying on connection churn for correctness is not worth the coupling.
- Same reset added defensively to the existing post-test flush (`flush_absurd_state`
  path).
- **Session-START sweep, required.** Per-test and post-flush resets are both scheduled
  AFTER a test body, so neither protects the first casualty of a poisoned reused DB: a
  SIGKILLed run strands the GUC, and with pytest-randomly the next run's first draining
  test can hang (no output, unkillable by pytest-timeout — see
  [Two clocks](#two-clocks-not-one)) before any resetting code runs. Mirror
  `_sweep_orphaned_pg_cron_jobs` in `django_absurd/pytest_plugin.py`: once per xdist
  worker, before any test, `ALTER DATABASE <test_db> RESET absurd.fake_now`. Same
  import-safety constraints as that fixture (takes only `request`, pulls DB fixtures via
  `getfixturevalue`).
- xdist-safe: per-worker test DBs (`absurd_test_core_gw0/1`), verified green under `-n2`
  with an empty `pg_db_role_setting` afterwards. The DB name must be read at runtime
  from `connections[<alias>].settings_dict["NAME"]`, never hardcoded, or the sweep and
  reset hit the wrong database under xdist.

## Testing plan (behavioral, through the fixture)

RED first, each test driving the real fixture + real worker; no monkeypatching, no
helper-unit tests.

- 7-day `sleep_for`: drain → `sleeping`; `advance(timedelta(days=7))`; drain →
  `completed`, step body ran once across replay.
- Not-yet-due: `advance` short of the wake → still `sleeping`. Guards against
  make-everything-due semantics.
- `sleep_until` with an absolute wake, driven by `freeze_at` + `advance`.
- Chain: task sleeping twice → advance, drain (re-arms second sleep, still `sleeping`),
  advance, drain → `completed`.
- `await_event` timeout: enqueue with week-long timeout → `sleeping`; advance past it;
  drain → task's timeout branch ran.
- `emit` resolves a waiter without any advance (already-covered path, now via facade).
- Retry backoff: failing task with exponential strategy; advance past the backoff; drain
  → attempt 2 runs.
- `freeze_at` before enqueue: `get_result(...).enqueued_at` equals the frozen instant.
- Missing time-machine: assert the complete `ImproperlyConfigured` message naming the
  install command. The import must genuinely fail, and time-machine IS installed in dev.
  Drive it with a real `sys.meta_path` finder that raises `ImportError` for
  `time_machine` — a real import condition, not a patched attribute — installed and
  removed by the test. Alternative if that proves brittle: a fixture-less subprocess
  pytest run in an env without the package (rejected by default: slow, and a skipped/
  env-gated test breaks patch coverage).
- Teardown resets: a test that advanced, then a following test asserting
  `pg_db_role_setting` holds no `absurd.fake_now` for the test DB, and that the
  following test's own `absurd.now` is real time. Cover the failure path too — a test
  that advances and then raises must still leave the GUC reset (drive via an inner
  failing test, e.g. `pytester`, rather than a passing test that pretends).
- `absurd_drain_queue` alias still drains.
- `drain` return value: a drain that runs two tasks returns both, in claim order, with
  `task_name`/`args`/`kwargs`/`attempt`/`state` populated; a drain with nothing
  claimable returns `[]`; a run that suspended appears with `state="sleeping"`.
- Spawned children surface in the SAME drain that ran the parent — assert both, in
  order, from one `drain()`. This is the case that has no ids for the test to hold.
- Retry sequence, per-run: a task failing twice then succeeding returns three
  `RunSnapshot`s with `attempt` 1/2/3 and states `failed`/`failed`/`completed`, the
  first two carrying their own `failure` and the third its `result`. This is what
  per-run records buy over a task-level view.
- Same RUN twice in one drain: an `await_event` waiter plus a same-drain emit returns
  the identical `run_id` twice, `sleeping` then `completed` — the reason entries are
  read per execution rather than batched at drain end.
- `get_result` echoes identity: `task_name`/`args`/`kwargs` equal what was enqueued.
- Default-retry burn: a task with no retry strategy returns five runs from ONE drain
  (`attempt` 1-5), and `get_result` then reads `state="failed"`, `attempts=5`.
- Long-backoff retry: one run returned, `get_result` reads `state="sleeping"`
  mid-backoff with `failure=None` (the documented caveat), then `advance` past the
  backoff and the next drain returns attempt 2.
- `drain() == []` after a cancellation: the sweep produces no claim rows, yet
  `get_result` reads `cancelled` — proving the empty list isn't "nothing happened".
- Foreign `params` shape (a raw-SDK row on a shared queue) does not blow up `drain`.
- `TaskSnapshot.failure` is populated for a terminally failed task (from the last
  attempt's `failure_reason`), and `result` for a completed one.
- jsonb decoding: `result`/`failure`/`args`/`kwargs` come back as Python objects, not
  strings — the loader is registered.
- Naive datetime to `freeze_at`: assert the complete curated error text.
- `freeze_at` with a non-UTC aware zone (e.g. `America/Chicago`): `absurd.now` and
  `enqueued_at` both equal the frozen instant, UTC-aware. Include a durable sleep
  advanced ACROSS a US spring-forward gap.
- Non-UTC SERVER: with `ALTER DATABASE <test_db> SET timezone` to a non-UTC zone, `now`
  and `enqueued_at` still read the correct instant and still report
  `utcoffset() == timedelta(0)` — the regression test for the mislabeling trap described
  in [Reads](#reads).
- `absurd.now` before any freeze is real time; after `freeze_at` it is exactly the
  frozen instant.
- Plain `db` test (no `transaction=True`): assert the complete curated error text, since
  the silent-`None` alternative is exactly what the guard exists to prevent.
- Claim-timeout expiry, cancellation `max_delay`, and `max_duration`: advance past the
  lease and the `$ClaimTimeout` sweep fires; a `max_delay` task lands `cancelled` with
  its body never run. All three were untestable before.
- Rewrite the four `time.sleep(2)` recipes in `tests/core/test_durable.py` to advances;
  delete the host-clock-vs-DB-clock skew comment. Measured: `4 passed in 9.53s` →
  `4 passed in 1.30s`.

## Adopt in the existing suite (phase 2, after the fixture ships)

**Order is deliberate: build and test the fixture first, migrate the suite second.** The
sweep below is recorded so nothing is lost between phases.

Measured with `-n4`, `--no-cov`: `tests/core` 374 passed in 32.30s, `tests/pg_cron` 257
passed in 21.84s.

### Convert

| Site                                                                       | Now                                                                                         | After                                                                     |
| -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| `tests/core/test_durable.py` × 4 (`sleep_for`/`sleep_until`, sync + async) | **8.59s combined** — the four slowest tests in `tests/core`, 27% of its aggregate test time | ~1.3s; the `time.sleep(2)` and its host-clock-vs-DB-clock comment both go |

### Make the fixture tasks honest

`tests/tasks.py` `ssleep_for_once` and `tests/atasks.py` `asleep_for_once` sleep
**1.5s**; `ssleep_until_once`/`asleep_until_once` use `time.time() + 1.5`. Those
durations exist only so a test could sit and wait for them. Raise them to something like
7 days: as written, these tests would pass against an implementation that only handles
sub-second sleeps, so the change buys correctness, not just speed.

### Coverage the fixture unlocks (absent today, not merely slow)

- `tests/core/test_events.py` has exactly one `await_event` timeout test and it passes
  `timeout=0` — degenerate, never exercises a deadline actually expiring.
- Nothing covers retry backoff with a real interval, claim-timeout expiry, or
  cancellation `max_delay`/`max_duration`. All four are reachable once time can be
  advanced.

### Keep wall-clock — these are timing and race tests, not slow tests

- `test_async_worker.py::test_async_concurrency_is_not_serial` (0.58s): asserts
  `elapsed < 1.5s` across 4 × `asleeper(0.5)`. The wall clock IS the assertion.
  Unaffected by a freeze anyway — `asyncio.sleep` plus an unpatched `monotonic`.
- `test_scheduler.py` live worker/beat tests
  (`test_worker_with_beat_runs_scheduled_task` 1.23s, the poll-until-row loops with
  SIGTERM killer threads, and the `time.sleep(0.05)` that lets beat enter `stop.wait`).
  Real command wiring and thread races.
- `tests/pg_cron/test_isolation_regression.py` — **14.25s, 65% of that suite**, and out
  of reach by construction (the launcher runs in the central database on its own clock).
  Its producer already uses `"1 seconds"`, so the time is scratch-DB creation plus
  schema install, not waiting. Leave it alone.

### Adjacent cleanup

24 call sites use `tests/utils.py:get_task_result`, which opens a raw psycopg connection
per call — they become `absurd.get_result`. (58 `run_absurd_worker` sites could become
`absurd.drain()`, but most have no reason to change.)

## Migrate our own suite off freezegun

One clock library in the repo, not two. In scope here:

- Drop `freezegun==1.5.5` from dev deps (`pyproject.toml:12`), add time-machine, relock.
- **Every destination becomes an explicit `dt.datetime(..., tzinfo=dt.UTC)`. No
  strings.** The two libraries disagree on bare strings: proven on a UTC-5 host,
  freezegun reads `'2026-01-01 12:00:00'` as `12:00 UTC`, time-machine as `17:00 UTC`
  (process-local). Our suite masks it because Django sets `os.environ["TZ"]` from
  `TIME_ZONE`, but `override_settings(TIME_ZONE=...)` calls `tzset` live — so a string's
  meaning would depend on whether the freeze started before or after the override.
  Host-invisible and fragile; convert, don't "verify".
- 6 decorator sites in `tests/core/test_scheduler.py:96-141` (cron-math):
  `@freeze_time("…")` →
  `@time_machine.travel(dt.datetime(…, tzinfo=dt.UTC), tick=False)`.
- `tests/core/test_scheduler.py:610` — the 7th site, `freeze_time(…, tick=True)` around
  a live worker with `beat=True` near a `*/1` boundary. Keeps `tick=True`: that test
  WANTS real time to reach the next slot. The `tick=False` rule is about the `absurd`
  fixture keeping two clocks in lockstep; it does not apply to a test that touches no
  `absurd.fake_now`.
- `run_beat_until` in `tests/core/test_scheduler.py:147` and
  `tests/core/test_cleanup.py:261`: `freeze_time(…) as frozen` + `frozen.tick(Δ)` →
  `travel(…, tick=False) as traveller` + `traveller.shift(Δ)`. Both already inject
  `run_beat`'s `wait` seam, so every advance is explicit and `tick=False` is correct —
  no real `threading.Event.wait` is involved.
- Update the stale comment at `tests/core/test_scheduler.py:144-146` that names
  freezegun.
- **Rehearsed end-to-end during review and reverted**: all 9 conversions across the two
  files, then `tests/core` 374 passed (-n4), `tests/pg_cron` 257 passed, `tests/multidb`
  6 passed. `traveller.shift(Δ)` is a faithful `frozen.tick(Δ)` substitute and the
  `tick=True` live-beat test survives. An `ag --hidden` sweep confirms no 8th call site
  — freezegun appears only in `pyproject.toml`/`uv.lock` and that stale comment.

## Docs

- New `docs/web/testing.md` section: the fixture, the freeze-then-enqueue rule, the
  `transaction=True` requirement, the explicit "install time-machine yourself" line, and
  why durable time only moves on `advance`. Plus the `TaskSnapshot` caveats and two
  hazards: a subprocess worker is only half-frozen, and a savepoint rollback issued
  mid-test after an `advance` reverts only Django's session GUC (the DB-level default
  and time-machine survive). Measured consequence, to state plainly: a later `enqueue()`
  stamps the STALE instant while `absurd.now` still reports the advanced one — `now`
  reports what the worker will act on, so it cannot flag a stale enqueue stamp.
- `django_absurd/AGENTS.md` integration note.
- CLAUDE.md testing conventions: durable tests use the fixture, never `time.sleep`.

## Out of scope / follow-ups

- Async facade twins (`adrain`/`aadvance`/…) for pytest-asyncio users.
- `settle(result)` auto-advance loop — deliberately not shipped; explicit steps first.
- Driving the blocking (non-burst) worker, or a `manage.py absurd_worker` subprocess,
  under a frozen clock — the subprocess case is DB-frozen only, i.e. the deadlock
  direction.
- `get_next_wake` / `get_run_counts` introspection. Cut: no public SDK API exposes
  either, so both would read `r_<q>`/`w_<q>` for assertions no test in the plan needs —
  every state check goes through `get_result`.
- Partitioned queues under a freeze. PG18's `uuidv7()` ignores `absurd.fake_now`
  (proven: `absurd.current_time()` = 2036 while `uuidv7_timestamp(portable_uuidv7())` =
  real 2026-07-28), so partition routing keys on real time while rows carry fake
  `available_at`. Harmless for `order by (available_at, run_id)` — ties break by real
  enqueue order — but unproven for partitioned storage, which no suite currently uses.
- Non-pytest `manage.py test` parity (existing issue #96 shape).

## Appendix: rejected alternatives

### Rewriting deadline rows instead of the clock

Alternative was SQL UPDATEs pulling `available_at` / `w_<q>.timeout_at` / the paired
sleep checkpoint in `c_<q>` back by Δ. Works, needs no dependency, reaches a subprocess
worker. Rejected: couples to Absurd's per-queue table names, needs run↔checkpoint
pairing heuristics, and gives less for free. One frozen clock also covers retry backoff,
`claim_expires_at`, cancellation `max_delay`/`max_duration` with zero extra code.

### freezegun as the Python clock

freezegun patches `time.monotonic`; asyncio's loop clock IS `time.monotonic`. Frozen
freezegun deadlocks the drain — verified, 40s timeout stuck in `KqueueSelector.select`.
freezegun only works with `tick=True`, which violates the consistency rule above. Out.
time-machine leaves `monotonic`/`perf_counter` alone.

### Django's `TaskResult`, the SDK snapshot, and the admin pseudo-models

- **Django's `TaskResult`**: the backend's status mapping folds `sleeping → RUNNING` and
  `cancelled → FAILED`, so through Django's vocabulary you cannot tell "asleep for 7
  days" from "executing", nor "cancelled by `max_delay`" from "failed" — the exact
  distinctions this plan asserts. It stays reachable the normal way (`enqueue()` returns
  one); the facade exists to expose what that type cannot.
- **The SDK's `TaskResultSnapshot`**: `state`/`result`/`failure` only, no identity, and
  it would make `absurd_sdk` part of our test-API surface.
- **The admin pseudo-models**: `build_admin_model` needs `rebuild_views()` to have run,
  so it breaks in any test that drops the schema (our own `_isolate_queues` does).
  `build_queue_table_model` avoids the view, but both are built dynamically and type as
  `Any` — nothing for mypy/pyright users — and both are the stopgap a standing refactor
  is meant to replace.
