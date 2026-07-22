# SYNC_SCHEDULES_ON_MIGRATE / SYNC_SCHEDULES_ON_TEST_DB Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** stop `migrate` from silently syncing a project's declared pg_cron schedules
into a real, live `cron.job` catalog whenever it runs against a **test** database.
Closes the hazard confirmed in
`docs/specs/2026-07-21-sync-schedules-on-migrate-design.md`.

**Architecture:** two new per-backend `OPTIONS` keys (`SYNC_SCHEDULES_ON_MIGRATE`
default `True`, `SYNC_SCHEDULES_ON_TEST_DB` default `False`), checked inside
`reconcile_crons_after_migrate` (the `post_migrate` signal receiver only — never the
shared `sync_crons`/`sync_admin_crons` functions, never the explicit `absurd_sync_crons`
command). Test-DB detection compares a snapshot of each database alias's `NAME`,
captured once in `PgCronConfig.ready()` (always before any test-DB swap), against the
live value at signal-fire time.

**Tech Stack:** Django 6.0 / Python 3.12+, pg_cron, pytest (function-based), psycopg3.

## Global Constraints

- `import typing as t` (never `from typing import X`); absolute imports only.
- Functions verb-named; no leading-underscore module constants/helpers.
- No monkeypatching; behavioral tests via real entrypoints (`migrate`,
  `absurd_sync_crons`, direct ORM writes) — never a unit-level call straight into
  `reconcile_crons_after_migrate`.
- No ruff ignores without asking first.
- Full patch coverage on every added line/branch.
- Assert complete error-message text (not fragments) where applicable.
- Alphabetize `@pytest.mark.parametrize` values, fixture `params`, and a test function's
  own fixture parameters (e.g. `def test_x(admin_user, client, settings)`).
