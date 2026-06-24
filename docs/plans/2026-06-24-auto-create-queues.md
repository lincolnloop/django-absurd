# Auto-create queues — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) tracking.
> Project CLAUDE.md OVERRIDES the writing-plans skill: plans show TESTS in full (RED
> first) and describe implementation in PROSE — never finished implementation blocks
> (coding-ahead = TDD violation). Each impl step: prose only.

**Goal:** Declared queues materialize automatically on first use (enqueue + worker
start); the `absurd_sync_queues` command is no longer required, and obsolete
system-check warnings are removed.

**Architecture:** A new `reconcile_queue(backend, queue_name)` factors `sync_queues`'
per- queue create/reconcile into a reusable, DRIFT-GATED unit (only writes policy when
declared opts actually differ). Worker start reconciles via the `absurd_worker` command
(reports to stdout/stderr). Enqueue create is failure-driven (spawn → on missing-queue
error, create declared queue → retry once). System checks shrink: W001 dropped, W002
narrowed to storage_mode-drift only.

**Tech Stack:** Django 6 Tasks, absurd_sdk, psycopg3, pytest (function-based), Postgres.

## Global Constraints

- Floor Django 6.0 / Python 3.12; psycopg (v3) backend only.
- `import typing as t` (never `from typing import X`). Absolute imports only.
- Functions contain a verb; no leading-underscore module constants/helpers; helpers
  BELOW their public callers.
- pytest function-based only; autouse `_enable_db` gives DB access. Touched test modules
  already set `pytestmark = pytest.mark.django_db(transaction=True)`. No mocks / no
  `unittest.mock.patch`. Drive states with REAL DB conditions.
- **Test at the ENTRYPOINTS** (the `enqueue` API + management commands + system checks
  run via `call_command`) — NOT by calling `reconcile_queue`/internal helpers directly.
  Assert on emitted text AND, for reconciliation, on the DB `Queue` row VALUES (prove
  the write happened). PARAMETRIZE when multiple cases share structure.
- ruff `select=ALL`; NO new ignores/noqa without asking.
- System-check `msg` = PROBLEM only; `hint` = RESOLUTION only.
- Settings = source of truth: only queues in `TASKS[...]['QUEUES']` auto-create.
- Enqueue hot path: zero added cost when queue exists (auto-create only in `except`).
- Test env facts: `_reset_absurd_queues` (conftest) leaves the `absurd` schema present
  with ZERO queues before each test (declared queue is unprovisioned until created).
  Default `tests/settings.py` declares queues `["default", "other"]`; any other name
  (e.g. `"ghost"`) is UNDECLARED. `tests/tasks.py` has `make_group(name)` (creates a
  `Group`).

**Task order rationale:** checks cleanup (Task 1) removes `parse_interval`/
`has_option_drifted` from `checks.py` FIRST, so Task 2 can add fresh drift-gating to
`queues.py` with no duplication window.

---

### Task 1: System checks cleanup (drop W001, narrow W002 to storage_mode)

**Files:**

- Modify: `django_absurd/checks.py` (drop W001 + `W001_MSG`/`W001_HINT`; narrow W002 to
  storage_mode-drift; remove `has_option_drifted`, `parse_interval`,
  `DURATION_OPTION_KEYS`, `SCALAR_OPTION_KEYS`; reword `W002_MSG`/`W002_HINT`)
- Test: `tests/test_checks.py`

**Interfaces:**

- Produces: `query_queue_state(alias, declared)` returns `[]` for unreachable / schema-
  absent / missing / mutable-drift; returns one `absurd.W002` `Warning` ONLY when an
  EXISTING queue's declared `storage_mode` differs from the DB.

- [ ] **Step 1: Repoint + add failing tests** in `tests/test_checks.py`. Uses existing
      `build_tasks_setting`, `run_absurd_check`, `connection`, `call_command`. Changes:
  - DELETE `test_schema_absent_warns_migrate_first`; replace with the silence test
    below.
  - DELETE the bodies of `test_drift_warns_run_sync`, `test_option_drift_warns`,
    `test_duration_drift_warns`, `test_mixed_missing_and_drifted_hint_names_both` —
    folded into the parametrized `test_self_healing_drift_no_longer_warns` below.
  - In `test_in_sync_no_warning`: DROP the W001-message assertion line (keep the W002
    one).
  - In `test_db_unreachable_is_silent`: DROP the W001-message assertion line (keep
    W002).
  - ADD:

