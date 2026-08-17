# Explicit provisioning — design

Follows [#195](https://github.com/lincolnloop/django-absurd/issues/195).

## Problem

Four code paths create queue topology today:

| path                                        | creates                             | runs in           |
| ------------------------------------------- | ----------------------------------- | ----------------- |
| `post_migrate` → `provision_backend`        | declared queues + all 5 admin views | `migrate`         |
| `absurd_sync_queues` → `provision_backend`  | declared queues + all 5 admin views | operator command  |
| `absurd_worker` start → `provision_backend` | declared queues + all 5 admin views | worker boot       |
| `enqueue` (`backends.py:181-205`)           | one declared queue, retries spawn   | **a web request** |

Last two are self-heal, from [#13](https://github.com/lincolnloop/django-absurd/pull/13)
(2026-06-24). That design locked "both seams auto-create, always on, no opt-out" on a
stated constraint:

> `absurd.create_queue` already idempotent … Calling on existing queue = no-op.
> Concurrent creators race harmlessly.

Second sentence false. `ensure_queue_tables` is `CREATE TABLE IF NOT EXISTS`, which
Postgres documents as not race-free — concurrent FIRST creation collides on a catalog
unique index. #195 proved it in the view half (`pg_type_typname_nsp_index`, 9 of 12
concurrent calls). Enqueue half is the same race, still unguarded: N web containers
enqueueing to a declared-but-unprovisioned queue collide, and the `IntegrityError`
surfaces out of a request.

Three further facts, all verified:

- **Upstream does not self-heal.** Probed against a live Absurd schema: `spawn_task` and
  `claim_task` on a missing queue both raise
  `UndefinedTable: relation "absurd.t_<queue>" does not exist`. `create_queue` is a
  separate explicit call; the SDK worker never invokes it. Auto-create is entirely a
  django-absurd layer.
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
  `migrate`/`post_migrate`/`ready()` magic. A system check tells you when to run the
  command." #13 overrode it AND deleted that check as collateral (W001 dropped, W002
  narrowed to storage_mode drift), on the premise disproved above.

## Ship

Provisioning is a deploy step. Runtime paths classify and refuse.

### Enqueue stops creating

`AbsurdBackend.enqueue`'s
`except (UndefinedTable, UndefinedFunction, InvalidSchemaName)` branch keeps
classifying, drops the create-and-retry:

- undeclared queue → `QueueNotDeclaredError` (unchanged)
- schema absent → `SchemaNotInstalledError` (unchanged)
- declared, schema present, missing queue table → `QueueNotProvisionedError`

Third case reuses `names_a_queue_table` so an unrelated `UndefinedTable` from inside
`spawn_task` re-raises untouched, same discipline as `worker.drain_queue` and
`events.emit_event`. `QueueNotProvisionedError`'s message already names the fix
(`Run: manage.py absurd_sync_queues`).

Savepoint stays: `spawn` still runs inside `transaction.atomic(savepoint=True)` so a
failed enqueue leaves an enclosing atomic usable. The retry spawn goes with the create.

### Worker start stops provisioning

`absurd_worker.handle` drops `provision_backend` + `report_sync_result`. Claim path
already raises `QueueNotProvisionedError` (`worker.py:168`), and the command base
translates it to `CommandError` (it is in `CONFIGURATION_ERRORS`), so an operator gets
one clean line naming the command to run.

Loses the view rebuild on worker boot. Acceptable: views are admin-only, and both
remaining provisioners rebuild the full catalog.

Note the worker seam had already drifted past what #13 authorized — spec said reconcile
the SERVED queue only; #17 widened it to the whole catalog plus views.

### No queue-state check

The founding spec paired explicit-only provisioning with "a system check tells you when
to run the command", and #13 deleted that check. Not restoring it, deliberately:

