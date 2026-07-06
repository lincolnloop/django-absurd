# Test-suite core/pg_cron fork — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fork the test layout into two nested suites — `tests/core/` (the
`django_absurd.pg_cron` app ABSENT, running on a plain non-pg_cron Postgres) and
`tests/pg_cron/` (the app INSTALLED, running on a pg_cron-enabled Postgres, holding all
pg_cron + co-existence tests) — so the opt-in boundary is enforced by construction, not
by a marker.

**Architecture:** Mirror the existing `tests/multidb/` nested-suite pattern (own
`pytest.toml` + `settings.py` + `conftest.py`, run separately in tox). Repo-root
`compose.yaml` grows from one pg_cron db to two services: a plain `db` (core + multidb)
and a `db_pg_cron` (pg_cron suite). The base `tests/settings.py` drops the pg_cron app
(becomes the core config); `tests/pg_cron/settings.py` re-adds it and points at the
pg_cron server. The `pg_cron` pytest marker is removed — directory membership replaces
it. `uv run pytest` at root no longer runs "everything"; each suite is invoked
explicitly and tox runs all three.

**Tech Stack:** pytest / pytest-django, tox-uv, Docker Compose, Postgres 18 (+ pg_cron
≥1.4 on one service), Django 6.

## Global Constraints

- Runtime floor **Django 6.0 / Python 3.12**; **psycopg3**.
- Tests: **pytest function-based only**; **no monkeypatch/unittest.mock**; drive
  checks/commands by running them + asserting full emitted text; prefer the `settings`
  fixture; autouse `_enable_db(db)` for DB access;
  `@pytest.mark.django_db(transaction=True)` only for commits/DDL.
- **No history-narration comments**; **no ruff ignores/noqa without asking**; helpers
  below caller; `import typing as t`; absolute imports.
- **`cron.database_name` must equal the pg_cron suite's TEST db NAME** —
  `CREATE EXTENSION pg_cron` is only permitted in that DB.
- **Core suite must not depend on pg_cron**: it runs on a server with NO
  `shared_preload_libraries=pg_cron`; any test needing the extension belongs in
  `tests/pg_cron/`.
- Coverage source stays `["django_absurd", "tests"]`; combine across suites with `--cov`
  (core) + `--cov-append` (pg_cron); `tests/multidb/*` stays omitted.
- Preserve every existing test's assertions verbatim on move — this is a relocation, not
  a rewrite (except the E008/W003 split in Task 4, which is a genuine behavioral
  improvement).

---

### Task 1: Two-DB compose + base settings = core (no pg_cron)

Split the single pg_cron db into a plain default `db` (core/multidb) and a pg_cron
`db_pg_cron`; make base `tests/settings.py` the core config (pg_cron app dropped, points
at the plain server).

**Files:**

- Modify: `compose.yaml` (plain `db` + new `db_pg_cron` service)
- Modify: `.envrc` (add `PGPORT_PGCRON`)
- Modify: `tests/settings.py` (drop `"django_absurd.pg_cron"` from INSTALLED_APPS; TEST
  NAME `absurd_test_core`)

**Interfaces:**

- Produces: plain server on host `${PGPORT:-5432}` (db `postgres`, TEST
  `absurd_test_core`); pg_cron server on host `${PGPORT_PGCRON:-5434}`
  (`shared_preload_libraries=pg_cron`, `cron.database_name=absurd_test_pg_cron`). Base
  `tests/settings.py` INSTALLED_APPS has NO pg_cron app and DB points at the plain
  server.

- [ ] **Step 1: Rewrite `compose.yaml` to two services**

