# Explicit provisioning — plan

Design:
[`docs/specs/2026-08-17-explicit-provisioning-design.md`](../specs/2026-08-17-explicit-provisioning-design.md).

Two PRs, deliberately. The suites currently self-heal through the two paths the design
deletes, so removing them and repairing ~95 tests in one diff tangles two signals: a red
suite would not say whether the tests were wrong or the change was. Phase 1 makes the
test suite stop depending on self-heal while self-heal is still there — a no-op against
today's code — so phase 2 lands against a suite that already proves nothing needs it.

## Phase 1 — test fixture, no prod code (own PR, off origin/main)

Goal: `tests/` passes with runtime self-heal disabled, while shipping only test changes.

`_isolate_queues` (`tests/conftest.py`) drops all topology before AND after its test.
The "after" is what leaves the hole: the next test to enqueue is repaired today by
`backends.enqueue`'s auto-create or by `utils.start_worker*` provisioning at boot.

- Teardown re-provisions: drop the schema, then put the declared set back. Fixture
  contract becomes "hermetic AND leaves a clean baseline". Setup keeps its drop — a test
  that varies topology still starts from nothing.
- Reuse the existing provisioning entry point rather than a test-local reimplementation.
- Any test that genuinely needs an unprovisioned queue provisions or drops explicitly
  inside the test, where a reader can see it.

Verification is the point of this phase, and it is a temporary, UNCOMMITTED prod edit:
neuter `backends.enqueue`'s auto-create branch and `absurd_worker`'s `provision_backend`
call, run all three suites, fix what fails, restore the prod code, confirm green again.
Ship the test diff only.

Order:

1. Baseline: three suites green, serial and `-n4`. Record counts.
2. Change the fixture. Suites stay green — a no-op today. Both modes.
3. Temporarily disable self-heal locally. Run all three suites, serial and `-n4`.
   Inventory every failure.
4. Repair each failing test at its own call site (explicit provisioning where the test
   changed topology; explicit drop where the test wants the unprovisioned state). No
   prod code in the repair.
5. Restore the prod code. Suites green again, both modes, plus `tox -e dev`.
6. Commit test-only. Diff must touch nothing under `django_absurd/`.

Phase 1 ships no behavior change, so it is `test:` — invisible in the changelog, which
is correct.

## Phase 2 — remove self-heal (follow-up PR)

Preconditions: phase 1 merged; suite proven independent of self-heal.

RED first, per the design's Testing section. Sequence:

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
4. Docs + `admin.py`'s changelist message (source, with its message-asserting test), per
   the design's Docs section. `docs/WHY.md` gets the durable record.
5. Examples: real `docker compose up` on all three, watching the worker converge rather
   than reading the compose file.

Phase 2 is `feat!` — a breaking change with the upgrade note from the design.