```python
import pytest


@pytest.mark.parametrize(
    "after",
    [
        {"synced": {}, "missing": {}},
        {"synced": {"cleanup_limit": 250}},
        {"synced": {"cleanup_ttl": "60 days"}},
    ],
    ids=["missing-queue", "mutable-scalar", "mutable-duration"],
)
def test_self_healing_drift_no_longer_warns(settings, capsys, after):
    settings.TASKS = build_tasks_setting({"synced": {}})
    call_command("absurd_sync_queues")
    settings.TASKS = build_tasks_setting(after)
    out = run_absurd_check(capsys, databases=["default"])
    assert "absurd.W002" not in out


def test_storage_mode_drift_warns(settings, capsys):
    settings.TASKS = build_tasks_setting({"q": {}})
    call_command("absurd_sync_queues")  # 'q' created unpartitioned
    settings.TASKS = build_tasks_setting({"q": {"storage_mode": "partitioned"}})
    out = run_absurd_check(capsys, databases=["default"])
    assert "absurd.W002" in out
    assert "storage_mode" in out
    assert "q" in out


def test_schema_absent_check_is_silent(settings, capsys):
    settings.TASKS = build_tasks_setting({"a": {}})
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS absurd CASCADE")
    try:
        out = run_absurd_check(capsys, databases=["default"])
        assert "absurd.W001" not in out
        assert "absurd.W002" not in out
    finally:
        call_command("migrate", "django_absurd", "zero", verbosity=0)
        call_command("migrate", "django_absurd", verbosity=0)
```

- [ ] **Step 2: Run, verify FAIL.** `uv run pytest tests/test_checks.py -v` Expected:
      the new/parametrized tests FAIL (current code still emits W001 and warns on
      missing / mutable drift).

- [ ] **Step 3: Implement (prose).** In `checks.py`: remove `W001_MSG`, `W001_HINT`,
      `DURATION_OPTION_KEYS`, `SCALAR_OPTION_KEYS`, `has_option_drifted`,
      `parse_interval`. Rewrite `query_queue_state(alias, declared)`: read
      `actual = {q.queue_name: q for q in     Queue.objects.using(alias).filter(queue_name__in=declared)}`
      inside `try/except     (OperationalError, ProgrammingError): return []`
      (schema-absent now silent, folded with unreachable — W001 gone). Compute
      `drift = [name for name in declared if name in     actual and declared[name].get("storage_mode") and declared[name]["storage_mode"] !=     actual[name].storage_mode]`.
      If `drift`, return one
      `DjangoWarning(W002_MSG,     hint=f"{W002_HINT} Affected: {', '.join(drift)}", id="absurd.W002")`
      else `[]`. Reword constants:
      `W002_MSG = "django-absurd: a queue's declared storage_mode differs from     the database (storage_mode is immutable)."`
      (problem only);
      `W002_HINT = "Recreate the     queue, or revert the declared storage_mode."`
      (resolution only — names appended at call site). Remove now-unused imports
      (`timedelta`; `connections` only if nothing else uses it — verify with ruff).
      `check_absurd_config` (E001–E005) untouched.

- [ ] **Step 4: Run + ruff.** `uv run pytest tests/test_checks.py -v` (PASS) then
      `uv run ruff check django_absurd/checks.py` (clean — no unused imports / dead
      code).

- [ ] **Step 5: Commit.**

```bash
git add django_absurd/checks.py tests/test_checks.py
git commit -m "refactor: drop W001, narrow W002 to storage_mode drift (queues auto-create)"
```

---

### Task 2: `reconcile_queue` (drift-gated) + `sync_queues` refactor

**Files:**

- Modify: `django_absurd/queues.py` (add `reconcile_queue` + a drift helper +
  `parse_interval`; refactor `sync_queues` to loop `reconcile_queue`)
- Test: `tests/test_queue_sync.py` (regression only — no new unit tests; entrypoint =
  `absurd_sync_queues` command)

**Interfaces:**

- Consumes: `get_declared_queues(backend)`, `get_absurd_client(using)`,
  `validate_backend` (`django_absurd.connection`), `SyncResult`, `Queue`,
  `MUTABLE_OPTION_KEYS`.
