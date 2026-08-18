# Explicit provisioning — design

Follows [#195](https://github.com/lincolnloop/django-absurd/issues/195).

## Problem

Five code paths create queue topology today:

| path                                        | creates                             | runs in           |
| ------------------------------------------- | ----------------------------------- | ----------------- |
| `post_migrate` → `provision_backend`        | declared queues + all 5 admin views | `migrate`         |
| `absurd_sync_queues` → `provision_backend`  | declared queues + all 5 admin views | operator command  |
| `AbsurdTestRuntime.sync_queues` (`test.py`) | declared queues + all 5 admin views | a test, on demand |
| `absurd_worker` start → `provision_backend` | declared queues + all 5 admin views | worker boot       |
| `enqueue` (`backends.py:181-207`)           | one declared queue, retries spawn   | **a web request** |

Last two are self-heal, from [#13](https://github.com/lincolnloop/django-absurd/pull/13)
(2026-06-24). That design locked "both seams auto-create, always on, no opt-out" on a
stated constraint:

> `absurd.create_queue` already idempotent … Calling on existing queue = no-op.
> Concurrent creators race harmlessly.

Second sentence false. `ensure_queue_tables` is `CREATE TABLE IF NOT EXISTS`, which
Postgres documents as not race-free — concurrent FIRST creation collides on a catalog
unique index. #195 proved it in the view half: 74 failures of 100 concurrent calls at 4
processes, 39 of 80 at 2. Enqueue half is the same race, still unguarded (`create_queue`
at `backends.py:195` sits outside `lock_provisioning`): N web containers enqueueing to a
declared-but-unprovisioned queue collide, and the `IntegrityError` surfaces out of a
request.

Three further facts, all verified:

- **Upstream does not self-heal.** `spawn_task` and `claim_task` build dynamic SQL
  against `'t_' || p_queue_name` and raise
  `UndefinedTable: relation "absurd.t_<queue>" does not exist` on a missing queue
  (probed live; pinned SQL corroborates). `create_queue` is a separate explicit call the
  SDK never invokes implicitly. Auto-create is entirely a django-absurd layer.
- **Self-heal wants DDL rights in a web request.** Enqueue's `create_queue` needs
  `CREATE` on the `absurd` schema at request time. Prod web roles are commonly DML-only
  — the same anti-correlation that got admin read-path self-heal dropped. Here it is the
  write path.
- **`post_migrate` already delivers what #13 wanted.** #13 existed to remove a mandatory
  command; `post_migrate` provisioning landed two days later in
  [#17](https://github.com/lincolnloop/django-absurd/pull/17) and provisions the full
  declared set on every `migrate`, applied or not. The two seams are redundant with it,
  not complementary. The project's founding position
  (`docs/superpowers/specs/2026-06-18-queue-models-design.md`, initial commit) was
  explicit-only: "All mutation happens through an explicit management command — NO
  `migrate`/`post_migrate`/`ready()` magic. `migrate` only migrates. A system check
  tells you when to run the command." #13 overrode it and removed that check — W002
  narrowed to storage_mode drift because auto-create healed the missing-queue case, and
  W001 dropped on the separate ground that schema-absence is a runtime error rather than
  a deploy warning.

## Ship

Provisioning is a deploy step. Runtime paths classify and refuse.

### Enqueue stops creating

`AbsurdBackend.enqueue`'s
`except (UndefinedTable, UndefinedFunction, InvalidSchemaName)` branch keeps
classifying, drops the create-and-retry:

- undeclared queue → `QueueNotDeclaredError` (unchanged)
- `InvalidSchemaName`/`UndefinedFunction` → `SchemaNotInstalledError`
- `UndefinedTable` naming one of this queue's own tables → `QueueNotProvisionedError`
- anything else → re-raise untouched

Schema-absence classification MOVES. Today it is detected by the create-and-retry
failing (`backends.py:194-200`); with the create gone it must read off the ORIGINAL
spawn exception. Same outcomes, new mechanism, so its tests change too.

`UndefinedTable` case reuses `names_a_queue_table`, the discipline `drain_queue` and
`events.emit_event` already follow. `QueueNotProvisionedError`'s message already names
the fix (`Run: manage.py absurd_sync_queues`).

Savepoint stays: `spawn` still runs inside `transaction.atomic(savepoint=True)` so a
failed enqueue leaves an enclosing atomic usable. The retry spawn goes with the create.

### Worker start stops provisioning, and regains a startup guard

`absurd_worker.handle` drops `provision_backend` + `report_sync_result`.

That alone is NOT enough, and the naive version regresses the operator experience. The
live worker path (`run_worker` → `arun_worker` → `run_blocking_worker`) carries no
`UndefinedTable` handling: the `QueueNotProvisionedError` translation at `worker.py:168`
belongs to `drain_queue`, the in-process test-tooling entry point, not to the CLI. The
only guard on the live path is `aworker_client`'s schema probe (`worker.py:289-296`),
and its `list_queues()` succeeds whenever the schema exists — including when the served
queue's tables do not. A raw `psycopg.errors.UndefinedTable` is not in
`CONFIGURATION_ERRORS`, so without new work the operator gets an untranslated traceback
out of the claim loop.

So restore the guard #13 deleted, retyped. #13's spec says outright: "`aworker_client`:
DELETE the `if queue not in provisioned: raise ImproperlyConfigured` block. Queue
guaranteed to exist by the time the async client runs (command reconciled first)." That
premise dies with worker provisioning, so the block comes back beside the existing
schema probe, raising `QueueNotProvisionedError` instead of `ImproperlyConfigured`. It
costs no extra query — `list_queues()` is already awaited there — and it fires before
the worker announces itself, so the command base turns it into one clean `CommandError`.

Residual, accepted: `list_queues` reads catalog rows, so a row-present/tables-absent
queue still reaches the claim loop and raises raw `UndefinedTable`. See the reconcile
fix below, which is what makes that state recoverable at all.

Losing the view rebuild on worker boot is fine: views are admin-only (`get_result` reads
the raw per-queue tables, not the union views), and both command-side provisioners
rebuild the full catalog.

Note the worker seam had already drifted past what #13 authorized — spec said reconcile
the SERVED queue only; #17 widened it to the whole catalog plus views.

### Sync heals what the error promises it heals

`reconcile_queue` calls `create_queue` only when the `Queue` catalog row is absent
(`queues.py:129-157`); with the row present it reconciles policy and nothing else. So a
row-present/tables-absent queue — manual drop, partial restore — is healed today ONLY by
enqueue's unconditional `create_queue`, and after this change by nothing:
`QueueNotProvisionedError` would send an operator to a command that reports "no
changes".

Call `create_queue` unconditionally instead. It is idempotent by construction
(`INSERT … ON CONFLICT DO NOTHING` then `perform ensure_queue_tables`), so it recreates
missing tables for an existing row and no-ops otherwise. `created` accounting still keys
off the pre-existing row check, so command output is unchanged. Cost is a handful of
`IF NOT EXISTS` statements per declared queue, at deploy time only.

### No queue-state check

The founding spec paired explicit-only provisioning with "a system check tells you when
to run the command", and #13 removed it. Not restoring it, deliberately:

- DB-dependent checks run BEFORE the command that would fix the condition. Verified:
  `migrate` injects `databases=[options["database"]]` into its check kwargs
  (`migrate.py:95`) and `BaseCommand.execute` runs `self.check()` ahead of `handle()`.
  So a "declared queue not provisioned" check fires on every `migrate` that follows
  declaring a queue — moments before `post_migrate` provisions it. An Error would block
  that `migrate`; a Warning cries wolf. This is half of why W001 was retired as noisy.
- A warning that routinely fires when nothing is wrong gets added to
  `SILENCED_SYSTEM_CHECKS`, and is then dead in the case it exists for.
- The condition is already reported at runtime with a message naming the command.
- The founding spec wrote that requirement when NOTHING provisioned automatically —
  `post_migrate` did not exist. It carries the ergonomic load now, so the gap the check
  covered is far narrower than it was.

`query_queue_state` therefore keeps reporting W002 storage-mode drift only, unchanged.

### Test tooling needs no fixture change

`post_migrate` provisions the test database at creation (`tests/settings.py:61-65`) and
per-test `flush_absurd_state` only TRUNCATEs, so the declared catalog is standing for
the whole session. The suites do not depend on self-heal to reach a provisioned queue;
they reach one because they ran `migrate`.

The exception is `_isolate_queues` (`tests/conftest.py`), which hard-drops all topology
before AND after its test. Three tests inside those files then enqueue or start a worker
with nothing of their own, and today the hole is repaired by accident. Those three take
an explicit `dj_absurd.sync_queues()` at the call site — the util that already exists
for exactly this, already used by `test_task_outside_tasks_py_runs`:

- `tests/core/test_worker.py::test_queue_defaults_to_default`
- `tests/core/test_worker.py::test_worker_uses_single_backend_at_nondefault_alias`
- `tests/multidb/test_router.py::test_roundtrip_drains_on_the_non_default_alias`

`_isolate_queues` keeps its current contract (drop before and after). Re-provisioning on
teardown was tried and MEASURED to change nothing: with the three call-site fixes in
place the control run fails the same eight tests either way, so the fixture change was
dead weight and is not shipping.

Same exposure downstream, and it needs documenting rather than fixing: a project using
the pytest tooling that drops topology and relies on first-enqueue recreation must call
`dj_absurd.sync_queues()`; a `--reuse-db` run that declares a NEW queue gets no
`post_migrate` on later runs, so it needs `--create-db` or that same call.
`AbsurdTestRuntime.sync_queues`'s docstring ("rarely needed") stays true — it is still
rare, just no longer optional in a topology-dropping test.

### Unchanged

`post_migrate` and `absurd_sync_queues` keep `provision_backend`, and with it
`lock_provisioning`. Best-effort swallow in the `post_migrate` receiver stays as is.

## Consequences elsewhere, accepted

- **Beat.** `spawn_scheduled` (`scheduler.py:76`) enqueues, and `fire_schedule`
  (`:176-180`) catches every exception into `logger.exception`. A schedule pointed at an
  unprovisioned queue currently auto-creates; afterwards it logs the typed error every
  slot and never runs, while the worker stays up. Loud in the log, invisible in the
  queue — and the beat loop must keep going for the other schedules, so the swallow
  stays.
- **Deferred enqueue.** The `run_after` wrapper's inner enqueue happens inside a worker
  at due time. An unprovisioned target now burns the wrapper's `max_attempts` and lands
  FAILED instead of auto-creating. Visible in the admin, which is the right place.

## Out of scope

- Skipping the view rebuild when nothing changed (deploy-stall hedge). Measured on this
  branch with a temporary timing test: `rebuild_views` is 4.9ms at one queue and 24.2ms
  at 25 (min of 5 runs, local Postgres), so the only argument was lock exposure, and
  that is dominated by the admin ORDER BY work.
- `post_migrate`'s silent swallow — see the upgrade note, which it makes worse. Separate
  decision.
- Anything about admin views' shape or the pg_cron provisioning path (pg_cron is
  unaffected: its SQL wrapper spawns directly and never had auto-create).

## Testing

RED first, through real entrypoints.

- Enqueue to a declared, unprovisioned queue raises `QueueNotProvisionedError`; assert
  the complete message. Assert the queue is still absent afterwards — the point is that
  nothing was created.
- Enqueue to an undeclared queue still raises `QueueNotDeclaredError`; enqueue against a
  hidden schema still raises `SchemaNotInstalledError` (now classified off the spawn
  exception, so this test is exercising new code).
- An unrelated `UndefinedTable` raised from inside `spawn_task` propagates as itself,
  not relabelled.
- Enqueue inside an outer `transaction.atomic` leaves that block usable after the
  refusal (replaces the existing auto-create-under-atomic test).
- `absurd_worker` against an unprovisioned declared queue exits with `CommandError`
  carrying the typed message, before any "Started worker" output, and creates neither
  the queue nor the views.
- `absurd_sync_queues` recreates the tables of a queue whose catalog row survived but
  whose tables were dropped, and still reports it as no change.
- Existing `post_migrate` / `absurd_sync_queues` and system-check tests keep passing
  untouched — no check changes ship here.

Tests asserting the removed behaviour, to delete or rewrite. MEASURED, not predicted:
with the three call-site fixes above already applied, disabling both self-heal seams
fails exactly these eight in `tests/core` and nothing at all in `tests/multidb` or
`tests/pg_cron`.

- `tests/core/test_enqueue.py::test_enqueue_auto_creates_declared_queue_and_runs`,
  `::test_enqueue_auto_create_survives_outer_atomic` — delete; replaced by the refusal
  tests above.
- `tests/core/test_enqueue.py::test_enqueue_with_absent_schema_raises_clear_error` —
  keep the assertion, rewrite: schema-absence is classified off the spawn exception now.
- `tests/core/test_worker.py::test_worker_start_provisions_all_declared_queues` —
  delete; the worker no longer provisions.
- `tests/core/test_worker.py::test_worker_command_reconciles_changed_mutable_option`,
  `::test_worker_command_reconciles_changed_interval_option`,
  `::test_worker_command_warns_on_storage_mode_drift` — the reconcile-on-boot half goes;
  each already syncs by command first, so what survives is a startup-output assertion.
  Their reconcile coverage belongs to `absurd_sync_queues`' own tests.
- `tests/core/test_orm_views.py::test_worker_start_rebuilds_when_it_created_queue` —
  delete; boot no longer rebuilds views.

`::test_worker_command_no_reconcile_when_unchanged` passes unchanged (it asserts the
ABSENCE of provisioning output), as does
`test_orm_views.py::test_sync_command_rebuilds_views_with_new_queue` — though the latter
never calls the command its name claims and passes on in-file ordering, worth fixing
while in there.

## Docs

- `django_absurd/AGENTS.md:757-761` states the removed behaviour outright ("on worker
  start, by `absurd_sync_queues`, and on first enqueue") — rewrite, plus `:906-909` and
  `:917-918`. `README.md` already promises migrate-provisioning only.
- `docs/web/workers.md:16` — "On start it provisions every declared queue and rebuilds
  the admin views" is exactly what stops being true.
- `docs/web/configuration.md:120-122` — the `QueueNotProvisionedError` raiser list gains
  enqueue and the worker. `docs/web/testing.md:118` and its `--reuse-db` guidance per
  the test-tooling section above.
- `django_absurd/admin.py:146-149` is SOURCE, not docs: the changelist warning offers
  "(or start a worker on them)", which becomes false. Its message-asserting test changes
  with it.
- `django_absurd/queues.py:183-184`'s comment names worker start as a caller.
- `examples/`: worker services converge via `restart: on-failure` once the app service's
  `migrate` lands; confirm with a real `docker compose up` on all three rather than by
  reading. No example doc promises auto-create.
- `docs/WHY.md`: the durable record. Three decisions have passed through here
  (explicit-only → self-heal → post_migrate) and none was ever captured, which is why
  this had to be reconstructed from git.

## Upgrade note

Breaking; `feat!`. A deploy that runs `manage.py migrate` to completion is unaffected —
`post_migrate` provisions the declared set every time, applied migrations or not.

Three ways to be affected anyway, all worth naming in the release notes:

- Declaring a queue and deploying without `migrate`. Add `absurd_sync_queues` to the
  release step.
- Pipelines gated on `migrate --check`: it exits before `post_migrate` fires, and a
  queue-only change touches no migrations, so the provisioning step is skipped by a
  pipeline that believes it runs migrate.
- The `post_migrate` receiver swallows `ImproperlyConfigured`, `OperationalError`,
  `ProgrammingError` and `SchemaNotInstalledError` silently, so an unreachable database
  — or a migrate role without `CREATE` on the `absurd` schema — provisions nothing and
  reports nothing. Worker boot and first enqueue are today's fallbacks for that, and
  both are being removed.

## Shipped beyond this design

Adversarial review and live testing against a fresh project moved five things after the
sections above were written. Each supersedes what it names.

- **Missing-table probe follows storage mode.** `find_missing_queue_tables` probed the
  five tables every queue owns; the enqueue-time classifier matched six. A partitioned
  queue missing only `i_<queue>` therefore refused every keyed enqueue while sync
  reported nothing to repair and `--check` exited 0 — unrepairable by any shipped
  command, and fatal to a whole schedule, since `spawn_scheduled` always carries an
  idempotency key. The probe now takes the existing `storage_mode`. `aworker_client`
  asks the same question on its own async connection
  (`queues.afind_missing_queue_tables`) — a twin body, because a Django cursor inside
  the worker's loop raises `SynchronousOnlyOperation`.
- **Schema absence classified per operation, not per queue.** Classification lived in
  the per-queue loop, so a backend declaring no queues reached `rebuild_views` and
  raised a raw `ProgrammingError` — breaking the `migrate --fake` path the swallow
  exists for, and diverging from `--check`, which exited 0 in the same state.
  `require_installed_schema` asks up front, in both the write path and the dry run.
- **The swallow narrowed to genuine absence** — supersedes _Out of scope_'s
  "`post_migrate`'s silent swallow … Separate decision", and the third upgrade bullet
  below it. An absent schema and a dropped `absurd.queues` raise the identical
  `UndefinedTable`, and only the first is something `migrate` can fix; the second made
  `migrate` print `Not provisioned: … Run: manage.py migrate` and exit 0 forever, advice
  that loops because the migration is already applied. Only a live probe separates them,
  and it has to run before the failing statement, which has already aborted its
  transaction.
- **The worker refuses before it announces a start.** The catalog check passed a queue
  whose row outlived its tables, so `worker started:` and the `🐘 Started worker` banner
  both printed for a worker that died on its first claim — repeating under
  `restart: on-failure`, on the one line an operator would alert on. `drain_queue`
  logged it too. The boot guard in `aworker_client` covers both entry points;
  `run_worker`'s translation is left holding a queue dropped mid-flight.
- **No Absurd backend configured is an error for every `absurd_*` command**,
  `absurd_cleanup` and `absurd_flush` included — reverses the exit-code split left
  standing in `2026-08-15-command-error-translation-design.md`'s _Out of scope_. Exiting
  0 is defensible only where doing nothing is the intended outcome, and a missing
  backend is a misconfiguration: a nightly `absurd_cleanup` otherwise stops cleaning
  silently and reports success.

Two additions the _Docs_ section above does not list: `docs/web/deploying.md` (the
deploy script, the `--database` asymmetry, `--check`, and the `migrate --check` trap),
and examples gating `app` and `worker` on a one-shot `migrate` service via
`service_completed_successfully` — which replaces the `restart: on-failure` convergence
that section describes.