```yaml
---
services:
  db:
    # Plain Postgres (no pg_cron) — the core suite and tests/multidb run here so
    # they cannot depend on the extension.
    image: postgres:18
    environment:
      - POSTGRES_PASSWORD=postgres
    ports:
      - "${PGPORT:-5432}:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      timeout: 5s
      retries: 10
  db_pg_cron:
    # pg_cron-enabled Postgres for tests/pg_cron/. cron.database_name must match
    # the pg_cron suite's TEST db NAME (CREATE EXTENSION is only allowed there).
    build:
      context: .
      dockerfile: Dockerfile.pg_cron
    command:
      - postgres
      - -c
      - shared_preload_libraries=pg_cron
      - -c
      - cron.database_name=absurd_test_pg_cron
    environment:
      - POSTGRES_PASSWORD=postgres
    ports:
      - "${PGPORT_PGCRON:-5434}:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      timeout: 5s
      retries: 10
    volumes:
      - pgdata_pg_cron:/var/lib/postgresql

volumes:
  pgdata_pg_cron:
```

(The plain `db` needs no named volume — test DBs are disposable; dropping the old
`pgdata` volume avoids the alpine→glibc collation note entirely.)

- [ ] **Step 2: Add `PGPORT_PGCRON` to `.envrc`**

Append: `export PGPORT_PGCRON=5434` with a one-line comment (host port for the pg_cron
test server; `tests/pg_cron/settings.py` reads it).

- [ ] **Step 3: Make base `tests/settings.py` the core config**

Remove `"django_absurd.pg_cron"` from `INSTALLED_APPS` (leave `"django_absurd"` + the
rest). Change `DATABASES["default"]["TEST"]` to `{"NAME": "absurd_test_core"}`. Leave
`DATABASE_ROUTERS`, `TASKS` (default backend, beat) as-is. This base is imported by
every suite; core uses it directly.

- [ ] **Step 4: Verify the two servers**

Run:

```bash
docker compose up -d --wait db db_pg_cron
docker compose exec db psql -U postgres -c "show shared_preload_libraries"          # empty (no pg_cron)
docker compose exec db_pg_cron psql -U postgres -c "show shared_preload_libraries"  # pg_cron
```

Expected: plain `db` has no pg_cron; `db_pg_cron` loads it.

- [ ] **Step 5: Commit**

```bash
git add compose.yaml .envrc tests/settings.py
git commit -m "test: split compose into plain db + db_pg_cron; base settings drop pg_cron app"
```

---

### Task 2: `tests/core/` nested suite (app absent, plain DB)

Create the core suite and move every NON-pg_cron test into it. Mirror `tests/multidb/`
wiring.

**Files:**

- Create: `tests/core/__init__.py`, `tests/core/pytest.toml`, `tests/core/settings.py`,
  `tests/core/conftest.py`
- Move (git mv, verbatim) into `tests/core/`: `test_admin_backend_resolve.py`,
  `test_admin_checks.py`, `test_admin_models.py`, `test_admin_views.py`, `test_app.py`,
  `test_async_worker.py`, `test_backend.py`, `test_checks.py`, `test_enqueue.py`,
  `test_migrations.py`, `test_models.py`, `test_orm_models.py`, `test_orm_views.py`,
  `test_packaging.py`, `test_params.py`, `test_queue_sync.py`, `test_results.py`,
  `test_router_default.py`, `test_scheduler.py`, `test_scheduler_checks.py`,
  `test_worker.py`
- Keep at `tests/` root (shared, imported by suites): `__init__.py`, `settings.py`,
  `conftest.py` (shared fixtures — see Step 3), `tasks.py`, `atasks.py`, `jobs.py`,
  `models.py`, `admin.py`, `urls.py`, `raises_on_import.py`

**Interfaces:**

- Produces: `tests/core/settings.py` (`from tests.settings import *`, unchanged
  INSTALLED_APPS = no pg_cron); `tests/core/` collects all core tests against the plain
  `db`.
- Consumes: shared helper modules under `tests/` (`tests.tasks`, `tests.models`, etc.)
  via absolute import (unchanged import paths, since the suites keep
  `pythonpath=["../.."]`).

- [ ] **Step 1: Create `tests/core/pytest.toml`** (mirror `tests/multidb/pytest.toml`,
      add coverage since core is the primary cov run)

```toml
[pytest]
DJANGO_SETTINGS_MODULE = "tests.core.settings"
addopts = [
  "--allow-hosts=db,localhost,127.0.0.1",
  "--cov",
  "--cov-report=term",
  "--cov-report=xml",
  "--disable-socket",
  "--reuse-db",
  "--strict-markers",
]
pythonpath = ["../.."]
testpaths = ["."]
```

