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
per- queue create/reconcile into a reusable unit. Worker start reconciles via the
`absurd_worker` command (reports to stdout/stderr). Enqueue create is failure-driven
(spawn → on missing-queue error, create declared queue → retry once). System checks
shrink: W001 dropped, W002 narrowed to storage_mode-drift only.

**Tech Stack:** Django 6 Tasks, absurd_sdk, psycopg3, pytest (function-based), Postgres.

## Global Constraints

- Floor Django 6.0 / Python 3.12; psycopg (v3) backend only.
- `import typing as t` (never `from typing import X`). Absolute imports only.
- Functions contain a verb; no leading-underscore module constants/helpers; helpers
  BELOW their public callers.
- pytest function-based only; autouse `_enable_db` gives DB access (no
  `@pytest.mark. django_db` unless transactional — these tests use
  `@pytest.mark.django_db(transaction= True)`, already the module default in the touched
  files). No mocks / no `unittest.mock.patch`. Drive states with real DB conditions.
  Assert on emitted text.
- ruff `select=ALL`; NO new ignores/noqa without asking.
- System-check `msg` = PROBLEM only; `hint` = RESOLUTION only.
- Settings = source of truth: only queues in `TASKS[...]['QUEUES']` auto-create.
- Enqueue hot path: zero added cost when queue exists (auto-create only in `except`).
- Test env fact: `_reset_absurd_queues` (conftest) leaves the `absurd` schema present
  with ZERO queues before each test — so a declared queue is unprovisioned until
  created. Default `tests/settings.py` declares queues `["default", "other"]`; any other
  name (e.g. `"ghost"`) is UNDECLARED.

---

### Task 1: `reconcile_queue` + `sync_queues` refactor

**Files:**

- Modify: `django_absurd/queues.py` (add `reconcile_queue`; refactor `sync_queues` to
  loop it)
- Test: `tests/test_queue_sync.py`

**Interfaces:**

- Consumes: `get_declared_queues(backend)`, `get_absurd_client(using)`,
  `validate_backend(using)` (from `django_absurd.connection`), `SyncResult`, `Queue`,
  `MUTABLE_OPTION_KEYS`.
- Produces: `reconcile_queue(backend: AbsurdBackend, queue_name: str) -> SyncResult` —
  validates backend, raises `ImproperlyConfigured` if `queue_name` undeclared, creates
  the queue (declared opts) if absent else reconciles mutable policy + appends
  storage_mode warning, maps schema-absent DB errors →
  `ImproperlyConfigured("...Run: manage.py migrate")`. Returns a one-queue `SyncResult`.

- [ ] **Step 1: Write failing tests** in `tests/test_queue_sync.py` (append). Uses
      existing `build_tasks_setting`, `table_exists`, `Queue`. Add imports:
      `from     django_absurd.queues import reconcile_queue, get_absurd_backends` and
      `from     django.db import connection` (already imported).

```python
def get_backend():
    from django_absurd.queues import get_absurd_backends

    return get_absurd_backends()["default"]


def test_reconcile_queue_creates_when_absent(settings):
    settings.TASKS = build_tasks_setting({"q": {}})
    result = reconcile_queue(get_backend(), "q")
    assert result.created == ["q"]
    assert Queue.objects.filter(queue_name="q").exists()
    assert table_exists("t_q")


def test_reconcile_queue_is_idempotent(settings):
    settings.TASKS = build_tasks_setting({"q": {}})
    reconcile_queue(get_backend(), "q")
    second = reconcile_queue(get_backend(), "q")
    assert second.created == []
    assert second.reconciled == ["q"]
    assert Queue.objects.filter(queue_name="q").count() == 1


def test_reconcile_queue_applies_mutable_policy(settings):
    settings.TASKS = build_tasks_setting({"q": {"cleanup_limit": 100}})
    reconcile_queue(get_backend(), "q")
    settings.TASKS = build_tasks_setting({"q": {"cleanup_limit": 250}})
    reconcile_queue(get_backend(), "q")
    assert Queue.objects.get(queue_name="q").cleanup_limit == 250


def test_reconcile_queue_warns_on_storage_mode_drift(settings):
    settings.TASKS = build_tasks_setting({"q": {}})
    reconcile_queue(get_backend(), "q")
    settings.TASKS = build_tasks_setting({"q": {"storage_mode": "partitioned"}})
    result = reconcile_queue(get_backend(), "q")
    assert result.storage_warnings
    assert "storage_mode" in result.storage_warnings[0]


def test_reconcile_queue_undeclared_raises(settings):
    settings.TASKS = build_tasks_setting({"q": {}})
    with pytest.raises(ImproperlyConfigured, match="ghost"):
        reconcile_queue(get_backend(), "ghost")


def test_reconcile_queue_schema_absent_raises_migrate(settings):
    settings.TASKS = build_tasks_setting({"q": {}})
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS absurd CASCADE")
    try:
        with pytest.raises(ImproperlyConfigured, match="migrate"):
            reconcile_queue(get_backend(), "q")
    finally:
        call_command("migrate", "django_absurd", "zero", verbosity=0)
        call_command("migrate", "django_absurd", verbosity=0)
```