- The guard must live in `reconcile_crons_after_migrate` specifically — not in
  `sync_crons`/`sync_admin_crons` (would also silently gate the explicit
  `absurd_sync_crons` command) and not at `PgCronConfig.ready()`'s `.connect()` call
  (can't know test-vs-real yet at that point — verified, see spec).
- `ORIGINAL_DATABASE_NAMES` must store plain string values captured out of
  `settings.DATABASES` inside `ready()` — never a dict reference. Verified empirically:
  `connections[alias].settings_dict` **is** `settings.DATABASES[alias]` (the same
  object, not a copy), so a reference-based approach can never detect a test-DB swap.
- This project's own `tests/pg_cron/test_pg_cron_post_migrate.py` suite must stay green
  — its `run_cron_sync` fixture parametrizes over `["absurd_sync_crons", "migrate"]` and
  asserts identical outcomes for both, which this feature would otherwise break (every
  test in that suite runs on a test DB).
- Docs mirrored between `django_absurd/AGENTS.md` and `docs/web/cron-jobs.md`, build
  clean (`uvx zensical build`).

---

### Task 1: Implement the guard, keep the existing suite green, add dedicated tests

**Files:**

- Modify: `django_absurd/pg_cron/apps.py`
- Modify: `tests/pg_cron/utils.py`
- Create: `tests/pg_cron/fixtures_tasks.py`
- Create: `tests/pg_cron/test_sync_schedules_on_migrate.py`

**Interfaces:**

- Produces: `django_absurd.pg_cron.apps.ORIGINAL_DATABASE_NAMES: dict[str, str]`
  (module-level, populated in `PgCronConfig.ready()`); the two new per-backend `OPTIONS`
  keys read via `backend.options.get(key, default)`.
- Consumes: `django_absurd.backends.get_absurd_backends()` (already imported/used in
  this file); `django_absurd.backends.AbsurdBackend` (**not** currently imported at all
  — added under `TYPE_CHECKING` only, referenced via a quoted annotation, see Step 4);
  `django.db.connections`, `django.conf.settings` (new imports, safe at module level —
  this file already imports `django.db.utils`/`django.db.models.signals` at module
  level, and these two are only ever _called_ later, at signal-fire time, long after
  `django.setup()` completes).

**IMPORTANT — this task's real-DB subprocess test design was independently verified
end-to-end before this plan was finalized** (not just sketched): a throwaway script
confirmed the exact sequence below — `migrate django_absurd_pg_cron zero` (rolls back
just this app's own migrations; core `django_absurd`'s schema is untouched), a minimal
subprocess `migrate` targeting `absurd_test_pg_cron` directly (bypassing pytest-django's
test-DB machinery entirely, so no swap ever happens in that process — confirming
`is_test_db=False` there), then `migrate django_absurd_pg_cron zero` + a full `migrate`
again to restore. Two real bugs were found and fixed during that verification, both
already reflected in the code below — do not "simplify" them back out:

1. `OPTIONS["QUEUES"]` must be a **dict** (`{"default": {}}`), never a list — the
   list-shorthand form is only valid as a **top-level** `TASKS["default"]["QUEUES"]`
   key, not nested under `OPTIONS`. `get_declared_queues` does
   `dict(backend.options["QUEUES"])`, which raises `ValueError` on a list.
2. The `reconcile_crons_after_migrate` `except` block logging "skipped cron reconcile"
   during the `migrate ... zero` step is **expected, benign noise** — `post_migrate`
   fires even on a reverse migration, racing against the just-dropped
   `django_absurd_scheduledtask` table; the function's own documented best-effort catch
   handles it. Do not treat this log line as a test failure.

Also confirmed: `tests/tasks.py`'s `add` task cannot be used as the subprocess's
`SCHEDULE` target — its module imports `django.contrib.auth.models.Group` and
`tests.models.Payload` at the top level, which the subprocess's minimal `INSTALLED_APPS`
doesn't support. Hence the new, dependency-free `tests/pg_cron/fixtures_tasks.py`.

- [ ] **Step 1: Create the minimal task fixture module**

```python
# tests/pg_cron/fixtures_tasks.py
"""Minimal, dependency-free task used only by test_sync_schedules_on_migrate.py's
subprocess-based real-DB test — must not import anything beyond django.tasks (no
django.contrib.auth, no tests.models) so a bare, minimally-configured subprocess can
resolve its dotted path."""

from django.tasks import task


@task
def add(a: int, b: int) -> int:
    return a + b
```

- [ ] **Step 2: Write the failing tests in
      `tests/pg_cron/test_sync_schedules_on_migrate.py`**

```python
"""Tests for SYNC_SCHEDULES_ON_MIGRATE / SYNC_SCHEDULES_ON_TEST_DB
(django_absurd/pg_cron/apps.py). Not in test_pg_cron_post_migrate.py: that file's
run_cron_sync fixture exists specifically to prove absurd_sync_crons/migrate are
IDENTICAL — the opposite of what these tests show."""

import os
import subprocess
import sys
import typing as t
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import connections

from django_absurd.pg_cron.models import ScheduledTask
from tests.pg_cron.utils import build_pg_cron_tasks

if t.TYPE_CHECKING:
    import pytest_django.fixtures

pytestmark = pytest.mark.django_db(transaction=True)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Minimal, self-contained settings for the subprocess — least config required for
# django_absurd + django_absurd.pg_cron to migrate and reconcile a SCHEDULE. Neither
# app's migrations reference contenttypes/auth (verified), so neither is needed here.
SUBPROCESS_SCRIPT = """
import django
from django.conf import settings

settings.configure(
    DATABASES={{
        "default": {{
            "ENGINE": "django.db.backends.postgresql",
            "NAME": {dbname!r},
            "USER": {user!r},
            "PASSWORD": {password!r},
            "HOST": {host!r},
            "PORT": {port!r},
        }}
    }},
    INSTALLED_APPS=["django_absurd", "django_absurd.pg_cron"],
    TASKS={{
        "default": {{
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {{
                "QUEUES": {{"default": {{}}}},
                "SCHEDULE": {{
                    "nightly": {{
                        "task": "tests.pg_cron.fixtures_tasks.add",
                        "cron": "0 2 * * *",
                    }},
                }},
                "SYNC_SCHEDULES_ON_MIGRATE": {sync_on_migrate!r},
            }},
        }}
    }},
    DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
    USE_TZ=True,
)
django.setup()
from django.core.management import call_command

call_command("migrate", verbosity=0)
"""


def migrate_real_db_in_subprocess(*, sync_on_migrate: bool) -> None:
    params = connections["default"].get_connection_params()
    script = SUBPROCESS_SCRIPT.format(
        dbname=params["dbname"],
        user=params.get("user", ""),
        password=params.get("password", ""),
        host=params.get("host", "localhost"),
        port=params.get("port", ""),
        sync_on_migrate=sync_on_migrate,
    )
    subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        cwd=REPO_ROOT,
        check=True,
    )


@pytest.fixture
def real_db_migration_cycle() -> "t.Iterator[None]":
    """Roll back JUST django_absurd_pg_cron's own migrations (not the whole
    database — that app owns CREATE EXTENSION pg_cron, so this is a genuine
    from-scratch migrate for the exact feature under test) so the subprocess's
    migrate is a real first-time provisioning run. Restores the schema afterward
    so the rest of this --reuse-db session's tests aren't affected."""
    call_command("migrate", "django_absurd_pg_cron", "zero", verbosity=0)
    try:
        yield
    finally:
        call_command("migrate", "django_absurd_pg_cron", "zero", verbosity=0)
        call_command("migrate", verbosity=0)


def test_migrate_syncs_by_default_on_a_real_non_test_database(
    real_db_migration_cycle: None,
) -> None:
    # Regression check, not a true RED->GREEN test: this passes even before the
    # guard exists (today's unchanged behavior). Kept to prove the guard doesn't
    # accidentally also break the real-DB default.
    migrate_real_db_in_subprocess(sync_on_migrate=True)
    scheduled_task = ScheduledTask.objects.get(name="nightly", source="s")
    assert scheduled_task.get_pg_cron_job() is not None


def test_migrate_skips_sync_on_a_real_database_when_explicitly_disabled(
    real_db_migration_cycle: None,
) -> None:
    # Genuine RED before the guard exists: SYNC_SCHEDULES_ON_MIGRATE doesn't exist
    # yet, so it's silently ignored and sync happens anyway — this assertion fails.
    # GREEN once the guard respects it.
    migrate_real_db_in_subprocess(sync_on_migrate=False)
    assert not ScheduledTask.objects.filter(name="nightly", source="s").exists()


def test_migrate_skips_sync_by_default_on_a_test_database(
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    settings.TASKS = build_pg_cron_tasks(
        {"nightly": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    settings.TASKS["default"]["OPTIONS"]["SYNC_SCHEDULES_ON_TEST_DB"] = False

    call_command("migrate", verbosity=0)

    assert ScheduledTask.pg_cron.get_managed_jobs() == []
    assert ScheduledTask.objects.filter(source="s").count() == 0


def test_migrate_syncs_on_a_test_database_when_explicitly_enabled(
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    settings.TASKS = build_pg_cron_tasks(
        {"nightly": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    settings.TASKS["default"]["OPTIONS"]["SYNC_SCHEDULES_ON_TEST_DB"] = True

    call_command("migrate", verbosity=0)

    assert [r[0] for r in ScheduledTask.pg_cron.get_managed_jobs()] == ["_dj:s:nightly"]


def test_scheduled_task_create_and_delete_are_unaffected_by_either_setting(
    settings: "pytest_django.fixtures.SettingsWrapper",
) -> None:
    settings.TASKS = build_pg_cron_tasks({})
    settings.TASKS["default"]["OPTIONS"]["SYNC_SCHEDULES_ON_TEST_DB"] = False

    scheduled_task = ScheduledTask.objects.create(
        source="a",
        name="direct_create",
        task="tests.tasks.add",
        cron="0 2 * * *",
    )
    assert scheduled_task.get_pg_cron_job() is not None

    scheduled_task.delete()
    assert ScheduledTask.pg_cron.get_job("direct_create", "a") is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/pg_cron/test_sync_schedules_on_migrate.py -v --no-cov`
Expected: **2 of the 5 tests genuinely FAIL** (real RED, not a placeholder claim):
`test_migrate_skips_sync_on_a_real_database_when_explicitly_disabled` (the
`SYNC_SCHEDULES_ON_MIGRATE` key doesn't exist yet, so it's silently ignored and sync
happens anyway) and `test_migrate_skips_sync_by_default_on_a_test_database` (the guard
doesn't exist yet, so `migrate` syncs unconditionally). The other 3 tests PASS already —
they exercise behavior that's either already correct today (real-DB default sync,
per-row create/delete) or an explicit-`True` override that happens to match today's
unconditional-sync behavior anyway.

- [ ] **Step 4: Implement the guard in `django_absurd/pg_cron/apps.py`**

Add two imports to the top of the file:

```python
from django.conf import settings
from django.db import connections
```

Add the module-level snapshot dict, right after the existing top-level constants (before
the `PgCronConfig` class):

```python
ORIGINAL_DATABASE_NAMES: dict[str, str] = {}
```

In `PgCronConfig.ready()`, add the snapshot population as the very first line of the
method body (before the existing `# Side-effect import` comment):

```python
    def ready(self) -> None:
        for db_alias, db_config in settings.DATABASES.items():
            ORIGINAL_DATABASE_NAMES[db_alias] = db_config["NAME"]

        # Side-effect import: running the module registers its @register'd E007 checks.
        import django_absurd.pg_cron.checks  # noqa: F401, PLC0415
        ...
```

In `reconcile_crons_after_migrate`, add the check right after
`alias, backend = next(iter(absurd_backends.items()))` and before the existing `try:`
block:

```python
    alias, backend = next(iter(absurd_backends.items()))
    if not resolve_sync_schedules_option(backend):
        return
    try:
```

Add the new helper function below `reconcile_crons_after_migrate` (matching this
project's "helpers live below the public function that uses them" convention). **The
annotation must be a quoted string** — this file has no
`from __future__ import annotations`, and `AbsurdBackend` is only imported under
`TYPE_CHECKING`; an unquoted annotation here would raise `NameError` at import time and
brick the whole pg_cron app (mirrors the existing precedent in
`django_absurd/backends.py::get_declared_queues(backend: "AbsurdBackend")`):

```python
def resolve_sync_schedules_option(backend: "AbsurdBackend") -> bool:
    is_test_db = (
        connections[backend.database].settings_dict["NAME"]
        != ORIGINAL_DATABASE_NAMES.get(backend.database)
    )
    if is_test_db:
        return bool(backend.options.get("SYNC_SCHEDULES_ON_TEST_DB", False))
    return bool(backend.options.get("SYNC_SCHEDULES_ON_MIGRATE", True))
```

Add a `TYPE_CHECKING` import block for `AbsurdBackend` (this file doesn't have one yet —
`AbsurdBackend` is currently only referenced via `get_absurd_backends`'s return type,
which is a plain runtime call, not an annotation):

```python
if t.TYPE_CHECKING:
    from django_absurd.backends import AbsurdBackend
```

- [ ] **Step 5: Run the new tests, verify they pass**

Run: `uv run pytest tests/pg_cron/test_sync_schedules_on_migrate.py -v --no-cov`
Expected: all 5 tests PASS.

- [ ] **Step 6: Run the full existing pg_cron suite — confirm it's now broken**

Run: `uv run pytest tests/pg_cron -v --no-cov 2>&1 | tail -40` Expected: multiple
failures in `test_pg_cron_post_migrate.py` — the `run_cron_sync` fixture's `"migrate"`
parametrization now silently skips syncing (since every test in this suite runs on a
test DB, and `SYNC_SCHEDULES_ON_TEST_DB` defaults `False`), while the
`"absurd_sync_crons"` parametrization still works — the two branches diverge where tests
assert they're identical. Not limited to `run_cron_sync`-based tests either — several
tests in that file call `reconcile_crons_after_migrate`-driving entrypoints directly
(e.g. `test_reconcile_emits_migrate_stdout_on_sync`,
`test_migrate_provisions_queues_and_reconciles_crons`, the prune tests) and break the
same way. This is expected and matches the Global Constraints — proceed to Step 7 to fix
it.

- [ ] **Step 7: Fix `tests/pg_cron/utils.py::build_pg_cron_tasks` to restore this
      project's own suite behavior**

Read the current file — it's a 12-line module with one function. Replace it:

```python
"""Shared helpers for the pg_cron test suite (plain functions — fixtures live in
conftest.py; pg_cron catalog queries live on ``ScheduledTask.pg_cron``)."""

import typing as t

from tests.utils import make_tasks_settings


def build_pg_cron_tasks(
    schedule: dict[str, dict[str, object]],
) -> dict[str, dict[str, t.Any]]:
    settings = make_tasks_settings(schedule=schedule)
    # This project's own pg_cron suite is specifically testing reconcile-on-migrate
    # behavior — restore today's always-sync default here (every test in this suite
    # runs on a test DB, where the new library default is now False) rather than
    # forcing every call site to override it individually. A test that wants to
    # exercise the OFF-by-default path sets this back to False explicitly (see
    # test_sync_schedules_on_migrate.py).
    settings["default"]["OPTIONS"]["SYNC_SCHEDULES_ON_TEST_DB"] = True
    return settings
```

- [ ] **Step 8: Run the full pg_cron suite again, confirm it's green**

Run: `uv run pytest tests/pg_cron -v --no-cov` Expected: all tests PASS, including every
`test_pg_cron_post_migrate.py` test using `run_cron_sync`.

- [ ] **Step 9: Run the new dedicated test file once more (confirms Step 7 didn't
      regress it — its tests explicitly override `SYNC_SCHEDULES_ON_TEST_DB` back down
      per-test, so `build_pg_cron_tasks`'s new default shouldn't affect them, but verify
      directly rather than assume)**

Run: `uv run pytest tests/pg_cron/test_sync_schedules_on_migrate.py -v --no-cov`
Expected: all 5 tests still PASS.

- [ ] **Step 10: Enable coverage instrumentation for the subprocess**

The real-DB tests spawn a `subprocess.run(...)`; without this, its execution of
`reconcile_crons_after_migrate`/`resolve_sync_schedules_option` is invisible to
coverage, permanently "missing" the `SYNC_SCHEDULES_ON_MIGRATE` branch and violating the
full-patch-coverage rule. Verified empirically (a standalone throwaway
subprocess+coverage experiment, not just config-reading) that coverage.py's built-in
subprocess patching closes this with a one-line config addition — no
`COVERAGE_PROCESS_START`/`sitecustomize.py` needed. Add `patch = ["subprocess"]` to the
root `pyproject.toml`'s `[tool.coverage.run]` section:

```toml
[tool.coverage.run]
branch = true
omit = [
  "tests/multidb/*",
]
patch = ["subprocess"]
plugins = [
  "django_coverage_plugin",
]
source = ["django_absurd", "tests"]
```

- [ ] **Step 11: Full suite + mypy + ruff clean**

Run: `uv run pytest tests/pg_cron --create-db -v` (full run with coverage; use the
`ALTER DATABASE ... WITH ALLOW_CONNECTIONS false` + terminate-backend dance from
`CLAUDE.md` first if pg_cron's launcher blocks the `--create-db` drop) Run:
`uv run mypy django_absurd/pg_cron/apps.py tests/pg_cron/utils.py tests/pg_cron/fixtures_tasks.py tests/pg_cron/test_sync_schedules_on_migrate.py`
Run:
`uv run ruff check django_absurd/pg_cron/apps.py tests/pg_cron/utils.py tests/pg_cron/fixtures_tasks.py tests/pg_cron/test_sync_schedules_on_migrate.py`
Expected: full suite passes, no missed lines/branches in `apps.py`'s new code (including
the `SYNC_SCHEDULES_ON_MIGRATE` branch, now instrumented via Step 10); mypy and ruff
clean.

- [ ] **Step 12: Commit**

```bash
git add pyproject.toml django_absurd/pg_cron/apps.py tests/pg_cron/utils.py tests/pg_cron/fixtures_tasks.py tests/pg_cron/test_sync_schedules_on_migrate.py
git commit -m "feat: SYNC_SCHEDULES_ON_MIGRATE / SYNC_SCHEDULES_ON_TEST_DB"
```

---

### Task 2: Docs — AGENTS.md + docs/web/cron-jobs.md

**Files:**

- Modify: `django_absurd/AGENTS.md`
- Modify: `docs/web/cron-jobs.md`

**Interfaces:**

- Consumes (from Task 1): `OPTIONS["SYNC_SCHEDULES_ON_MIGRATE"]`,
  `OPTIONS["SYNC_SCHEDULES_ON_TEST_DB"]`.
- No test step — docs-only; verification is the `zensical build` in Step 3.

- [ ] **Step 1: Add a bold-lead paragraph to `django_absurd/AGENTS.md`'s
      `### pg_cron     backend` section**

Insert right after the existing paragraph ending "`absurd_sync_crons` is the backstop
for pipelines that skip `migrate`." and before the `--teardown` paragraph — matching
this section's existing bold-lead-paragraph style (`**Wrapper model:**`,
`**Non-default-backend schedules.**`, `**Admin.**`):

```markdown
**Test databases.** An automatic, migrate-time sync of your real `SCHEDULE` is a hazard
on a **test** database — pg_cron's launcher runs independently of pytest/Django, so a
synced schedule fires for real, on schedule, against test data, for the rest of the
session. Two `OPTIONS` keys govern this: `SYNC_SCHEDULES_ON_MIGRATE` (default `True`,
governs a real database — unchanged from today) and `SYNC_SCHEDULES_ON_TEST_DB` (default
`False`, governs a database Django's test framework has swapped in). The safe default
requires no settings changes — test databases are detected automatically. Set
`OPTIONS["SYNC_SCHEDULES_ON_TEST_DB"] = True` if a test genuinely needs `migrate` to
reconcile schedules for real (this project's own pg_cron test suite does exactly this,
via `tests/pg_cron/utils.py::build_pg_cron_tasks`). `absurd_sync_crons` is never gated
by either key — it's a deliberate, explicit invocation, not an automatic side effect.
```

- [ ] **Step 2: Add the mirrored subsection to `docs/web/cron-jobs.md`**

Insert a new `### Test databases` subsection after `### Reconcile explicitly` and before
`### Authoring schedules in the admin` (this file uses `###` subheadings for this kind
of content, not bold-lead paragraphs — mirror the AGENTS.md content adapted to that
convention):

```markdown
### Test databases

An automatic, migrate-time sync of your real `SCHEDULE` is a hazard on a **test**
database — pg_cron's launcher runs independently of pytest/Django, so a synced schedule
fires for real, on schedule, against test data, for the rest of the session.

Two `OPTIONS` keys govern this:

- **`SYNC_SCHEDULES_ON_MIGRATE`** (default `True`) — governs `migrate` against a real
  database. Unchanged from today's behavior.
- **`SYNC_SCHEDULES_ON_TEST_DB`** (default `False`) — governs `migrate` when Django's
  test framework has swapped in a test database. Safe by default, detected automatically
  — no settings changes needed.

If a test genuinely needs `migrate` to reconcile schedules for real, set
`OPTIONS["SYNC_SCHEDULES_ON_TEST_DB"] = True` explicitly.

`absurd_sync_crons` is never gated by either key — it's a deliberate, explicit
invocation, not an automatic side effect of `migrate`.
```

- [ ] **Step 3: Build docs clean**

Run: `uvx zensical build` Expected: build succeeds, no broken links/anchors.

- [ ] **Step 4: Commit**

```bash
git add django_absurd/AGENTS.md docs/web/cron-jobs.md
git commit -m "docs: SYNC_SCHEDULES_ON_MIGRATE / SYNC_SCHEDULES_ON_TEST_DB"
```

---

## Self-Review Notes

This plan went through one full adversarial review round (Opus) that found two real
blockers and one guaranteed crash bug, all now fixed in the content above — not just
noted:

1. **Blocker — the original subprocess test design was infeasible.**
   `CREATE EXTENSION pg_cron` is only permitted on `absurd_test_pg_cron` specifically
   (this project's `compose.yaml` comment confirms it — `cron.database_name` is a
   cluster-wide, postmaster-context setting), so a brand-new, separate database could
   never host pg_cron at all. Fixed: the redesigned test targets `absurd_test_pg_cron`
   itself, in a genuinely separate _process_ (not a separate database) — the
   subprocess's own `ready()` never sees a test-DB swap, so `is_test_db` correctly
   evaluates `False` there regardless of the physical database being shared with the
   outer pytest session.
2. **Blocker — the subprocess couldn't import its own schedule's task.**
   `tests.tasks.add` pulls in `django.contrib.auth`/`tests.models` at module level,
   which the minimal subprocess `INSTALLED_APPS` doesn't support; the failure was
   silently swallowed by `reconcile_crons_after_migrate`'s own broad `except`, making
   the test falsely pass/fail without testing anything. Fixed: a new, dependency-free
   `tests/pg_cron/fixtures_tasks.py`.
3. **Major — guaranteed `NameError` on import.** The originally-planned
   `resolve_sync_schedules_option(backend: AbsurdBackend) -> bool` would crash `apps.py`
   at import time (no `from __future__ import annotations`, `AbsurdBackend` only under
   `TYPE_CHECKING`). Fixed: quoted annotation (`backend: "AbsurdBackend"`), matching
   `backends.py`'s own established precedent.
4. **Major — the subprocess wasn't coverage-instrumented.** Verified empirically (a
   standalone throwaway experiment, not just config-reading) that coverage.py's built-in
   `patch = ["subprocess"]` closes this cleanly. Added as Task 1 Step 10.