- Produces: `reconcile_queue(backend: AbsurdBackend, queue_name: str) -> SyncResult` —
  validates backend; raises `ImproperlyConfigured` if `queue_name` undeclared (message
  names it); maps schema-absent DB errors →
  `ImproperlyConfigured("...Run: manage.py migrate")`; creates the queue (declared opts)
  if absent (→ `created`); else DRIFT-GATES: only `set_queue_policy` + record
  `reconciled` if a declared MUTABLE opt differs from the DB row, else no-op;
  storage_mode drift → `storage_warnings` regardless. Unchanged existing queue → empty
  `SyncResult`.

- [ ] **Step 1: Confirm the regression net.** No new tests authored here
      (entrypoint-only rule: `reconcile_queue` is exercised through the
      `absurd_sync_queues` command, already covered, and through the `absurd_worker`
      command in Task 5). The existing `tests/test_queue_sync.py` tests are the gate —
      especially these, which assert DB VALUES and thus prove create + drift-gated
      reconcile:
  - `test_sync_creates_with_options_and_model_maps` (create →
    `storage_mode`/`cleanup_ttl` in DB; `t_x` table exists),
  - `test_sync_reconciles_changed_option_idempotent` (change `cleanup_limit` 100→250 →
    DB == 250; re-sync → still 250),
  - `test_non_destructive`, `test_list_shorthand`,
    `test_sync_command_screams_on_non_postgres_backend`,
    `test_sync_command_reports_nothing_when_no_absurd_backend`. Run them now to capture
    the green baseline: `uv run pytest tests/test_queue_sync.py   -v` (all PASS on
    current code).

- [ ] **Step 2: Implement (prose).** In `queues.py`:
  - Add `parse_interval(alias, interval_str) -> timedelta` (relocated from the
    now-cleaned `checks.py`; runs `SELECT %s::interval`) and a drift helper
    `mutable_options_drifted(alias, opts, queue_row) -> bool` that iterates
    `MUTABLE_OPTION_KEYS`, comparing declared values to `getattr(queue_row, key)` —
    parsing interval-typed keys via `parse_interval`, comparing the rest directly.
    (`storage_mode` is NOT in `MUTABLE_OPTION_KEYS`, so it's excluded — handled
    separately as a warning.) Place helpers BELOW `reconcile_queue`.
  - Add `reconcile_queue(backend, queue_name)`: `validate_backend(backend.database)`;
    read `declared = get_declared_queues(backend)`; if `queue_name not in declared`
    raise `ImproperlyConfigured` naming the queue + pointing at `TASKS QUEUES`.
    `opts = declared[queue_name]`; `result = SyncResult()`;
    `client = get_absurd_client(backend.database)`. Wrap DB work in
    `try/except ProgrammingError` (`from django.db.utils import ProgrammingError`) →
    raise
    `ImproperlyConfigured("Absurd schema is not installed. Run: manage.py migrate")`.
    Inside: fetch the row via
    `Queue.objects.using(db).filter(queue_name=queue_name).first()`. If `None`:
    `client.create_queue(queue_name, **opts)`; `result.created.append(queue_name)`.
    Else: build
    `mutable_opts = {k: v for k, v in opts.items() if k in MUTABLE_OPTION_KEYS}`; if
    `mutable_opts and mutable_options_drifted(db, mutable_opts, row)`:
    `client. set_queue_policy(queue_name, **mutable_opts)`;
    `result.reconciled.append(queue_name)`. In all existing-row cases, if
    `"storage_mode" in opts and opts["storage_mode"] != row.storage_mode`: append the
    existing storage_mode warning string (reuse current wording from `sync_queues`).
    Return `result`.
  - Refactor `sync_queues(backend)`: `result = SyncResult()`;
    `for name in get_declared_queues(backend): r = reconcile_queue(backend, name)`;
    extend `result.created/reconciled/storage_warnings` from `r`. Return `result`. (Drop
    the inline create/reconcile loop body now living in `reconcile_queue`. Keep
    `MUTABLE_OPTION_KEYS`.)

- [ ] **Step 3: Run regression.** `uv run pytest tests/test_queue_sync.py -v` Expected:
      PASS (drift-gating keeps the idempotent test green: first change writes 250,
      re-run is a no-op, value stays 250). Then
      `uv run ruff check django_absurd/queues.py`.

- [ ] **Step 4: Commit.**

```bash
git add django_absurd/queues.py tests/test_queue_sync.py
git commit -m "feat: reconcile_queue (drift-gated per-queue create+reconcile); sync_queues loops it"
```

---

