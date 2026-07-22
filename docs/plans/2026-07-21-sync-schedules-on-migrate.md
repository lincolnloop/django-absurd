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
- Create: `tests/pg_cron/test_sync_schedules_on_migrate.py`

**Interfaces:**

- Produces: `django_absurd.pg_cron.apps.ORIGINAL_DATABASE_NAMES: dict[str, str]`
  (module-level, populated in `PgCronConfig.ready()`); the two new per-backend `OPTIONS`
  keys read via `backend.options.get(key, default)`.
- Consumes: `django_absurd.backends.get_absurd_backends()`,
  `django_absurd.backends.AbsurdBackend` (already imported/used in this file);
  `django.db.connections`, `django.conf.settings` (new imports, safe at module level —
  this file already imports `django.db.utils`/`django.db.models.signals` at module
  level, and these two are only ever _called_ later, at signal-fire time, long after
  `django.setup()` completes).

- [ ] **Step 1: Write the failing tests in
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

import psycopg
import pytest
from django.core.management import call_command
from django.db import connections

from django_absurd.pg_cron.models import ScheduledTask
from tests.pg_cron.utils import build_pg_cron_tasks

if t.TYPE_CHECKING:
    import pytest_django.fixtures

pytestmark = pytest.mark.django_db(transaction=True)

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_DB_NAME = "absurd_sync_schedules_real_db_check"

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
                "QUEUES": ["default"],
                "SCHEDULE": {{
                    "nightly": {{"task": "tests.tasks.add", "cron": "0 2 * * *"}},
                }},
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