- [ ] **Step 2: Run, verify FAIL.**
      `uv run pytest tests/test_queue_sync.py -k reconcile -v` Expected: FAIL
      (ImportError: cannot import name `reconcile_queue`).

- [ ] **Step 3: Implement (prose).** In `queues.py`, add
      `reconcile_queue(backend,     queue_name)` placed BELOW `sync_queues` (or above —
      keep public funcs grouped; it is public). Body, minimal: call
      `validate_backend(backend.database)`; read
      `declared =     get_declared_queues(backend)`; if `queue_name not in declared`,
      raise `ImproperlyConfigured` whose message NAMES the queue and points at
      `TASKS QUEUES` (problem+pointer, one string). Build a fresh `SyncResult`. Get
      `client =     get_absurd_client(backend.database)`. Wrap the DB work in
      `try/except ProgrammingError` (`from django.db.utils import ProgrammingError`,
      already used elsewhere) → re-raise
      `ImproperlyConfigured("Absurd schema is not installed. Run: manage.py migrate")`.
      Inside: query `Queue.objects.using(db).filter(queue_name=queue_name)`; if not
      present → `client.create_queue(queue_name, **declared[queue_name])` and append to
      `result.created`; else → apply mutable opts
      (`{k: v ... if k in MUTABLE_OPTION_KEYS}` via `client.set_queue_policy`), append
      to `result.reconciled`, and if `"storage_mode"     in opts` and it differs from
      the existing row's `storage_mode`, append the existing storage_mode warning string
      (reuse the exact wording currently in `sync_queues`). Return `result`. Refactor
      `sync_queues(backend)` to: build `SyncResult()`, loop
      `for name in get_declared_queues(backend)`, call `reconcile_queue(backend, name)`,
      and extend the three result lists from each. Keep the existing
      `MUTABLE_OPTION_KEYS` constant; move its drift/warning logic into
      `reconcile_queue`.

- [ ] **Step 4: Run reconcile tests + full sync regression.**
      `uv run pytest tests/test_queue_sync.py -v` Expected: PASS (new + all existing —
      `test_sync_creates_with_options_and_model_maps`,
      `_reconciles_changed_option_     idempotent`, `_non_destructive`,
      `_list_shorthand`, `_screams_on_non_postgres_backend`,
      `_reports_nothing_when_no_absurd_backend`).

- [ ] **Step 5: Commit.**

```bash
git add django_absurd/queues.py tests/test_queue_sync.py
git commit -m "feat: reconcile_queue (per-queue create+reconcile); sync_queues loops it"
```

---

### Task 2: Enqueue auto-create (failure-driven, retry once)

**Files:**

- Modify: `django_absurd/backends.py` (`AbsurdBackend.enqueue`, ~lines 42-86)
- Test: `tests/test_enqueue.py`

**Interfaces:**

- Consumes: `get_declared_queues`
  (`from django_absurd.queues import get_declared_queues`), existing
  `build_absurd_client`, `client.create_queue`, `psycopg.errors`.
- Produces: enqueue auto-creates a declared-but-missing queue then retries spawn once;
  undeclared queue → `ImproperlyConfigured`; schema absent →
  `ImproperlyConfigured(migrate)`.