### Task 3: Enqueue auto-create (failure-driven, retry once)

**Files:**

- Modify: `django_absurd/backends.py` (`AbsurdBackend.enqueue`, ~lines 42-86)
- Test: `tests/test_enqueue.py`

**Interfaces:**

- Consumes: `get_declared_queues`
  (`from django_absurd.queues import get_declared_queues`), existing
  `build_absurd_client`, `client.create_queue`, `psycopg.errors`.
- Produces: enqueue auto-creates a declared-but-missing queue then retries spawn once;
  undeclared queue → `ImproperlyConfigured` naming it; schema absent →
  `ImproperlyConfigured(migrate)`.

- [ ] **Step 1: Repoint + add failing tests** in `tests/test_enqueue.py`. REPLACE the
      body of `test_enqueue_to_unprovisioned_queue_raises_clear_error` (used declared
      `default`, now auto-creates) and REFRAME
      `test_enqueue_error_does_not_poison_atomic_block`. KEEP
      `test_enqueue_with_absent_schema_raises_clear_error` (still green — new code
      path). Use existing imports; add `from django.core.management import call_command`
      and `from     tests.tasks import make_group` if absent.

```python
def test_enqueue_auto_creates_declared_queue_and_runs():
    # 'default' declared but unprovisioned (no absurd_sync_queues). Enqueue auto-creates
    # it; the worker then runs the task end-to-end.
    make_group.enqueue("auto")
    call_command("absurd_worker", queue="default", burst=True)
    assert Group.objects.filter(name="auto").exists()


def test_enqueue_to_undeclared_queue_raises():
    with pytest.raises(ImproperlyConfigured, match="ghost"):
        add.using(queue_name="ghost").enqueue(1, 2)


def test_enqueue_auto_create_survives_outer_atomic():
    with transaction.atomic():
        make_group.enqueue("inatomic")
        assert Group.objects.count() == 0  # nothing committed yet
    call_command("absurd_worker", queue="default", burst=True)
    assert Group.objects.filter(name="inatomic").exists()
```

      Delete the old `test_enqueue_to_unprovisioned_queue_raises_clear_error` and
      `test_enqueue_error_does_not_poison_atomic_block` bodies (replaced above).

- [ ] **Step 2: Run, verify FAIL.**
      `uv run pytest tests/test_enqueue.py -k "auto_create or     undeclared" -v`
      Expected: FAIL (spawn raises with the old "absurd_sync_queues" message; no
      auto-create/retry).

- [ ] **Step 3: Implement (prose).** In `enqueue`, keep the existing
      `try: with     transaction.atomic(savepoint=True): spawn`. CHANGE the
      `except (UndefinedTable,     UndefinedFunction, InvalidSchemaName)` handler: (a)
      `declared =     get_declared_queues(self)`; if `task.queue_name not in declared`
      raise `ImproperlyConfigured` naming the queue + telling the user to declare it in
      `TASKS     QUEUES`; (b) else
      `try: client.create_queue(task.queue_name,     **declared[task.queue_name]) except (UndefinedFunction, InvalidSchemaName): raise     ImproperlyConfigured("Absurd schema is not installed. Run: manage.py migrate") from     None`;
      (c) RETRY spawn ONCE inside a fresh `with transaction.atomic(savepoint=True):`,
      reassigning `spawn_result`. Second failure propagates. The happy-path spawn and
      the `TaskResult(...)` construction are untouched. Import `get_declared_queues` at
      module top (absolute).

- [ ] **Step 4: Run enqueue suite.** `uv run pytest tests/test_enqueue.py -v` Expected:
      PASS (new + kept schema-absent test + all other existing enqueue tests).

- [ ] **Step 5: Commit.**

```bash
git add django_absurd/backends.py tests/test_enqueue.py
git commit -m "feat: enqueue auto-creates a declared queue on missing-table, retries once"
```

---

### Task 4: Worker `aworker_client` — drop the not-provisioned block

**Files:**

- Modify: `django_absurd/worker.py` (`aworker_client`, ~lines 125-141)
- Test: `tests/test_worker.py`

**Interfaces:**

- Consumes: existing `aworker_client(backend, queue)`, `backend()` helper, `asyncio`.
- Produces: `aworker_client` no longer raises for an unprovisioned (schema-present)
  queue; schema-absent still raises `ImproperlyConfigured(migrate)`.