- DB-dependent checks run BEFORE the command that would fix the condition. Verified:
  `migrate` injects `databases=[options["database"]]` into its check kwargs
  (`migrate.py:95`) and `BaseCommand.execute` runs `self.check()` ahead of `handle()`.
  So a "declared queue not provisioned" check fires on every `migrate` that follows
  declaring a queue — moments before `post_migrate` provisions it. An Error would block
  that `migrate`; a Warning cries wolf. This is the same complaint that retired W001.
- A warning that routinely fires when nothing is wrong gets added to
  `SILENCED_SYSTEM_CHECKS`, and is then dead in the case it exists for.
- The condition is already loud at runtime, from three entrypoints, with a message
  naming the command. A check only buys earliness, and only for projects that run
  `check --database` rather than the bare `check`.
- The founding spec wrote that requirement when NOTHING provisioned automatically —
  `post_migrate` did not exist. It carries the ergonomic load now, so the gap the check
  covered is far narrower than it was.

`query_queue_state` therefore keeps reporting W002 storage-mode drift only, unchanged.

### Unchanged

`post_migrate` and `absurd_sync_queues` keep `provision_backend`, and with it
`lock_provisioning`. Best-effort swallow in the `post_migrate` receiver stays as is.

## Out of scope

- Skipping the view rebuild when nothing changed (deploy-stall hedge). Measured: rebuild
  is 5-28ms for 1-25 queues, so the only argument was lock exposure, and that is
  dominated by the admin ORDER BY work.
- `post_migrate`'s silent swallow. Real (unreachable DB provisions nothing, says
  nothing) but a separate decision.
- Anything about admin views' shape or the pg_cron provisioning path.

## Testing

RED first, through real entrypoints.

- Enqueue to a declared, unprovisioned queue raises `QueueNotProvisionedError`; assert
  the complete message. Assert the queue is still absent afterwards — the point is that
  nothing was created.
- Enqueue to an undeclared queue still raises `QueueNotDeclaredError`; enqueue against a
  hidden schema still raises `SchemaNotInstalledError`.
- An unrelated `UndefinedTable` raised from inside `spawn_task` propagates as itself,
  not relabelled.
- Enqueue inside an outer `transaction.atomic` leaves that block usable after the
  refusal (replaces the existing auto-create-under-atomic test).
- `absurd_worker` against an unprovisioned declared queue exits with `CommandError`
  carrying the typed message, and creates neither the queue nor the views.
- Existing `post_migrate` / `absurd_sync_queues` and system-check tests must keep
  passing untouched — no check changes ship here.

Tests that assert the removed behaviour and must go or be rewritten:
`tests/core/test_enqueue.py::test_enqueue_auto_creates_declared_queue_and_runs`,
`::test_enqueue_auto_create_survives_outer_atomic`,
`tests/core/test_orm_views.py::test_worker_start_rebuilds_when_it_created_queue`, plus
the two `tests/core/test_worker.py` sites whose comments lean on enqueue auto-creating
(`:184`, `:524`).

## Docs

- `django_absurd/AGENTS.md:757-759` states the removed behaviour outright ("on worker
  start, by `absurd_sync_queues`, and on first enqueue") — rewrite. `README.md` and the
  rest of AGENTS.md: `migrate` provisions, `absurd_sync_queues` is the explicit path,
  run it in the release step if the deploy does not run `migrate`.
- `docs/web/`: same claim wherever it recurs (`configuration.md`, `admin.md`,
  `cleanup.md`, `testing.md` all mention `sync_queues`).
- `examples/`: worker services rely on the app service's `migrate`; confirm each compose
  still converges (worker restarts until provisioned) and fix the flow docs if they
  promise auto-create.
- `docs/WHY.md`: the durable record. Three decisions have passed through here
  (explicit-only → self-heal → post_migrate) and none of it was ever captured, which is
  why this had to be reconstructed from git.

## Upgrade note

Breaking; `feat!`. Deploys that run `migrate` are unaffected — `post_migrate` provisions
the declared set every time. Exposed case is declaring a queue and deploying without
`migrate`: add `absurd_sync_queues` to the release step.