- [ ] **Step 1: Repoint + add failing tests** in `tests/test_enqueue.py`. REPLACE
      `test_enqueue_to_unprovisioned_queue_raises_clear_error` (it used declared
      `default`, which now auto-creates) and REFRAME
      `test_enqueue_error_does_not_poison_atomic_block`. Keep
      `test_enqueue_with_absent_schema_raises_clear_error` (still green). New/updated
      tests (use existing imports: `add`, `Group`, `transaction`, `connection`,
      `ImproperlyConfigured`; add `from tests.tasks import make_group` if not present —
      check file; `make_group` creates a Group named by its arg and is in
      `tests/tasks.py`):

```python
def test_enqueue_auto_creates_declared_queue_and_runs():
    # 'default' is declared but unprovisioned (no absurd_sync_queues). Enqueue must
    # auto-create it, then the worker runs the task end-to-end.
    result = make_group.enqueue("auto")
    call_command("absurd_worker", queue="default", burst=True)
    assert Group.objects.filter(name="auto").exists()


def test_enqueue_to_undeclared_queue_raises():
    with pytest.raises(ImproperlyConfigured, match="ghost"):
        add.using(queue_name="ghost").enqueue(1, 2)


def test_enqueue_auto_create_survives_outer_atomic():
    # Auto-create + retry happens inside the savepoint; the surrounding atomic() stays
    # usable and the task persists after commit.
    with transaction.atomic():
        make_group.enqueue("inatomic")
        assert Group.objects.count() == 0  # nothing committed yet
    call_command("absurd_worker", queue="default", burst=True)
    assert Group.objects.filter(name="inatomic").exists()
```

      Add `from django.core.management import call_command` if absent. Delete the old
      `test_enqueue_to_unprovisioned_queue_raises_clear_error` body and the
      `test_enqueue_error_does_not_poison_atomic_block` body, replaced by the above.

- [ ] **Step 2: Run, verify FAIL.**
      `uv run pytest tests/test_enqueue.py -k "auto_create or     undeclared" -v`
      Expected: FAIL — `test_enqueue_to_undeclared_queue_raises` currently raises with
      the old "absurd_sync_queues" message (no queue-name match guaranteed) and the
      auto-create tests fail (spawn raises, no retry).