- [ ] **Step 2: Create `tests/core/settings.py`**

```python
from tests.settings import *  # noqa: F403

# Base tests.settings is already the core config (pg_cron app absent, plain db).
# This module exists so the suite has its own DJANGO_SETTINGS_MODULE, matching
# the tests/multidb pattern.
```

(If `from tests.settings import *` triggers a ruff F403/F401, keep the `# noqa: F403` —
matches `tests/multidb/settings.py` which already uses it.)

- [ ] **Step 3: Shared fixtures — one home, imported by suites**

The root `tests/conftest.py` currently holds fixtures used by BOTH core and pg_cron
tests (`_enable_db`, `_reset_absurd_queues`, `admin_user`, `staff_user`) AND
pg_cron-only ones (`ensure_pg_cron`, `_clear_owned_pg_cron_jobs`, `owned_cron_jobs`,
`cron_job_rows`). Split:

- Move the pg_cron-only fixtures OUT of `tests/conftest.py` into
  `tests/pg_cron/conftest.py` (Task 3).
- Keep the shared ones (`_enable_db`, `_reset_absurd_queues`, `admin_user`,
  `staff_user`) in `tests/conftest.py`.
- A nested suite does NOT automatically inherit `tests/conftest.py` across its own
  `pytest.toml` rootdir. Follow the multidb precedent: `tests/core/conftest.py` and
  `tests/pg_cron/conftest.py` each import the shared fixtures. Create
  `tests/core/conftest.py`:

```python
from tests.conftest import (  # noqa: F401
    _enable_db,
    _reset_absurd_queues,
    admin_user,
    staff_user,
)
```