- [ ] **Step 1: Repoint test** in `tests/test_worker.py`. REMOVE
      `test_worker_client_unprovisioned_queue_errors` (premise deleted). KEEP
      `test_worker_client_absent_schema_errors`. ADD:

```python
def test_worker_client_opens_without_provisioning_check():
    # No absurd_sync_queues; 'default' unprovisioned (schema present). aworker_client must
    # NOT raise — the provisioned-or-die check is gone.
    async def _enter():
        async with aworker_client(backend(), "default") as client:
            return await client.list_queues()

    assert "default" not in asyncio.run(_enter())  # unprovisioned, yet no error
```

- [ ] **Step 2: Run, verify state.**
      `uv run pytest tests/test_worker.py -k "worker_client"     -v` Expected: the new
      test FAILS (current code raises "not provisioned");
      `test_worker_client_absent_schema_errors` passes.

- [ ] **Step 3: Implement (prose).** In `aworker_client`, delete the
      `if queue not in     provisioned:` block and its raise. Keep
      `await client.list_queues()` wrapped in the schema-absent
      `except (InvalidSchemaName, UndefinedTable, UndefinedFunction) →     ImproperlyConfigured(migrate)`
      (it now only probes the schema). If `provisioned` becomes unused, drop the
      assignment and call `await client.list_queues()` bare, with a short comment that
      it probes for the schema-absent guard.

- [ ] **Step 4: Run.** `uv run pytest tests/test_worker.py -k "worker_client" -v`
      Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add django_absurd/worker.py tests/test_worker.py
git commit -m "refactor: aworker_client drops provisioned-or-die check (queues auto-create)"
```

---

### Task 5: `absurd_worker` command — reconcile + report; shared report helper

**Files:**

- Modify: `django_absurd/management/commands/absurd_worker.py` (`handle`: reconcile +
  report before `run_worker`)
- Modify: `django_absurd/management/commands/absurd_sync_queues.py` (delegate to shared
  helper)
- Modify: `django_absurd/queues.py` (add `write_sync_report`)
- Test: `tests/test_worker.py`

**Interfaces:**

- Consumes: `reconcile_queue` (Task 2), `SyncResult`, `self.stdout`/`self.stderr`/
  `self.style`.
- Produces: `write_sync_report(command, result, prefix="")` in `queues.py` — writes
  `Created: ...` / `Reconciled: ...` to `command.stdout`, `No queues to sync.` when both
  empty, storage_warnings to `command.stderr` (`command.style.WARNING`).
  `absurd_worker.handle` calls `reconcile_queue` (wrapped → `CommandError`), then
  `write_sync_report(self, result)`, then `run_worker(...)`.

- [ ] **Step 1: Write failing tests** in `tests/test_worker.py` (uses existing
      `call_command`, `capsys`, `backend`, `make_group`, `connection`, `CommandError`,
      and `Queue` — add `from django_absurd.models import Queue` if absent). Also DELETE
      `test_command_maps_improperly_configured_to_commanderror` (its old trigger now
      auto- creates; the mapping is covered by the schema-absent test below). New tests:

```python
def test_worker_command_reports_created_on_unprovisioned_queue(capsys):
    make_group.enqueue("rep")
    call_command("absurd_worker", queue="default", burst=True)
    out = capsys.readouterr().out
    assert "Created: default" in out
    assert Queue.objects.filter(queue_name="default").exists()


def test_worker_command_reconciles_changed_mutable_option(settings, capsys):
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"QUEUES": {"default": {"cleanup_limit": 100}}},
        }
    }
    call_command("absurd_sync_queues")
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"QUEUES": {"default": {"cleanup_limit": 250}}},
        }
    }
    capsys.readouterr()  # drop sync output
    call_command("absurd_worker", queue="default", burst=True)
    out = capsys.readouterr().out
    assert "Reconciled: default" in out
    assert Queue.objects.get(queue_name="default").cleanup_limit == 250  # DB proof


def test_worker_command_no_reconcile_when_unchanged(settings, capsys):
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"QUEUES": {"default": {"cleanup_limit": 100}}},
        }
    }
    call_command("absurd_sync_queues")
    before = Queue.objects.get(queue_name="default").cleanup_limit
    capsys.readouterr()
    call_command("absurd_worker", queue="default", burst=True)
    out = capsys.readouterr().out
    assert "Reconciled: default" not in out  # drift-gated: nothing changed
    assert Queue.objects.get(queue_name="default").cleanup_limit == before