- [ ] **Step 3: Implement (prose).** In `enqueue`, wrap the existing
      `with transaction.atomic(savepoint=True): spawn` in the current `try`. CHANGE the
      `except (UndefinedTable, UndefinedFunction, InvalidSchemaName)` handler: instead
      of raising the old "run absurd_sync_queues" message, (a) read
      `declared =     get_declared_queues(self)`; if `task.queue_name not in declared`,
      raise `ImproperlyConfigured` naming the queue + telling the user to declare it in
      `TASKS QUEUES`; (b) else attempt
      `client.create_queue(task.queue_name,     **declared[task.queue_name])` inside an
      inner `try/except (UndefinedFunction,     InvalidSchemaName)` that re-raises
      `ImproperlyConfigured("Absurd schema is not     installed. Run: manage.py migrate")`
      (schema absent → create can't help); (c) then RETRY the spawn ONCE inside a fresh
      `with transaction.atomic(savepoint=True):`, assigning `spawn_result`. A second
      failure propagates unchanged. The happy-path spawn and the `TaskResult(...)`
      construction below are untouched. Import `get_declared_queues` at module top
      (absolute import).

- [ ] **Step 4: Run enqueue suite.** `uv run pytest tests/test_enqueue.py -v` Expected:
      PASS (new auto-create tests, undeclared-raises, atomic-survives, kept
      schema-absent test, and all other existing enqueue tests).

- [ ] **Step 5: Commit.**

```bash
git add django_absurd/backends.py tests/test_enqueue.py
git commit -m "feat: enqueue auto-creates a declared queue on missing-table, retries once"
```

---

### Task 3: Worker `aworker_client` — drop the not-provisioned block

**Files:**

- Modify: `django_absurd/worker.py` (`aworker_client`, ~lines 125-141: remove the
  `if queue not in provisioned: raise ImproperlyConfigured` block; keep the
  schema-absent `except` that maps `list_queues` errors →
  `ImproperlyConfigured(migrate)`)
- Test: `tests/test_worker.py`

**Interfaces:**

- Consumes: existing `aworker_client(backend, queue)`, `backend()` helper, `asyncio`.
- Produces: `aworker_client` no longer raises for an unprovisioned (but schema-present)
  queue; schema-absent still raises `ImproperlyConfigured(migrate)`.

- [ ] **Step 1: Repoint failing test** in `tests/test_worker.py`. REMOVE
      `test_worker_client_unprovisioned_queue_errors` (its premise — aworker_client
      raises on unprovisioned — is deleted). Keep
      `test_worker_client_absent_schema_errors` (still valid). Add a positive test that
      aworker_client opens against a schema-present queue WITHOUT a prior raise (it need
      not be provisioned — list_queues just returns without that queue):

```python
def test_worker_client_opens_without_provisioning_check():
    # No absurd_sync_queues, queue 'default' not provisioned: aworker_client must NOT
    # raise (the provisioned-or-die check is gone). Schema is present.
    async def _enter():
        async with aworker_client(backend(), "default") as client:
            return await client.list_queues()

    queues = asyncio.run(_enter())
    assert "default" not in queues  # unprovisioned, yet no error
```

- [ ] **Step 2: Run, verify state.**
      `uv run pytest tests/test_worker.py -k     "worker_client" -v` Expected: the new
      test FAILS (current code raises ImproperlyConfigured "not provisioned");
      `test_worker_client_absent_schema_errors` passes.

- [ ] **Step 3: Implement (prose).** In `aworker_client`, delete the
      `if queue not in provisioned:` block and its `ImproperlyConfigured` raise. Keep
      the `provisioned = await client.list_queues()` call wrapped in the schema-absent
      `except     (InvalidSchemaName, UndefinedTable, UndefinedFunction)` →
      `ImproperlyConfigured(migrate)` (still the schema guard). `provisioned` is now
      only used by that guard's success path; if it becomes unused, replace the
      assignment with a bare `await client.list_queues()` (kept solely to trigger the
      schema-absent error) — keep a short comment explaining it probes the schema.

- [ ] **Step 4: Run.** `uv run pytest tests/test_worker.py -k "worker_client" -v`
      Expected: PASS (new positive test + schema-absent test).

- [ ] **Step 5: Commit.**

```bash
git add django_absurd/worker.py tests/test_worker.py
git commit -m "refactor: aworker_client drops provisioned-or-die check (queues auto-create)"
```

---

### Task 4: `absurd_worker` command — reconcile + report; shared report helper

**Files:**

- Modify: `django_absurd/management/commands/absurd_worker.py` (`handle`: reconcile +
  report before `run_worker`)
- Modify: `django_absurd/management/commands/absurd_sync_queues.py` (use the shared
  report helper)
- Modify: `django_absurd/queues.py` (add shared `write_sync_report` helper)
- Test: `tests/test_worker.py`

**Interfaces:**

- Consumes: `reconcile_queue(backend, queue_name)` (Task 1), `SyncResult`, the command's
  `self.stdout`/`self.stderr`/`self.style`.
- Produces: `write_sync_report(command, result, prefix="")` in `queues.py` — writes
  `Created: ...` / `Reconciled: ...` to `command.stdout`, `No queues to sync.` when both
  empty (sync_queues path keeps that wording via prefix), and storage_warnings to
  `command.stderr` (`command.style.WARNING`). `absurd_worker.handle` calls
  `reconcile_queue` (wrapped → `CommandError`), then `write_sync_report(self, result)`,
  then `run_worker(...)`.

- [ ] **Step 1: Write failing tests** in `tests/test_worker.py` (uses existing
      `run_absurd_worker`/`call_command`, `capsys`, `backend`, `make_group`,
      `get_task_     result`, `connection`, `CommandError`). REPOINT
      `test_command_maps_improperly_configured_to_commanderror` (its old trigger —
      declared- but-unsynced queue → error — now auto-creates) to a schema-absent
      trigger. New tests:

```python
def test_worker_command_reports_created_on_unprovisioned_queue(capsys):
    make_group.enqueue("rep")
    call_command("absurd_worker", queue="default", burst=True)
    out = capsys.readouterr().out
    assert "Created: default" in out


def test_worker_command_reports_reconciled_when_already_provisioned(capsys):
    call_command("absurd_sync_queues")
    capsys.readouterr()  # drop sync output
    call_command("absurd_worker", queue="default", burst=True)
    out = capsys.readouterr().out
    assert "Reconciled: default" in out


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
    err = capsys.readouterr().err
    assert "storage_mode" in err


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

      Then REPLACE `test_command_maps_improperly_configured_to_commanderror`'s body so it
      asserts the schema-absent → CommandError path (or delete it as redundant with
      `test_worker_command_schema_absent_errors_migrate` — note in the commit which).

- [ ] **Step 2: Run, verify FAIL.**
      `uv run pytest tests/test_worker.py -k     "worker_command" -v` Expected: FAIL (no
      reconcile/report yet; no `write_sync_report`).

- [ ] **Step 3: Implement (prose).** (a) In `queues.py`, add
      `write_sync_report(command,     result, prefix="")`: mirror the current
      `absurd_sync_queues.Command.report_result` logic but write via
      `command.stdout.write` / `command.stderr.write` / `command.style.WARNING` (problem
      text lives in `SyncResult`). (b) Refactor
      `absurd_sync_queues.Command.report_result` to delegate to
      `write_sync_report(self,     result, prefix)` (behavior identical — its existing
      tests must stay green). (c) In `absurd_worker.Command.handle`, after the existing
      backend/queue resolution and BEFORE building `WorkerOptions`/calling `run_worker`:
      call `reconcile_queue(backend, queue)` inside
      `try/except ImproperlyConfigured → CommandError` (reuse/extend the existing
      mapping at the bottom), then `write_sync_report(self, result)`. Import
      `reconcile_queue` and `write_sync_report` (absolute imports). Leave `run_worker`
      untouched. Keep `report_result`'s "No queues to sync." wording in the helper for
      the sync command; the worker passes one queue so it always reports Created or
      Reconciled.

- [ ] **Step 4: Run worker + sync suites.**
      `uv run pytest tests/test_worker.py     tests/test_queue_sync.py -v` Expected:
      PASS (new worker-command tests + unchanged sync command reporting tests).

- [ ] **Step 5: Commit.**

```bash
git add django_absurd/management/commands/absurd_worker.py \
  django_absurd/management/commands/absurd_sync_queues.py django_absurd/queues.py \
  tests/test_worker.py
git commit -m "feat: absurd_worker reconciles served queue on boot, reports to stdout/stderr"
```

---

### Task 5: System checks cleanup (drop W001, narrow W002 to storage_mode)

**Files:**

- Modify: `django_absurd/checks.py` (drop W001 + `W001_MSG`/`W001_HINT`; narrow W002 to
  storage_mode-drift; delete `has_option_drifted`, `parse_interval`,
  `DURATION_OPTION_KEYS`, `SCALAR_OPTION_KEYS`; reword `W002_MSG`/`W002_HINT`)
- Test: `tests/test_checks.py`

**Interfaces:**

- Consumes: existing `query_queue_state(alias, declared)`, `Queue`,
  `get_declared_queues`.
- Produces: `query_queue_state` returns `[]` for unreachable / schema-absent / missing /
  mutable-drift; returns `[W002]` ONLY when an existing queue's declared `storage_mode`
  differs from the DB.

- [ ] **Step 1: Repoint + add failing tests** in `tests/test_checks.py`. Changes:
  - DELETE `test_schema_absent_warns_migrate_first` (W001 gone). Optionally replace with
    a silence assertion (below).
  - REPOINT `test_drift_warns_run_sync` (missing queue → was W002) to assert NO W002.
  - REPOINT `test_option_drift_warns` and `test_duration_drift_warns` (mutable drift) to
    assert NO W002.
  - REPOINT/REMOVE `test_mixed_missing_and_drifted_hint_names_both` (missing + mutable;
    now neither) → assert NO W002.
  - `test_db_unreachable_is_silent`: remove its now-dead reference to the W001 message
    string; assert only that W002 is absent.
  - ADD `test_storage_mode_drift_warns`.

```python
def test_missing_declared_queue_no_longer_warns(settings, capsys):
    settings.TASKS = build_tasks_setting({"synced": {}})
    call_command("absurd_sync_queues")
    settings.TASKS = build_tasks_setting({"synced": {}, "missing": {}})
    out = run_absurd_check(capsys, databases=["default"])
    assert "absurd.W002" not in out


def test_mutable_option_drift_no_longer_warns(settings, capsys):
    settings.TASKS = build_tasks_setting({"q": {"cleanup_limit": 100}})
    call_command("absurd_sync_queues")
    settings.TASKS = build_tasks_setting({"q": {"cleanup_limit": 250}})
    out = run_absurd_check(capsys, databases=["default"])
    assert "absurd.W002" not in out


def test_schema_absent_is_silent(settings, capsys):
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


def test_storage_mode_drift_warns(settings, capsys):
    settings.TASKS = build_tasks_setting({"q": {}})
    call_command("absurd_sync_queues")  # 'q' created unpartitioned
    settings.TASKS = build_tasks_setting({"q": {"storage_mode": "partitioned"}})
    out = run_absurd_check(capsys, databases=["default"])
    assert "absurd.W002" in out
    assert "storage_mode" in out
    assert "q" in out
```

      Delete the bodies of the four repointed tests and replace with the equivalents above
      (rename as shown). `test_in_sync_no_warning` (top of file) references the W001 message
      string — drop that W001 assertion line, keep the W002 one.

- [ ] **Step 2: Run, verify FAIL.** `uv run pytest tests/test_checks.py -v` Expected:
      the new/repointed tests FAIL (current code still warns on missing/mutable drift
      and emits W001).

- [ ] **Step 3: Implement (prose).** In `checks.py`: remove `W001_MSG`, `W001_HINT`,
      `DURATION_OPTION_KEYS`, `SCALAR_OPTION_KEYS`, `has_option_drifted`,
      `parse_interval`. Rewrite `query_queue_state(alias, declared)`: wrap the
      `Queue.objects` read in `except (OperationalError, ProgrammingError): return []`
      (schema-absent now silent, folded with unreachable — no W001). Compute
      `drift = [name for name in declared if     name in actual and declared[name].get("storage_mode") and declared[name]["storage_mode"]     != actual[name].storage_mode]`.
      If `drift`, return one
      `DjangoWarning(W002_MSG, hint=     f"{W002_HINT} Affected: {', '.join(drift)}", id="absurd.W002")`.
      Reword the constants:
      `W002_MSG = "django-absurd: a queue's declared storage_mode differs from the database     (storage_mode is immutable)."`
      (problem only);
      `W002_HINT = "Recreate the queue, or     revert the declared storage_mode."`
      (resolution only — the affected names are appended at call site). Drop the
      now-unused imports (`timedelta`, `connections` if only used by `parse_interval`;
      verify with ruff). `check_absurd_config` (E001-E005) untouched.

- [ ] **Step 4: Run check suite + full suite.** `uv run pytest tests/test_checks.py -v`
      then `uv run pytest` Expected: PASS. Confirm ruff clean:
      `uv run ruff check     django_absurd/checks.py` (no unused imports/dead code).

- [ ] **Step 5: Commit.**

```bash
git add django_absurd/checks.py tests/test_checks.py
git commit -m "refactor: drop W001, narrow W002 to storage_mode drift (queues auto-create)"
```

---

## Docs (fold into the final review task or a trailing commit)

- `README.md`: Setup section currently lists `absurd_sync_queues` as a required step —
  reword to "declared queues are created automatically on first enqueue / worker start;
  `absurd_sync_queues` remains for explicit/eager provisioning and policy
  reconciliation."
- `examples/README.md`: the `compose up` flow runs `absurd_sync_queues` explicitly —
  leave the example as-is (still valid) but note it's now optional.

## Self-review notes (coverage vs spec)

- Both seams: Task 2 (enqueue) + Tasks 1/3/4 (worker start). ✓
- Always-on, declared-bounded, settings source of truth: enqueue + reconcile read
  `get_declared_queues`. ✓
- `absurd_sync_queues` unchanged externally: Task 1 refactor is behavior-neutral
  (regression tests retained); Task 4 only reuses its report helper. ✓
- Worker reports to stdout/stderr: Task 4. ✓
- Schema-absent → `ImproperlyConfigured(migrate)` at both seams: Task 1 (reconcile) +
  Task 2 (enqueue). ✓
- Checks: W001 dropped, W002 storage_mode-only, dead code removed: Task 5. ✓
- Test inversions enumerated in spec all assigned to a task. ✓