def create_real_db() -> None:
    params = connections["default"].get_connection_params()
    params["dbname"] = "postgres"
    with psycopg.connect(**params, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{REAL_DB_NAME}"')
        cur.execute(f'CREATE DATABASE "{REAL_DB_NAME}"')


def drop_real_db() -> None:
    params = connections["default"].get_connection_params()
    params["dbname"] = "postgres"
    with psycopg.connect(**params, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{REAL_DB_NAME}"')


def migrate_real_db_in_subprocess() -> None:
    params = connections["default"].get_connection_params()
    script = SUBPROCESS_SCRIPT.format(
        dbname=REAL_DB_NAME,
        user=params.get("user", ""),
        password=params.get("password", ""),
        host=params.get("host", "localhost"),
        port=params.get("port", ""),
    )
    subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        cwd=REPO_ROOT,
        check=True,
    )


def count_cron_jobs_in(dbname: str) -> int:
    params = connections["default"].get_connection_params()
    params["dbname"] = dbname
    with psycopg.connect(**params, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("select count(*) from cron.job")
        row = cur.fetchone()
        assert row is not None
        return row[0]


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


def test_migrate_syncs_by_default_on_a_real_non_test_database() -> None:
    create_real_db()
    try:
        migrate_real_db_in_subprocess()
        assert count_cron_jobs_in(REAL_DB_NAME) == 1
    finally:
        drop_real_db()


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
    assert ScheduledTask.pg_cron.get_job("direct_create", "a") is not None

    scheduled_task.delete()
    assert ScheduledTask.pg_cron.get_job("direct_create", "a") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/pg_cron/test_sync_schedules_on_migrate.py -v --no-cov`
Expected: `test_migrate_skips_sync_by_default_on_a_test_database` FAILS (the guard
doesn't exist yet — `migrate` syncs unconditionally today, so
`ScheduledTask.pg_cron.get_managed_jobs()` is non-empty). The other three tests PASS
already (they exercise behavior that's either already correct — real-DB sync, per-row
create/delete — or explicitly opts in to today's behavior).

- [ ] **Step 3: Implement the guard in `django_absurd/pg_cron/apps.py`**

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
project's "helpers live below the public function that uses them" convention):

```python
def resolve_sync_schedules_option(backend: AbsurdBackend) -> bool:
    is_test_db = (
        connections[backend.database].settings_dict["NAME"]
        != ORIGINAL_DATABASE_NAMES.get(backend.database)
    )
    if is_test_db:
        return bool(backend.options.get("SYNC_SCHEDULES_ON_TEST_DB", False))
    return bool(backend.options.get("SYNC_SCHEDULES_ON_MIGRATE", True))
```

Add `AbsurdBackend` to this file's imports (it's currently only imported implicitly via
`get_absurd_backends`'s return type as a string annotation — add an explicit
`TYPE_CHECKING` import block if one doesn't already exist):

```python
if t.TYPE_CHECKING:
    from django_absurd.backends import AbsurdBackend
```

- [ ] **Step 4: Run the new tests, verify they pass**

Run: `uv run pytest tests/pg_cron/test_sync_schedules_on_migrate.py -v --no-cov`
Expected: all 4 tests PASS.

- [ ] **Step 5: Run the full existing pg_cron suite — confirm it's now broken**

Run: `uv run pytest tests/pg_cron -v --no-cov 2>&1 | tail -40` Expected: multiple
failures in `test_pg_cron_post_migrate.py` — the `run_cron_sync` fixture's `"migrate"`
parametrization now silently skips syncing (since every test in this suite runs on a
test DB, and `SYNC_SCHEDULES_ON_TEST_DB` defaults `False`), while the
`"absurd_sync_crons"` parametrization still works — the two branches diverge where tests
assert they're identical. This is expected and matches the Global Constraints — proceed
to Step 6 to fix it.

- [ ] **Step 6: Fix `tests/pg_cron/utils.py::build_pg_cron_tasks` to restore this
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

- [ ] **Step 7: Run the full pg_cron suite again, confirm it's green**

Run: `uv run pytest tests/pg_cron -v --no-cov` Expected: all tests PASS, including every
`test_pg_cron_post_migrate.py` test using `run_cron_sync`.

- [ ] **Step 8: Run the new dedicated test file once more (confirms Step 6 didn't
      regress it — its tests explicitly override `SYNC_SCHEDULES_ON_TEST_DB` back down
      per-test, so `build_pg_cron_tasks`'s new default shouldn't affect them, but verify
      directly rather than assume)**

Run: `uv run pytest tests/pg_cron/test_sync_schedules_on_migrate.py -v --no-cov`
Expected: all 4 tests still PASS.

- [ ] **Step 9: Full suite + mypy + ruff clean**

Run: `uv run pytest tests/pg_cron --create-db -v` (full run with coverage; use the
`ALTER DATABASE ... WITH ALLOW_CONNECTIONS false` + terminate-backend dance from
`CLAUDE.md` first if pg_cron's launcher blocks the `--create-db` drop) Run:
`uv run mypy django_absurd/pg_cron/apps.py tests/pg_cron/utils.py tests/pg_cron/test_sync_schedules_on_migrate.py`
Run:
`uv run ruff check django_absurd/pg_cron/apps.py tests/pg_cron/utils.py tests/pg_cron/test_sync_schedules_on_migrate.py`
Expected: full suite passes, no missed lines/branches in `apps.py`'s new code; mypy and
ruff clean.

- [ ] **Step 10: Commit**

```bash
git add django_absurd/pg_cron/apps.py tests/pg_cron/utils.py tests/pg_cron/test_sync_schedules_on_migrate.py
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

- **Spec coverage:** both `OPTIONS` keys + their defaults, the `ORIGINAL_DATABASE_NAMES`
  snapshot mechanism (with the same-object bug fix baked in from the start — this plan
  never contains the broken version), the guard's placement (receiver only, verified
  against `absurd_sync_crons`'s separate call site), keeping the existing suite green
  (`build_pg_cron_tasks` default), the two-pronged test strategy (in-process test-DB
  branch + subprocess real-DB branch), docs in both locations. All present.
- **Placeholder scan:** none — every step has real, complete code and exact commands.
- **Type consistency:** `resolve_sync_schedules_option(backend: AbsurdBackend) -> bool`
  matches how Task 1 Step 3 calls it (`resolve_sync_schedules_option(backend)` inside
  `reconcile_crons_after_migrate`, where `backend` is already typed via
  `next(iter(absurd_backends.items()))`, itself typed by
  `get_absurd_backends() -> dict[str, AbsurdBackend]`).
  `ORIGINAL_DATABASE_NAMES: dict[str, str]` matches both its population site (`ready()`,
  string values from `settings.DATABASES[alias]["NAME"]`) and its read site
  (`resolve_sync_schedules_option`, `.get(backend.database)` against
  `backend.database: str`).