def test_worker_command_warns_on_storage_mode_drift(settings, capsys):
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"QUEUES": {"default": {}}},
        }
    }
    call_command("absurd_sync_queues")  # create 'default' unpartitioned
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"QUEUES": {"default": {"storage_mode": "partitioned"}}},
        }
    }
    capsys.readouterr()
    call_command("absurd_worker", queue="default", burst=True)
    assert "storage_mode" in capsys.readouterr().err


def test_worker_command_schema_absent_errors_migrate():
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS absurd CASCADE")
    try:
        with pytest.raises(CommandError, match="migrate"):
            call_command("absurd_worker", queue="default", burst=True)
    finally:
        call_command("migrate", "django_absurd", "zero", verbosity=0)
        call_command("migrate", "django_absurd", verbosity=0)
```

- [ ] **Step 2: Run, verify FAIL.**
      `uv run pytest tests/test_worker.py -k "worker_command"     -v` Expected: FAIL (no
      reconcile/report; no `write_sync_report`).

- [ ] **Step 3: Implement (prose).** (a) In `queues.py`, add
      `write_sync_report(command,     result, prefix="")` mirroring the current
      `absurd_sync_queues.Command.report_result`: write `Created: ...`/`Reconciled: ...`
      (and `No queues to sync.` when both empty) via `command.stdout.write`, and each
      `storage_warnings` entry via `command.stderr.write(command.style.WARNING(...))`.
      (b) Refactor `absurd_sync_queues.Command.report_result` to delegate to
      `write_sync_report(self,     result, prefix)` (behavior identical — its existing
      tests stay green). (c) In `absurd_worker.Command.handle`, AFTER the existing
      backend/queue resolution and BEFORE building `WorkerOptions`:
      `result = reconcile_queue(backend, queue)` inside
      `try/except     ImproperlyConfigured → CommandError` (reuse/extend the existing
      mapping), then `write_sync_report(self, result)`. Import `reconcile_queue` and
      `write_sync_report` (absolute). `run_worker` untouched.

- [ ] **Step 4: Run worker + sync suites.**
      `uv run pytest tests/test_worker.py     tests/test_queue_sync.py -v` Expected:
      PASS (new worker-command tests + unchanged sync reporting tests). Then
      `uv run ruff check django_absurd`.

- [ ] **Step 5: Commit.**

```bash
git add django_absurd/management/commands/absurd_worker.py \
  django_absurd/management/commands/absurd_sync_queues.py django_absurd/queues.py \
  tests/test_worker.py
git commit -m "feat: absurd_worker reconciles served queue on boot, reports to stdout/stderr"
```

---

## Docs (trailing commit after Task 5, or fold into final review)

- `README.md`: the Setup section lists `absurd_sync_queues` as a required step — reword
  to "declared queues are created automatically on first enqueue / worker start;
  `absurd_sync_queues` remains for explicit/eager provisioning and policy
  reconciliation."
- `examples/README.md`: the `compose up` flow runs `absurd_sync_queues` explicitly —
  leave it (still valid) but note it's now optional.

## Self-review (coverage vs spec)

- Both seams: Task 3 (enqueue) + Tasks 2/4/5 (worker start). ✓
- Always-on, declared-bounded, settings source of truth: enqueue + reconcile read
  `get_declared_queues`. ✓
- Drift-gated reconcile ("don't reconcile if unchanged"; prove via DB value): Task 2
  logic + Task 5 entrypoint tests (`reconciles_changed_mutable_option` asserts DB ==
  250; `no_reconcile_when_unchanged` asserts no write). ✓
- Entrypoint-only testing, no `reconcile_queue` unit tests, parametrized where shared
  (Task 1 self-healing-drift). ✓
- `absurd_sync_queues` unchanged externally: Task 2 refactor regression-tested; Task 5
  only reuses its report helper. ✓
- Worker reports to stdout/stderr: Task 5. ✓
- Schema-absent → `ImproperlyConfigured(migrate)` at both seams: Task 2 (reconcile,
  surfaced via Task 5 worker command) + Task 3 (enqueue). ✓
- Checks: W001 dropped, W002 storage_mode-only, helpers relocated to `queues.py`: Tasks
  1 + 2. ✓
- Deletions: `test_command_maps_improperly_configured_to_commanderror` (Task 5),
  `test_worker_client_unprovisioned_queue_errors` (Task 4). ✓
