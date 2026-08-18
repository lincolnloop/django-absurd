# Explicit provisioning — plan

Design:
[`docs/specs/2026-08-17-explicit-provisioning-design.md`](../specs/2026-08-17-explicit-provisioning-design.md).

One PR. An earlier version of this plan split it in two on the premise that the suites
self-heal broadly and that ~95 tests would need repairing. Measured: they do not. The
suites reach a provisioned queue because they run `migrate`, not because anything heals
at runtime. Disabling both self-heal seams fails exactly eight tests in `tests/core` and
none in `tests/multidb` or `tests/pg_cron` — and every one of the eight asserts the
behaviour being deleted. There is no signal to untangle, so there is no split.

## Prerequisite, already done

Three tests inside `_isolate_queues` files enqueue or start a worker after the fixture
dropped the catalog, and are repaired today by accident. Each takes an explicit
`dj_absurd.sync_queues()` at its call site (`tests/core/test_worker.py`
`::test_queue_defaults_to_default` and
`::test_worker_uses_single_backend_at_nondefault_alias`,
`tests/multidb/test_router.py::test_roundtrip_drains_on_the_non_default_alias`). A no-op
against today's code; it just stops the three depending on a path that is going away.

No fixture change. Re-provisioning on `_isolate_queues` teardown was built and then
measured to change nothing — same eight failures with or without it.

## Sequence

RED first, per the design's Testing section.

1. Enqueue refuses. Tests: declared-but-unprovisioned raises `QueueNotProvisionedError`
   with the queue still absent afterwards; schema-absent still raises
   `SchemaNotInstalledError` (now read off the spawn exception, not a failed retry);
   undeclared still raises `QueueNotDeclaredError`; an unrelated `UndefinedTable` from
   inside `spawn_task` propagates as itself; an outer `atomic` survives the refusal.
   Then delete the create-and-retry branch.
2. Worker refuses at startup. Tests: `absurd_worker` on an unprovisioned declared queue
   exits `CommandError` with the typed message BEFORE any "Started worker" line, and
   creates neither queue nor views. Then restore `aworker_client`'s provisioned-queue
   guard as `QueueNotProvisionedError`, and drop `provision_backend` +
   `report_sync_result` from the command.
3. Sync heals the row-present/tables-absent state. Test: drop a provisioned queue's
   tables leaving its catalog row, run `absurd_sync_queues`, tables are back and the
   command still reports no change. Then make `reconcile_queue` call `create_queue`
   unconditionally.
4. Retire the eight, per the design's inventory — delete the five that only exist for
   self-heal, rewrite the three whose assertion survives.
5. Docs + `admin.py`'s changelist message (source, with its message-asserting test), per
   the design's Docs section. `docs/WHY.md` gets the durable record: explicit-only →
   self-heal → `post_migrate` → back to explicit-only, none of which was ever captured.
6. Examples: real `docker compose up` on all three, watching the worker converge rather
   than reading the compose file.

`feat!` — a breaking change, with the upgrade note from the design.