(pytest fixtures are usable when imported into a conftest. Verify collection sees them;
if import-of-fixtures proves flaky, fall back to the multidb approach of defining
`_enable_db`/`_reset_absurd_queues` inline in each suite's conftest.)

- [ ] **Step 4: Move the core test files**

`git mv tests/<file> tests/core/<file>` for each file in the "Move" list above. Imports
inside them already use absolute `tests.` / `django_absurd.` paths, so no edits needed
(the suite keeps `pythonpath=["../.."]`).

- [ ] **Step 5: Run the core suite**

Run: `uv run pytest tests/core -q` Expected: all moved core tests pass against the plain
`db`; the pg_cron package is present on disk but its app is not installed, so nothing
pg_cron runs. If any moved test imported a pg_cron-only fixture or the `pg_cron` marker,
it was misclassified — move it to `tests/pg_cron/` (Task 3) instead.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "test: add tests/core suite (pg_cron app absent, plain db); move core tests"
```

---

### Task 3: `tests/pg_cron/` nested suite (app installed, pg_cron DB)

Create the pg_cron suite, move every pg_cron test into it, and relocate the pg_cron-only
fixtures.

**Files:**

- Create: `tests/pg_cron/__init__.py`, `tests/pg_cron/pytest.toml`,
  `tests/pg_cron/settings.py`, `tests/pg_cron/conftest.py`
- Move (git mv) into `tests/pg_cron/`: `test_pg_cron_checks.py`, `test_pg_cron_e2e.py`,
  `test_pg_cron_infra.py`, `test_pg_cron_naming.py`, `test_pg_cron_options.py`,
  `test_pg_cron_post_migrate.py`, `test_pg_cron_sync_jobs.py`,
  `test_pg_cron_sync_rows.py`, `test_pg_cron_teardown.py`, `test_run_scheduled_fn.py`,
  `test_scheduledtask_model.py`, `test_absurd_sync_crons_command.py`,
  `test_scheduler_selector.py`

**Interfaces:**

- Produces: `tests/pg_cron/settings.py` — `from tests.settings import *`, then re-add
  the pg_cron app + point at the pg_cron server.
- Consumes: `PGPORT_PGCRON` (Task 1); shared fixtures from `tests/conftest.py`.

- [ ] **Step 1: Create `tests/pg_cron/settings.py`**

```python
import os

from tests.settings import *  # noqa: F403

INSTALLED_APPS = [*INSTALLED_APPS, "django_absurd.pg_cron"]  # noqa: F405

DATABASES["default"]["HOST"] = os.environ.get("PGHOST", "localhost")  # noqa: F405
DATABASES["default"]["PORT"] = os.environ.get("PGPORT_PGCRON", "5434")  # noqa: F405
# TEST db name must equal db_pg_cron's cron.database_name so CREATE EXTENSION works.
DATABASES["default"]["TEST"] = {"NAME": "absurd_test_pg_cron"}  # noqa: F405
```

- [ ] **Step 2: Create `tests/pg_cron/pytest.toml`** (`--cov-append` so its coverage
      combines with core's)

```toml
[pytest]
DJANGO_SETTINGS_MODULE = "tests.pg_cron.settings"
addopts = [
  "--allow-hosts=db,db_pg_cron,localhost,127.0.0.1",
  "--cov",
  "--cov-append",
  "--cov-report=term",
  "--cov-report=xml",
  "--disable-socket",
  "--reuse-db",
  "--strict-markers",
]
pythonpath = ["../.."]
testpaths = ["."]
```

- [ ] **Step 3: Move pg_cron-only fixtures into `tests/pg_cron/conftest.py`**

Cut `ensure_pg_cron`, `_clear_owned_pg_cron_jobs`, `owned_cron_jobs`, `cron_job_rows`
from `tests/conftest.py` and paste into `tests/pg_cron/conftest.py`, plus import the
shared fixtures:

```python
from tests.conftest import (  # noqa: F401
    _enable_db,
    _reset_absurd_queues,
    admin_user,
    staff_user,
)
# ...then the four pg_cron fixtures moved verbatim from tests/conftest.py...
```

Update `ensure_pg_cron`'s docstring to state the pg_cron suite runs on the pg_cron
server (`db_pg_cron`), TEST db `absurd_test_pg_cron` = `cron.database_name`. Remove the
now-stale "default `uv run pytest` runs pg_cron tests" sentence (that behavior is gone —
the suite is directory-scoped).

- [ ] **Step 4: Move the pg_cron test files + drop the marker usage**

`git mv` each pg_cron test file into `tests/pg_cron/`. Every one currently carries
`pytestmark = [..., pytest.mark.pg_cron]` or `@pytest.mark.pg_cron` /
`pytest.mark.usefixtures("ensure_pg_cron", "_clear_owned_pg_cron_jobs")`. Remove the
`pytest.mark.pg_cron` entries (the marker is retired in Task 5) but KEEP the
`usefixtures("ensure_pg_cron", ...)` — those are still needed to create the extension +
clean jobs. Where a module used `pytest.mark.pg_cron` as its ONLY marker, replace with
the `usefixtures` it needs (most already have it).

- [ ] **Step 5: Run the pg_cron suite**

Run: `PGPORT_PGCRON=5434 uv run pytest tests/pg_cron -q` (or rely on `.envrc`).
Expected: all moved pg_cron tests pass against `db_pg_cron`; `ensure_pg_cron` creates
the extension in `absurd_test_pg_cron`.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "test: add tests/pg_cron suite (app installed, pg_cron db); move pg_cron tests + fixtures"
```

---

### Task 4: Split the scheduler-app checks across the fork (E008 → core, W003 → pg_cron)

`tests/test_scheduler_app_checks.py` currently tests both `absurd.E008`
(SCHEDULER=pg_cron but app absent) and `absurd.W003` (app present but mis-ordered) by
manipulating `settings.INSTALLED_APPS`. With the fork, each half runs where its
precondition is REAL.

**Files:**

- Move/split: `tests/test_scheduler_app_checks.py` →
  `tests/core/test_scheduler_app_checks.py` (E008, app genuinely absent) +
  `tests/pg_cron/test_scheduler_app_checks.py` (W003 + check-clean-when-installed)

**Interfaces:**

- Consumes: `check_scheduler_app_installed`, `E008_MSG`/`E008_HINT`,
  `W003_MSG`/`W003_HINT`, `PG_CRON_APP_NAME` from `django_absurd/checks.py`.

- [ ] **Step 1: Read the current tests + check code**

Read `tests/test_scheduler_app_checks.py` and `django_absurd/checks.py`
(`check_scheduler_app_installed`, `resolve_installed_app_names`). Note which assertions
depend on the app being ABSENT (E008) vs PRESENT+mis-ordered (W003).

- [ ] **Step 2: core E008 test — real absence**

In `tests/core/test_scheduler_app_checks.py`: the pg_cron app is genuinely absent from
this suite's INSTALLED_APPS, so `apps.is_installed("django_absurd.pg_cron")` is False
without manipulation. Test: set the default backend `OPTIONS["SCHEDULER"]="pg_cron"` via
the `settings` fixture, run `call_command("check", "django_absurd")`, assert the exact
`E008_MSG` + `E008_HINT`. Also a clean-case test: with the default (beat) scheduler,
`check` reports no issues.

- [ ] **Step 3: pg_cron W003 test — app present**

In `tests/pg_cron/test_scheduler_app_checks.py`: the app IS installed here. Test W003 by
ordering `settings.INSTALLED_APPS` with `"django_absurd.pg_cron"` before
`"django_absurd"` (mirror the existing W003 test, incl. the AppConfig-dotted-path case),
assert `W003_MSG`/`W003_HINT`; and the correctly-ordered case reports no W003. (E008
cannot fire here — app is installed — so those cases live in core, Step 2.)

- [ ] **Step 4: Run both halves**

Run: `uv run pytest tests/core/test_scheduler_app_checks.py -q` and
`uv run pytest tests/pg_cron/test_scheduler_app_checks.py -q`. Expected: both pass;
delete the original `tests/test_scheduler_app_checks.py`.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "test: split scheduler-app checks — E008 (app absent) to core, W003 to pg_cron"
```

---

### Task 5: Drop the `pg_cron` marker + rewire pyproject / tox / CI

Retire the marker (directory replaces it) and point all the config at the three suites.

**Files:**

- Modify: `pyproject.toml` (`[tool.pytest.ini_options]` addopts/markers/testpaths;
  `[tool.coverage.run].omit`)
- Modify: `tox.ini` (`[testenv]` + `[testenv:dev]` commands)
- Verify: `.github/workflows/test.yml` (compose brings up both db services)

**Interfaces:**

- Produces: `uv run pytest tests/core` / `tests/pg_cron` / `tests/multidb` as the three
  suites; tox runs all three; the `pg_cron` marker no longer exists.

- [ ] **Step 1: `pyproject.toml` — retire the root flat run + marker**

In `[tool.pytest.ini_options]`: remove the `pg_cron` entry from `markers`; the root
config no longer collects a flat suite. Set `addopts` to ignore all nested suites so a
bare `pytest` from root doesn't misfire, and keep the shared knobs:

```toml
addopts = [
  "--ignore=tests/core",
  "--ignore=tests/multidb",
  "--ignore=tests/pg_cron",
  "--strict-markers",
]
```

(Coverage/junit/socket flags now live in each suite's `pytest.toml`; the root run
collects nothing, which is intended — invoke a suite explicitly.) In
`[tool.coverage.run].omit`, keep `tests/multidb/*`; the core + pg_cron suites ARE
measured (via their own `--cov`/`--cov-append`).

- [ ] **Step 2: `tox.ini` — a line per suite**

`[testenv].commands` (drop the old marker lines):

```
!mypy: pytest tests/core {posargs}
!mypy: pytest tests/pg_cron {posargs}
!mypy: pytest tests/multidb {posargs}
mypy: mypy .
```

`[testenv:dev].commands`:

```
pytest tests/core {posargs}
pytest tests/pg_cron {posargs}
pytest tests/multidb {posargs}
```

Keep `pass_env = PG*` so `PGPORT`/`PGPORT_PGCRON`/`PGHOST` reach both servers. Update
the `packaging`-marker comment: `test_packaging.py` now lives in `tests/core/` — confirm
the `packaging` marker still deselects correctly in CI (the matrix must still skip it;
`dev` runs it). If `packaging` tests need the build backend, keep them in core and
ensure the matrix line excludes them:
`!mypy: pytest tests/core -m "not packaging" {posargs}`.

- [ ] **Step 3: CI `.github/workflows/test.yml` — bring up both servers**

Change the db step to `docker compose up -d --build --wait db db_pg_cron` so both
services are ready; export `PGPORT`/`PGPORT_PGCRON` matching the compose host ports for
the tox step (tox `pass_env = PG*` forwards them). Confirm the matrix env still runs
`uvx tox -e "${{ matrix.env }}"` (unchanged).

- [ ] **Step 4: Full verification (all three suites + boundary)**

Run:

```bash
docker compose up -d --wait db db_pg_cron
uv run pytest tests/core -q          # green, plain db, no pg_cron
uv run pytest tests/pg_cron -q       # green, pg_cron db
uv run pytest tests/multidb -q       # green, plain db
uv run python -m django makemigrations --check --settings tests.settings   # clean
uvx --with tox-uv tox -e py312-django60   # all three suites run under tox
grep -rn "mark.pg_cron\|\"pg_cron\":" pyproject.toml tests   # no marker refs remain
```

Boundary check: `tests/core` runs on `db` (no `shared_preload_libraries=pg_cron`), so
any accidental pg_cron dependency there fails loudly rather than silently passing.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tox.ini .github/workflows/test.yml
git commit -m "test: drop pg_cron marker; run core/pg_cron/multidb suites via tox"
```

---

### Task 6: Docs (CLAUDE.md testing section)

**Files:**

- Modify: `CLAUDE.md` (testing conventions)

- [ ] **Step 1: Update the testing section**

State the three suites and how to run each: `uv run pytest tests/core` (core, plain db),
`uv run pytest tests/pg_cron` (needs the pg_cron server on `PGPORT_PGCRON`),
`uv run pytest tests/multidb`; full matrix via `uvx --with tox-uv tox`. Note the two
compose services (`db` plain, `db_pg_cron`) and that
`docker compose up -d db db_pg_cron` starts both. Drop the retired `pg_cron` marker
wording and any "single-DB suite / default `uv run pytest` runs everything" phrasing.
Keep the `--create-db` note (now applies per suite). No history-narration.

- [ ] **Step 2: Verify + commit**

Grep `CLAUDE.md` for stale `-m pg_cron` / "deselected" / single-suite wording. Then:

```bash
git add CLAUDE.md
git commit -m "docs: document the core/pg_cron/multidb test-suite fork"
```

---

## Self-Review

**Spec coverage:** two-DB compose + base=core (Task 1) ✓; `tests/core/` full fork of
non-pg_cron tests (Task 2) ✓; `tests/pg_cron/` + fixtures + co-existence/dual-scheduler
already live in the pg_cron test files being moved (Task 3) ✓; E008/W003 split (Task 4)
✓; drop marker + pyproject/tox/CI rewiring (Task 5) ✓; docs (Task 6) ✓; multidb
untouched on plain db ✓.

**Open decisions flagged for plan review:** (a) root `uv run pytest` collects nothing —
suites are explicit (matches multidb precedent); (b) coverage combined via core
`--cov` + pg_cron `--cov-append`; (c) `packaging` tests land in `tests/core/` and the
matrix must keep deselecting them; (d) the dual-scheduler multi-backend beat test
currently in `test_scheduler.py` — it uses two beat backends (no pg_cron), so it moves
to `tests/core/` with `test_scheduler.py`; a test is "co-existence → pg_cron suite" only
when it actually involves the pg_cron scheduler.

**Placeholder scan:** new config files shown in full; moves enumerated file-by-file; the
one behavioral change (E008/W003 split) has explicit per-half specs. No TBD.

**Consistency:** `absurd_test_core` (plain) vs `absurd_test_pg_cron` (=
`cron.database_name`) used consistently; `PGPORT` (plain) vs `PGPORT_PGCRON` (pg_cron)
consistent across compose/.envrc/settings/tox/CI.
