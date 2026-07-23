# Auto Absurd test cleanup via `_post_teardown` monkeypatch — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Django PARITY for Absurd's non-Django-managed tables — a test with DB access
gets its Absurd state reset automatically, exactly as Django resets its own tables; a
test WITHOUT DB access can't touch Absurd at all. No per-test opt-in, no fixture to
request, no marker.

**Architecture:** monkeypatch `django.test.TransactionTestCase._post_teardown` — wrap
the original so it runs first, then `flush_absurd_state()` (truncate). Patch installed
by `install_absurd_cleanup()` in a new `django_absurd/test.py`, wired from the
`pytest11` plugin's `pytest_configure` (covers every pytest run — pytest-django
fixtures/markers AND unittest-style classes under pytest). The `manage.py test`
(non-pytest `DiscoverRunner`) surface is DEFERRED to a follow-up
([#96](https://github.com/lincolnloop/django-absurd/issues/96));
`install_absurd_cleanup()` is public so a determined `manage.py test` user can wire it
themselves. `_post_teardown` exists only on Django test cases → patching it IS the
detection: no DB test case → hook never fires → Absurd stays blocked. The prior branch's
`absurd_db` fixture + marker are DELETED; `absurd_drain_queue` (unrelated) stays.

**Monkeypatch is BLESSED here (supersedes the repo's blanket ban for this one seam):**
human explicitly approved patching Django test internals at library level — the "no
monkeypatching" testing rule is about test-code hygiene, and still applies to tests
themselves. Precedent: pytest-django patches `BaseDatabaseWrapper.ensure_connection`
(`pytest_django/plugin.py:896`).

**Scope line (load-bearing, unchanged):** SHIPPED cleanup = truncate-only
(`flush_absurd_state()`, rows). `drop_schema=True` (hard queue drop) is INTERNAL-only —
this repo's suites create/vary queue topology as the subject-under-test; a normal
consumer declares queues once and only needs the row reset. Never surfaced in any
fixture/marker/runner/docs.

**Tech Stack:** Django 6.0 / Python 3.12+, pytest 9, pytest-django 4.x, psycopg3.

## Why (context — the pivot)

Prior iteration designed an autouse-fixture detection
(`'_django_db_helper' in request.fixturenames`). Adversarial review found + verification
confirmed a BLOCKING flaw: shipped as a `pytest11` entry-point plugin, an autouse
fixture sets up BEFORE test-requested fixtures, so its teardown runs AFTER
pytest-django's `_django_db_helper` re-installs the DB blocker → `flush_absurd_state`
hits `RuntimeError: Database access not allowed` (hard teardown ERROR) for all DB-access
patterns. Earlier "it works" probes were from unrepresentative environments (in-conftest
fixture position; examples/web never arms the blocker).

The monkeypatch dissolves the ordering problem: pytest-django calls
`test_case._post_teardown()` INSIDE `with django_db_blocker.unblock():`
(`pytest_django/fixtures.py:296`, re-verified this session against the installed 4.x) —
a hook there runs while the DB is unblocked, always.

## Empirical findings (ALL verified this session — cite, don't re-derive)

1. **Patch target.** `_post_teardown` is defined in `vars(TransactionTestCase)`
   (`django/test/testcases.py:1232`); `TestCase` does NOT override it; `SimpleTestCase`
   has its own separate one (`:389`, a no-DB `pass`). Patching `TransactionTestCase`
   fires for `TestCase`/`TransactionTestCase`/`LiveServerTestCase` only — never for
   no-DB tests.
2. **Import safety.** `import django.test` succeeds with settings UNCONFIGURED
   (`settings.configured` stays False) — safe inside `pytest_configure` before
   pytest-django/nanodjango configure Django.
3. **The hook fires through pytest-django for both case types** — probe observed
   teardown wrapper runs with `case=TestCase` (plain `db`) and
   `case=TransactionTestCase` (`transaction=True`).
4. **Plain `TestCase` edge.** Enqueued rows ride the test transaction; Django's rollback
   (`TestCase._fixture_teardown`, inside the original `_post_teardown`) runs BEFORE our
   wrapper code → probe measured `rows-before-flush=0` after a plain-`db` enqueue. A
   truncate there is a harmless no-op on committed state — but it's skippable (below).
5. **Flush cost.** `flush_absurd_state()` (truncate, 1 queue) ≈ **28 ms/call**.
   Unconditional per-test flush: tests/core 69–74 s; with the plain-`TestCase`
   short-circuit: 60.8 s. Short-circuit pays.
6. **Entry-point-position integration WORKS** (the exact position that killed the
   fixture design): in `examples/web` with the patch installed from the real `pytest11`
   `pytest_configure`, a marker-only `transaction=True` enqueue test + follow-up test
   asserting rows truncated AND queue still present — PASSED. Plain-`db` enqueue also
   fine.
7. **examples/web CANNOT host the no-DB proof**: nanodjango configures Django after
   `pytest_configure`, pytest-django's blocker is never armed there — a no-DB
   `add.enqueue(...)` did NOT raise. The no-DB proof lives in `tests/core` via a
   module-level no-op `_enable_db` override (probe verified; blocker message captured
   verbatim in Task 1).
8. **pytest-django reorders tests Django-style** (`plugin.py`
   `pytest_collection_modifyitems`): no-DB, then plain `TestCase`, then transactional
   LAST. (pytest-randomly is NOT a dependency of this repo — order is deterministic
   today, but that reordering plus `--reuse-db` cross-run leakage already make
   cross-test observation pairs fragile.) → Internal mechanism tests must be
   order-independent (direct hook invocation, Task 1); the one cross-test pair lives in
   `examples/web` (deterministic order, dev deps: pytest/pytest-cov/pytest-django only).
9. **Fresh internal-suite experiment (monkeypatch active, blanket reset removed):**
   - `tests/core`: **50 failed / 338** without isolation; failing files EXACTLY 5:
     `test_cleanup.py` (15), `test_enqueue.py` (1), `test_results.py` (14),
     `test_scheduler.py` (17), `test_worker.py` (3). With `_isolate_queues` (drop
     before+after) on those 5 files: **338 pass**, and 3 further repeat runs all green.
     (The old plan's 9-file guess incl. admin files is WRONG under the monkeypatch —
     per-test truncate on transactional tests already keeps admin/orm files clean.)
   - `tests/pg_cron`: **228 pass with NO isolation** (`tests/pg_cron/conftest.py` is
     removed; the shipped auto-cleanup hook's scoped `teardown_crons` + the pg_cron
     tests' own self-cleanup suffice — no `_isolate_queues` needed here).
   - `tests/multidb`: 1 failure (`test_router.py::test_orm_routes_to_alias`, leaked
     `default` queue from stale `--reuse-db` state); green with `_isolate_queues` on
     `test_router.py` only.
   - Truncate genuinely can't fix topology:
     `test_enqueue_with_empty_queues_reports_ undeclared` fails precisely because a
     leaked queue EXISTS (guard unreachable) — drop is required, confirming
     `_isolate_queues` survives the pivot.

## Design decisions (resolved, with justification)

- **Install point:** `pytest_configure` (all pytest runs) only. The `manage.py test`
  surface is DEFERRED (#96) — `install_absurd_cleanup()` is public for the determined.
  **A shipped `AbsurdTestRunner` and `AbsurdCleanupMixin` are both DROPPED** for this
  feature: the runner would force a `TEST_RUNNER` setting + conflict with a consumer's
  own runner; a per-class mixin invites missed classes (silent partial cleanup). Never
  patch in `AppConfig.ready()` (would patch production).
- **Plain `TestCase` short-circuit:** wrapper returns without flushing when the instance
  is a `django.test.TestCase` AND `self._databases_support_transactions()` — the SCOPED
  check Django's own `TestCase._fixture_teardown` uses (`testcases.py:1490`; limited to
  `cls.databases`, values cached in `setUpClass`). NOT the bare module-level
  `connections_support_transactions()` (aliases=None → probes ALL aliases → opens a
  cursor on an undeclared/blocked alias → `DatabaseOperationForbidden` crash on every
  plain-`TestCase` teardown for consumers with an extra non-transactional alias). Exact
  parity (flush where Django flushes, rely on rollback where Django rolls back) +
  finding 5's measured savings. Documented caveat: writes committed via a SECOND
  connection during a plain-`db` test (e.g. a mis-used burst drain — docs already
  require `transaction=True` for `absurd_drain_queue`) are not cleaned, same as Django's
  own tables.
- **Multi-DB guard:** wrapper skips when the Absurd alias is not in the test case's
  allowed `databases` (handle the `"__all__"` sentinel). Django's per-alias
  `DatabaseOperationForbidden` patching (`SimpleTestCase.setUpClass`) is still armed
  during `_post_teardown` (removed only in `tearDownClass`, which runs later), so
  flushing an undeclared alias would hard-error. Skipping is also parity-correct: an
  undeclared alias can't have been written. NOT probed this session — verify at
  implementation. Coverage note: Task 1's direct-invocation probes are the ONLY place
  the declared-alias flush path is exercised through the guard — Task 3's multidb suite
  is plain-`db` (non-transactional), so the `TestCase` short-circuit fires before the
  alias guard is ever reached there.
- **Unconfigured-backend guard:** wrapper returns early when NO Absurd backend is
  configured — explicit emptiness check, lazy `from django_absurd import backends` +
  `if not backends.get_absurd_backends(): return`. Do NOT lean on
  `resolve_absurd_database()` raising `ImproperlyConfigured` — it never does
  (`queues.py:40-44` returns `"default"` when the backend list is empty), so that guard
  would be dead code with an uncoverable except branch. The emptiness check is coverable
  (point `TASKS` at a non-Absurd/empty backend via the `settings` fixture) and
  mutant-verifiable. Makes the patch inert in Django projects that install django-absurd
  but haven't configured an Absurd backend.
- **No shipped fixture remains.** `absurd_db` deleted, `@pytest.mark.absurd_db(...)`
  marker deleted (with its `addinivalue_line`). Escape hatch for exotic harnesses = call
  `flush_absurd_state()` / `install_absurd_cleanup()` directly (documented).
  `absurd_drain_queue` untouched.

## Test-honesty constraint (false-positive guard)

Cleanup now layers: the patch's post-test truncate (every transactional DB test) + the
topology files' `_isolate_queues` (drop before+after). A test verifying a cleanup
MECHANISM can silently become a false positive — some OTHER cleanup does the work.
Rules:

- Every load-bearing behavior (hook fires + truncates; queue survives truncate; no-DB →
  no Absurd access; installer idempotency; databases-guard skip) MUST be MUTANT-VERIFIED
  during implementation: break the mechanism (no-op the wrapper's flush call; skip the
  patch install), confirm the test goes RED, revert. Record RED/GREEN evidence in the
  task report.
- Mechanism-test files must NOT use `_isolate_queues` (its drops would mask the flush
  behavior under test). Fresh data keeps them disjoint: mechanism files
  (`tests/core/test_pytest_plugin.py`, `tests/pg_cron/test_pytest_plugin.py`) are not in
  the topology set. Keep it that way.
- Internal mechanism tests are order-independent (direct hook invocation) — finding 8's
  reordering + `--reuse-db` cross-run leakage make cross-test pairs fragile in the
  internal suites. Cross-test pairs live only in `examples/web` (deterministic order).
- The no-DB guarantee is provable ONLY in a module without DB access: `tests/core` with
  a module-level `_enable_db` no-op override (finding 7 rules out examples/web).

## Global Constraints

- `import typing as t`; absolute imports; verb-named functions (autouse fixtures never
  called directly keep `_`+plain-name).
- Monkeypatching allowed ONLY for the `_post_teardown` patch itself (human-blessed),
  PLUS one narrow test seam that blessing explicitly extends to: the installer/runner
  tests' restore-`__wrapped__`/reinstall assignments to
  `TransactionTestCase._post_teardown` (try/finally-restored — the only way to cover the
  fresh-install branch and prove the runner installs). Tests otherwise never use
  `unittest.mock.patch`/monkeypatching.
- `django_absurd/pytest_plugin.py` module top level imports only `typing`/`pytest`;
  `django_absurd.test` may top-level-import `django.test`
  (`TransactionTestCase`/`TestCase`) but must lazy-import `flush`/`queues`/`backends`
  (those chain into models) inside the wrapper.
- `drop_schema=True` internal-only: appears only in `tests/conftest.py`
  (`_isolate_queues`) and the direct `flush_absurd_state` tests.
- Post-test flush timing only (wrapper runs AFTER the original `_post_teardown`); the
  pre-test drop exists only inside `_isolate_queues`.
- Assert COMPLETE error/message text; full patch coverage on added/changed lines and
  branches; alphabetize parametrize values and fixture params.
- Wrapper failures propagate (a real DB error during flush = loud teardown error, same
  as Django's own flush).

---

### Task 1: `install_absurd_cleanup()` + pytest wiring + fixture/marker removal

**Files:**

- Create: `django_absurd/test.py`
- Modify: `django_absurd/pytest_plugin.py`
- Modify: `tests/core/test_pytest_plugin.py`
- Create: `tests/core/test_pytest_plugin_no_db.py`
- Modify: `tests/pg_cron/test_pytest_plugin.py`
- Modify: `examples/web/tests/test_add.py` (one-line: drop the deleted `absurd_db`
  parameter — must land IN THIS TASK, or examples/web is broken until Task 4 and
  per-commit CI/bisect breaks)

**Interfaces:**

- Produces: `django_absurd.test.install_absurd_cleanup()` — idempotently wraps
  `TransactionTestCase._post_teardown`; `pytest_configure` calls it (lazy import);
  `absurd_db` fixture + marker registration deleted.
- Consumes: `django_absurd.flush.flush_absurd_state`,
  `django_absurd.backends.get_absurd_backends` +
  `django_absurd.queues.resolve_absurd_database` (lazy, inside the wrapper).

**Implementation shape (prose, not code):** `install_absurd_cleanup()` version-guards
first — `"_post_teardown" not in vars(TransactionTestCase)` → raise `RuntimeError`
naming the missing hook (loud CI failure on a Django restructure, never a silent no-op
patch). Idempotency: wrapper carries a marker attribute; if the current `_post_teardown`
already has it, return. Wrapper (`functools.wraps` the original, so `__wrapped__`
exposes it): call original first; return early when (a) instance is a
`django.test.TestCase` and `self._databases_support_transactions()` (rollback already
cleaned — findings 4-5; the SCOPED check, never the bare module-level
`connections_support_transactions()` — see the design bullet), (b) no Absurd backend is
configured — lazy `from django_absurd import backends` +
`if not backends.get_absurd_backends(): return` (never an `except ImproperlyConfigured`
around `resolve_absurd_database()`, which never raises — see the design bullet), or (c)
`resolve_absurd_database()`'s alias isn't in the case's `databases` (respect
`"__all__"`); else `flush_absurd_state()`. Helpers below the public function.
`pytest_configure` body: lazy `from django_absurd.test import install_absurd_cleanup` +
call — replaces the marker registration entirely.

- [ ] **Step 1: RED — direct-invocation mechanism test.** In
      `tests/core/test_pytest_plugin.py` (keep
      `pytestmark = django_db(transaction=True)` — the hook closes connections, unsafe
      mid-atomic), replace the two marker-driven `absurd_db` tests
      (`test_absurd_db_drops_schema_when_marked`,
      `test_absurd_db_default_truncates_only`) with order-independent direct hook
      invocations. Keep ALL `flush_absurd_state` direct-call tests. Sketch:

```python
def test_post_teardown_hook_truncates_absurd_state() -> None:
    call_command("absurd_sync_queues")
    add.enqueue(1, 2)
    task_model: t.Any = Task
    assert task_model.objects.filter(queue="default").count() == 1

    class ProbeCase(TransactionTestCase):  # in-function: never collected
        databases = {"default"}

        def runTest(self) -> None: ...

    ProbeCase()._post_teardown()

    assert task_model.objects.filter(queue="default").count() == 0
    assert Queue.objects.filter(queue_name="default").exists()  # truncate, not drop


def test_post_teardown_hook_skips_undeclared_absurd_alias() -> None:
    call_command("absurd_sync_queues")
    add.enqueue(1, 2)

    class NoDatabasesCase(TransactionTestCase):
        databases = frozenset()

        def runTest(self) -> None: ...

    NoDatabasesCase()._post_teardown()

    task_model: t.Any = Task
    assert task_model.objects.filter(queue="default").count() == 1  # guard skipped it
```

Notes for the implementer: `databases={"default"}` makes the original `_post_teardown`
run `call_command("flush")` on default mid-test — acceptable in a dedicated
transactional test (only Absurd state is asserted). The `frozenset()` probe exercises
OUR guard (Django's own per-alias failure-patching is a `setUpClass` effect, absent
under direct invocation). Add a guard-(b) test: point `TASKS` at an empty/non-Absurd
backend via the `settings` fixture, enqueue is impossible then — so seed a row FIRST
under real settings, switch `TASKS`, invoke the probe hook, assert the row survived
(flush skipped). Also add an idempotency test: capture
`TransactionTestCase._post_teardown`, call `install_absurd_cleanup()`, assert the
attribute is the SAME object (plugin already installed it at `pytest_configure`); then
cover the fresh-install branch by restoring `__wrapped__`, re-installing, asserting
re-wrapped (try/finally restore). Run:
`uv run pytest tests/core/test_pytest_plugin.py --no-cov -q` — expect FAIL
(module/function absent).

- [ ] **Step 2: RED — no-DB proof.** New module `tests/core/test_pytest_plugin_no_db.py`
      (own module: the override must not rob other tests of DB):

```python
@pytest.fixture(autouse=True)
def _enable_db() -> None:
    """Module-level override of the suite autouse fixture: NO db here."""


def test_absurd_access_blocked_without_db() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        add.enqueue(1, 2)
    assert str(excinfo.value) == (
        'Database access not allowed, use the "django_db" mark, or the "db" or '
        '"transactional_db" fixtures to enable it.'
    )
```

(Message verified verbatim this session.) This is currently GREEN even before
implementing (blocking is pytest-django's, not ours) — its RED partner is the mutant
check in Step 5 (it guards against the plugin ever unblocking or touching the DB itself,
and documents the guarantee). Note that in the task report rather than forcing a fake
RED.

- [ ] **Step 3: Implement.** Create `django_absurd/test.py` per the shape above; rewrite
      `pytest_plugin.py`: delete `absurd_db` fixture + marker registration,
      `pytest_configure` now only installs the patch (lazy import), keep
      `absurd_drain_queue` byte-for-byte, update the module docstring (import-safety
      story
  - what the plugin now does + one sentence noting `pytest_configure` now imports
    `django.test` on EVERY pytest run in any venv with django-absurd installed —
    import-safe pre-configuration, verified, but a small universal startup cost). Also
    drop the `absurd_db` parameter from
    `examples/web/tests/test_add.py::test_add_task_completes_via_absurd_drain_queue` and
    run `cd examples/web && uv run pytest -q --no-cov` green before committing.

- [ ] **Step 4: Rework the pg_cron suite file.** In
      `tests/pg_cron/test_pytest_plugin.py`: delete
      `test_absurd_db_marker_drop_schema_true_in_pg_cron_suite` + its `verify_flush_ran`
      fixture (exercised the deleted marker). Keep every direct `flush_absurd_state`
      test (they cover the internal drop path + pg_cron scoping). No new hook test here
      — the hook→flush plumbing is covered in core; pg_cron flush CONTENT is covered by
      the direct-call tests (don't re-assert the same rule per entrypoint).

- [ ] **Step 5: GREEN + MUTANT-VERIFY.** Run `uv run pytest tests/core tests/pg_cron`
      (both green; blanket reset still present until Task 3 — fine, it runs pre-test,
      the hook runs post-test, no masking of Step 1's in-test assertions). Then mutants:
      (a) no-op the wrapper's `flush_absurd_state()` call → Step 1 truncate test RED;
      (b) skip the `install_absurd_cleanup()` call in `pytest_configure` → Step 1
      truncate test RED (hook not wrapped) AND Step 2 still GREEN (proves blocking never
      depended on us); (c) invert the databases-guard → skip test RED. Revert each;
      record RED/GREEN.

- [ ] **Step 6: mypy + ruff + coverage.** New/changed lines fully covered, with TWO
      honest carve-outs to REPORT (never pragma unilaterally): (1) `pytest_configure`
      runs BEFORE pytest-cov activates, so its body may show uncovered — the
      idempotency + reinstall tests cover `install_absurd_cleanup` itself; (2) the
      version-guard `RuntimeError` branch
      (`"_post_teardown" not in vars(TransactionTestCase)`) is uncoverable without
      deleting the attribute in test code (forbidden test monkeypatching) — report the
      miss with this rationale.

- [ ] **Step 7: Commit** —
      `feat: auto Absurd cleanup via TransactionTestCase._post_teardown patch; drop absurd_db fixture+marker`.

---

### Task 2: DEFERRED — `manage.py test` surface (no shipped runner in this feature)

**Decision (human, 2026-07-22):** defer the `manage.py test` (non-pytest
`DiscoverRunner`) surface entirely to a follow-up
([issue #96](https://github.com/lincolnloop/django-absurd/issues/96)). This feature
ships pytest coverage only (Task 1's `pytest_configure` wiring). Rationale: the project
is pytest-native and `manage.py test`-only consumers are a minority; a shipped
`AbsurdTestRunner` would add a required `TEST_RUNNER` setting AND conflict with a
consumer's own custom runner. `install_absurd_cleanup()` is already public (Task 1), so
a determined `manage.py test` user can call it from their own `DiscoverRunner` subclass
/ `setup_test_environment` today — the docs (Task 5) note this + link #96. No
`AbsurdTestRunner` class, no `tests/core/test_test_runner.py`.

---

### Task 3: Internal-suite refactor — drop blanket reset, local `_isolate_queues`

**Files:**

- Modify: `tests/conftest.py` (REMOVE autouse `_reset_absurd_queues`; ADD non-autouse
  `_isolate_queues` — `flush_absurd_state(drop_schema=True)` before AND after `yield`,
  depending on `_enable_db`)
- Modify (finding 9's exact fresh set — RE-DERIVE, don't trust blindly):
  `tests/core/test_cleanup.py`, `tests/core/test_enqueue.py`,
  `tests/core/test_results.py`, `tests/core/test_scheduler.py`,
  `tests/core/test_worker.py`, `tests/multidb/test_router.py` — module-level
  `pytestmark = [<existing django_db mark>, pytest.mark.usefixtures("_isolate_queues")]`

**Background:** finding 9. Whole remedy validated this session: exactly this set → all
three suites green, tests/core green across 3 repeat runs. `tests/pg_cron` needs NO
isolation. Re-derivation caveats: run each suite a few times, including at least one
shuffled run — pytest-randomly is NOT a repo dependency, pull it in ad hoc:
`uv run --with pytest-randomly pytest tests/core -q --no-cov`; `--reuse-db` carries
leaked state INTO the first run — a failure on run 1 that vanishes on run 2 is still
topology pollution, keep the file in the set; use `--create-db` for a clean baseline
where needed, pg_cron's documented ALLOW_CONNECTIONS dance applies there. Naming note:
`_isolate_queues` is non-autouse yet `_`-prefixed — outside the LETTER of CLAUDE.md's
autouse-only naming exception, though within its spirit (never called directly, applied
only via `usefixtures`); keep the name, flag it in the task report for the human to
bless or rename.

- [ ] **Step 1:** conftest change (fixture in, blanket reset out; add
      `import typing as t` for the iterator annotation).
- [ ] **Step 2:** `uv run pytest tests/core -q` → confirm/derive failing files; apply
      `_isolate_queues`; repeat until green; then 2+ repeat runs (one shuffled via
      `uv run --with pytest-randomly pytest tests/core -q --no-cov`) green.
- [ ] **Step 3:** same for `tests/pg_cron` (expect zero files) and `tests/multidb`
      (expect `test_router.py` only).
- [ ] **Step 4:** MUTANT-VERIFY `_isolate_queues` is load-bearing: comment out its
      pre-`yield` drop → `test_enqueue_with_empty_queues_reports_undeclared` (or the
      re-derived equivalent) goes RED on a dirtied DB. Revert.
- [ ] **Step 5:** mypy + ruff; **Commit** —
      `test: local _isolate_queues for topology-varying files; drop blanket reset`.

---

### Task 4: `examples/web` integration guard (entry-point position)

**Files:**

- Create: `examples/web/tests/test_absurd_cleanup.py`

(`test_add.py`'s `absurd_db`-param removal already landed in Task 1 — sequencing: the
fixture deletion and its last consumer must change in the same commit.)

**Background:** the ONLY place the shipped mechanism is exercised from the real
`pytest11` entry-point position with zero local conftest help — the CI regression guard
for findings 3/6. Deterministic order (no pytest-randomly) makes cross-test pairs honest
here. Do NOT add a no-DB test here (finding 7 — blocker never armed; it would be a false
guarantee). Pair must both be `transaction=True` (finding 8's reordering keeps relative
order only within the same transactionality class). Sketch:

```python
@pytest.mark.django_db(transaction=True)
def test_1_enqueue_commits_task_row() -> None:
    add.enqueue("2", "3")
    task_model: t.Any = Task
    assert task_model.objects.filter(queue="default").count() == 1


@pytest.mark.django_db(transaction=True)
def test_2_previous_tests_absurd_state_was_flushed() -> None:
    task_model: t.Any = Task
    assert task_model.objects.filter(queue="default").count() == 0
    assert Queue.objects.filter(queue_name="default").exists()  # truncate, not drop


def test_plain_db_enqueue_rides_the_test_transaction(db: None) -> None:
    result = add.enqueue("2", "3")
    assert result.id  # rolled back by Django; hook's skip branch — nothing to flush
```

(Exact pair verified passing this session at entry-point position.)

- [ ] **Step 1:** write tests; `cd examples/web && uv run pytest -v --no-cov` green.
- [ ] **Step 2:** MUTANT-VERIFY: no-op the wrapper's flush → `test_2_...` RED. Revert.
- [ ] **Step 3:** ruff; **Commit** —
      `test(examples/web): entry-point regression guard for auto Absurd cleanup`.

---

### Task 5: Docs rewrite

**Files:**

- Modify: `django_absurd/AGENTS.md` (`## Testing` section)
- Modify: `docs/web/testing.md`
- Check: `grep -ri absurd_db docs/ django_absurd/AGENTS.md README.md` — purge every
  consumer-facing mention of the fixture, the marker, and `drop_schema`.

**Content model:** (1) LEAD with "cleanup is automatic" — pytest users do nothing:
installing django-absurd wires post-test Absurd truncation into Django's own test
teardown, exact `db`/`transactional_db` parity; plain `TestCase`/`db` tests are cleaned
by Django's rollback (enqueue rides the transaction), transactional tests by the
truncate. (2) `manage.py test` (non-pytest) is not auto-wired in this release (follow-up
[#96](https://github.com/lincolnloop/django-absurd/issues/96)); such users call
`django_absurd.test.install_absurd_cleanup()` from their own `DiscoverRunner` subclass /
`setup_test_environment`. (3) No DB access → no Absurd access (pytest-django's blocker /
Django's per-alias guard — link both). (4) `absurd_drain_queue` unchanged (keep the
`transaction=True` requirement note — it doubles as the second-connection caveat).
Multi-DB caveat to document alongside it: a `transaction=True` test that drains via the
worker's own raw connection while NOT declaring the Absurd alias in `databases` commits
state the alias guard then skips — a silent leak; declare the Absurd alias on any test
that runs a worker. (5) Keep the `SYNC_SCHEDULES_ON_TEST_DB` interaction note.
Cross-link Django test docs (`_post_teardown` is internal — link the flush-parity
concept via Django's "Order in which tests are executed" + TransactionTestCase docs),
pytest-django helpers, sibling pages. Follow sync-docs conventions; `uvx zensical build`
clean.

- [ ] **Step 1:** AGENTS.md. **Step 2:** docs/web/testing.md. **Step 3:** grep purge +
      `uvx zensical build`. **Step 4: Commit**.

---

### Task 6: ruff per-file-ignore consolidation + pytest-django dependency

**Files:**

- Modify: `pyproject.toml`

Unchanged carry-over from the prior plan. (a) `"examples/web/tests/**/*"` and
`"tests/**/*"` extend-per-file-ignores entries hold identical lists — consolidate to one
`"**/tests/**/*"` key; VERIFY with `uv run ruff check .` that both trees keep the
ignores. (b) Expose pytest-django for consumers (the shipped pytest integration expects
it): add `[project.optional-dependencies] test = ["pytest-django"]` (dev group already
pins it) — mirror the project's existing dependency style; flag in the report if a
different placement is cleaner.

- [ ] **Step 1:** consolidate + verify. **Step 2:** add the extra. **Step 3: Commit**.

---

## Self-Review Notes

**Supersedes:** the autouse-fixture/detection design (this file's previous revision) and
the prior branch's `absurd_db` fixture + `@pytest.mark.absurd_db(drop_schema=...)`
marker — all unmerged, safe to reshape. `flush_absurd_state(*, drop_schema=False)` /
`clear_queues` unchanged; `drop_schema=True` stays internal-only.

**Verified this session vs. verify-at-implementation:**

- VERIFIED: findings 1–9 above (patch target, unblock context, import safety, both-case
  firing, rollback-before-wrapper, flush cost, entry-point integration, examples/web
  blocker-unarmed, reordering, full three-suite experiment incl. repeat runs).
- VERIFY AT IMPLEMENTATION: (a) the databases-guard branches (Task 1's probes — designed
  but not probed; the multidb suite does NOT reach the alias guard, see the Multi-DB
  design bullet); (b) `"__all__"` sentinel handling; (c)
  `self._databases_support_transactions()` call-site behavior (the scoped check Django
  uses at `testcases.py:1490` — probe session used the bare module function, since
  replaced per review); (d) the unconfigured-backend guard's `get_absurd_backends()`
  emptiness check via the `settings` fixture; (e) coverage artifacts on
  `pytest_configure`'s body and the version-guard branch; (f) re-derived topology set
  matches finding 9.

**Open items for adversarial plan review:**

1. Wrapper skip-branch (plain `TestCase`) is perf-only and observationally equivalent
   except timing/second-connection leaks — it is branch-COVERED but not
   mutant-verifiable behaviorally; is a timing assertion worth it (probably not)?
2. Per-test 28 ms flush on transactional tests only — no fast path needed now; revisit
   if a consumer suite reports drag.
3. pytest-django pin as `test` extra vs README-only guidance.
4. `django_absurd/test.py` will hold runner + installer; if a future `TestCase` helper
   family grows, module stays the Django-parity home (`django.test` naming mirror).