**The entire migrate-zero → subprocess-migrate → migrate-zero → restore cycle was run
for real** against this project's actual `db_pg_cron` container before this plan was
finalized (not just designed on paper) — confirming the rollback, the corrected
subprocess settings (including a `QUEUES` dict-vs-list bug caught only by running it),
the real `ScheduledTask` row + `cron.job` creation, and a clean restore (`tests/pg_cron`
suite ran 218/218 green afterward).

- **Spec coverage:** both `OPTIONS` keys + their defaults, the `ORIGINAL_DATABASE_NAMES`
  snapshot mechanism (with the same-object bug fix baked in from the start — this plan
  never contains the broken version), the guard's placement (receiver only, verified
  against `absurd_sync_crons`'s separate call site), keeping the existing suite green
  (`build_pg_cron_tasks` default), the two-pronged, real (not simulated) test strategy,
  docs in both locations, subprocess coverage instrumentation. All present.
- **Placeholder scan:** none — every step has real, complete, independently-verified
  code and exact commands.
- **Type consistency:**
  `resolve_sync_schedules_option(backend: "AbsurdBackend") -> bool` matches how Task 1
  Step 4 calls it (`resolve_sync_schedules_option(backend)` inside
  `reconcile_crons_after_migrate`, where `backend` is already typed via
  `next(iter(absurd_backends.items()))`, itself typed by
  `get_absurd_backends() -> dict[str, AbsurdBackend]`).
  `ORIGINAL_DATABASE_NAMES: dict[str, str]` matches both its population site (`ready()`,
  string values from `settings.DATABASES[alias]["NAME"]`) and its read site
  (`resolve_sync_schedules_option`, `.get(backend.database)` against
  `backend.database: str`).
