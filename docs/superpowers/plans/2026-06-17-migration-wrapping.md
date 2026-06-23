# django-absurd Migration Wrapping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans.
> Steps use checkbox (`- [ ]`) syntax. Plans state SCENARIOS + RED tests + prose
> implementation — NOT finished implementation code. Write the test, watch it fail, then
> implement minimally.

**Goal:** A pip-distributable Django app `django_absurd` whose `migrate` installs the
Absurd Postgres schema at a pinned version, sourced offline from the installed
`absurdctl` wheel.

**Architecture:** A single initial migration `0001_initial_<ver>` wraps Absurd's full
bundled schema (from `absurdctl.BUNDLED_SCHEMA_SQL`) + a concrete version stamp, as a
`RunSQL` operation. Static committed files → deterministic client `migrate`. Ongoing
**migration maintenance** (codegen, per-release deltas, drift/upstream tests, SDK-floor
automation) is a SEPARATE later spec — see
`docs/superpowers/specs/2026-06-17-migration-maintenance-design.md` (intention saved,
not built here).

**Tech Stack:** Python ≥3.10, Django ≥5.2 (LTS), `absurd-sdk` (runtime), setuptools
(package at repo root, no `src/`), pytest + pytest-django against real Postgres via
compose, `absurdctl==<ver>` + `psycopg` (dev only), ruff + pre-commit, GitHub Actions,
Renovate.

## Global Constraints

- Postgres only. Targets `DATABASES['default']` only — no multi-DB routing.
- **NO network, ever.** All Absurd SQL comes ONLY from the `absurdctl` wheel installed
  per `pyproject.toml`. No GitHub, no `urllib`/`requests`.
- Package lives at repo root as `django_absurd/` — **no `src/` layout**.
- `ABSURD_SCHEMA_VERSION` lives in `django_absurd/__init__.py` (public, top-level). **No
  `_version.py`, no `_sql.py`, no underscore-private modules.** Migrations read their
  `.sql` inline.
- Runtime deps: `Django>=5.2`, `absurd-sdk>=ABSURD_SCHEMA_VERSION,<next-minor` (floor =
  schema head, rises when a migration is added; ceiling = next minor). **psycopg is NOT
  a runtime dep** — Django users choose their own driver (`absurd-sdk` brings psycopg
  transitively for worker use).
- Dev deps: `absurdctl==<ver>` (hard pin — reproducible source), `psycopg` (no
  `[binary]`), pytest stack, ruff, pre-commit, build.
- Initial migration SQL = `absurdctl.BUNDLED_SCHEMA_SQL` + appended concrete
  `get_schema_version()` stamp = `absurdctl.ABSURD_SCHEMA_TARGET_VERSION` (the bundled
  body reports `'main'`).
- Naming: `{seq}_{label}_{ver}` single-underscore; ver = Absurd version,
  dots→underscores. `0001_initial_0_4_0`.
- `0001` reverse = `DROP SCHEMA IF EXISTS absurd CASCADE` (destructive; teardown only).
  The bootstrap full-schema install is transaction-safe → use Django's default
  `atomic = True` (omit the line). Any `concurrently` in the bundled schema lives only
  in comments / string literals / plpgsql function bodies (runtime
  `DETACH ... CONCURRENTLY`), not in the migration's own DDL — so it does NOT make the
  migration non-transactional. (Detecting genuine top-level `CONCURRENTLY` in future
  deltas is the deferred migration-maintenance spec's job.) RunSQL is paramless →
  psycopg3 simple-query protocol runs the multi-statement bundle. No Django models → no
  `state_operations`.
- Greenfield only (django-absurd owns `absurd`); pre-existing-Absurd-DB adoption =
  manual `migrate --fake` (documented). pg_cron NOT created by any migration. `migrate`
  creates NO queues (worker spec handles that).
- Wheel ships ONLY `django_absurd/**` incl. migration `*.sql` as package data; `tests/`,
  `docs/` excluded.
- Tooling cherry-picked from django-layout (NOT `ll:startproject`). Dev/CI via
  `docker compose run --rm app pytest`. No Makefile.
- `pytest-socket` `--disable-socket` + `--allow-hosts=db,localhost,127.0.0.1`.
- Tests: pytest, function-based only. **Scenario/outcome-focused — no granular unit
  tests.**

> **Run convention:** every command runs in the container — prefix with
> `docker compose run --rm app`. The `db` service auto-starts. Bare commands shown for
> brevity.

---

## Scenarios (what spec 1 must prove)

1. **Package loads.** `django_absurd` imports, the app registers, and
   `ABSURD_SCHEMA_VERSION` is exposed as a concrete semver.
2. **Fresh `migrate` installs Absurd at the pinned version.** On an empty DB, `migrate`
   creates the `absurd` schema; `absurd.get_schema_version()` equals
   `ABSURD_SCHEMA_VERSION`; a representative object (`absurd.queues`) exists.
3. **Reverse tears down cleanly.** `migrate django_absurd zero` drops the `absurd`
   schema.
4. **Distribution boundary holds.** The built wheel contains the migration `.sql`, and
   excludes `tests/`.

---

## Task 1: Package, dev env, tooling (Scenario 1)

**Files:**

- Create: `django_absurd/__init__.py` (holds `ABSURD_SCHEMA_VERSION`),
  `django_absurd/apps.py`, `django_absurd/migrations/__init__.py`
- Create: `pyproject.toml`, `tests/__init__.py`, `tests/settings.py`,
  `tests/conftest.py`
- Create: `compose.yaml`, `docker/app/Dockerfile`, `.dockerignore`,
  `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `renovate.json`
- Test: `tests/test_app.py`

**Interfaces:**

- Produces: `django_absurd.ABSURD_SCHEMA_VERSION: str`; app label `"django_absurd"`;
  container dev env.

- [ ] **Step 1: Write the failing test (RED)** — `tests/test_app.py`

```python
import re

from django.apps import apps

from django_absurd import ABSURD_SCHEMA_VERSION


def test_app_is_registered():
    assert apps.get_app_config("django_absurd").name == "django_absurd"


def test_schema_version_is_concrete_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", ABSURD_SCHEMA_VERSION)
```

- [ ] **Step 2: Confirm RED**

Run: `uv run --no-project --with Django pytest tests/test_app.py -v` Expected: FAIL —
`ModuleNotFoundError: No module named 'django_absurd'`.

- [ ] **Step 3: Implement (prose)**

- `pyproject.toml`: project `name = "django-absurd"`, `requires-python = ">=3.10"`.
  Runtime `dependencies`: `Django>=5.2`, `absurd-sdk>=<ver>,<<next-minor>` (set `<ver>`
  to the absurdctl version installed, ceiling = next minor). `[dependency-groups] dev`:
  `absurdctl==<ver>` (hard pin), `psycopg` (no binary), `pytest`, `pytest-django`,
  `pytest-socket`, `pytest-cov`, `django-coverage-plugin`, `django-stubs`, `mypy`,
  `ruff`, `build`. setuptools: package found at root (`packages.find` with no `src`),
  `package-data` includes `django_absurd/migrations/*.sql`. Adopt django-layout
  `[tool.*]`: ruff `select=["ALL"]` + ignores (`D`, `ANN401`, `ARG001/2`, `COM812`,
  `FBT`, `PLR2004`, `RUF012`; per-file `tests/** S101`, migrations `E501`), pytest
  (`DJANGO_SETTINGS_MODULE=tests.settings`,
  `--reuse-db --strict-markers --disable-socket --allow-hosts=db,localhost,127.0.0.1`),
  coverage, mypy/django-stubs. Run `uv lock`.
- `django_absurd/__init__.py`: define `ABSURD_SCHEMA_VERSION = "<ver>"` (the pinned
  Absurd version; single public source of truth).
- `django_absurd/apps.py`: `AbsurdConfig(AppConfig)` with
  `name = label = "django_absurd"`.
- `django_absurd/migrations/__init__.py`: empty.
- `tests/settings.py`: `INSTALLED_APPS` = contenttypes, auth, `django_absurd`.
  `DATABASES['default']` built from `PG*` env vars
  (`PGHOST`/`PGUSER`/`PGPASSWORD`/`PGDATABASE`/`PGPORT`), engine
  `django.db.backends.postgresql`. No `dj-database-url`.
- `tests/conftest.py`: placeholder for shared fixtures.
- `compose.yaml`: `db` (postgres:16-alpine, `POSTGRES_PASSWORD=postgres`, named
  volume) + `app` (build `docker/app/Dockerfile`, mount `.:/app` + anon `.venv`, `PG*`
  env pointing at `db`, `depends_on: db`, no default command).
- `docker/app/Dockerfile`: `python:3.12-slim` + `uv`, `uv sync --locked` (deps then
  project). **No `CMD`** — runs are explicit via compose.
- `.dockerignore`: `.git`, `.venv`, `__pycache__`, `*.egg-info`, `docs`, `.data`.
- `.pre-commit-config.yaml`: django-layout's set — std hooks, yamllint,
  check-github-workflows, ruff-check + ruff-format, hadolint, pretty-format-toml,
  uv-sort, mypy (local via compose), renovate-config-validator — **plus prettier**
  scoped to docs/config formats (markdown, yaml, json), **excluding html**. Drop the
  README `cog` hook.
- `.github/workflows/ci.yml`: on push/PR; build image, `ruff check`, `pytest` — all via
  `docker compose run`.
- `renovate.json`: `config:recommended` (PyPI manager bumps the `absurdctl==` pin
  automatically).

- [ ] **Step 4: GREEN**

Run:
`uv lock && docker compose build app && docker compose run --rm app pytest tests/test_app.py -v`
→ PASS (2).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock django_absurd/ tests/ compose.yaml docker/ .dockerignore .pre-commit-config.yaml .github/ renovate.json
git commit -m "feat: package skeleton, dockerized dev env, tooling"
```

---

## Task 2: Initial Absurd migration (Scenarios 2 & 3)

**Files:**

- Create (one-time generated): `django_absurd/migrations/0001_initial_<ver>.sql`,
  `django_absurd/migrations/0001_initial_<ver>.py`
- Test: `tests/test_migrations.py`

**Interfaces:**

- Consumes: the migration graph; `tests.settings` Postgres DB;
  `django_absurd.ABSURD_SCHEMA_VERSION`.

- [ ] **Step 1: Write the failing tests (RED)** — `tests/test_migrations.py`

```python
import pytest
from django.core.management import call_command
from django.db import connection

from django_absurd import ABSURD_SCHEMA_VERSION


def _scalar(sql):
    with connection.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


@pytest.mark.django_db
def test_migrate_installs_absurd_schema_at_pinned_version():
    assert _scalar("SELECT to_regnamespace('absurd') IS NOT NULL") is True
    assert _scalar("SELECT to_regclass('absurd.queues') IS NOT NULL") is True
    assert _scalar("SELECT absurd.get_schema_version()") == ABSURD_SCHEMA_VERSION


@pytest.mark.django_db(transaction=True)
def test_reverse_drops_absurd_schema():
    call_command("migrate", "django_absurd", "zero", verbosity=0)
    assert _scalar("SELECT to_regnamespace('absurd') IS NULL") is True
    call_command("migrate", "django_absurd", verbosity=0)  # restore
    assert _scalar("SELECT absurd.get_schema_version()") == ABSURD_SCHEMA_VERSION
```

- [ ] **Step 2: Confirm RED**

Run: `docker compose run --rm app pytest tests/test_migrations.py -v` Expected: FAIL —
no `absurd` schema (no migration exists yet).

- [ ] **Step 3: Generate the initial migration SQL (one-time, offline, prose)**

In the container, extract the schema from the installed pinned `absurdctl` and write it
to `django_absurd/migrations/0001_initial_<ver>.sql`:

- read `absurdctl.BUNDLED_SCHEMA_SQL`,
- append a concrete stamp:
  `create or replace function absurd.get_schema_version () returns text language sql as $$ select '<ABSURD_SCHEMA_TARGET_VERSION>'::text $$;`
  (because the bundled body reports `'main'`),
- `<ver>` in the filename = `absurdctl.ABSURD_SCHEMA_TARGET_VERSION`, dots→underscores.

This is a throwaway extraction (no shipped codegen — that's the deferred maintenance
spec). Example one-off:

```bash
docker compose run --rm app python - <<'PY'
import absurdctl, pathlib
v = absurdctl.ABSURD_SCHEMA_TARGET_VERSION
sql = absurdctl.BUNDLED_SCHEMA_SQL + (
    "\n\n-- django-absurd: concrete schema version (bundled body reports 'main')\n"
    f"create or replace function absurd.get_schema_version () returns text language sql\n"
    f"as $$ select '{v}'::text $$;\n"
)
p = pathlib.Path(f"django_absurd/migrations/0001_initial_{v.replace('.', '_')}.sql")
p.write_text(sql, encoding="utf-8")
print("wrote", p, "stamp", v)
PY
```

- [ ] **Step 4: Implement the migration module (prose)**

Create `django_absurd/migrations/0001_initial_<ver>.py`: a standard
`migrations.Migration` with `initial = True`, `dependencies = []`, one
`migrations.RunSQL` operation whose `sql` reads the sibling `0001_initial_<ver>.sql`
inline (via
`importlib.resources.files("django_absurd.migrations").joinpath("<name>.sql").read_text()`),
`reverse_sql = "DROP SCHEMA IF EXISTS absurd CASCADE;"`. Use Django's default
`atomic = True` (omit the line) — the full-schema bootstrap is transaction-safe;
`concurrently` only appears in comments/strings/function-bodies, not top-level DDL. No
separate loader module — the read is inline.

- [ ] **Step 5: GREEN**

Run: `docker compose run --rm app pytest tests/test_migrations.py -v` → PASS (2). This
also proves the multi-statement bundle executes under psycopg3 (paramless RunSQL →
simple-query protocol). (If a future delta ever introduced a top-level
`CREATE INDEX CONCURRENTLY`, that migration would need `atomic = False` — but the
bootstrap has none.)

Also confirm a clean graph: `python -m django makemigrations --check --dry-run` → no
changes for `django_absurd`.

- [ ] **Step 6: Commit**

```bash
git add django_absurd/migrations/0001_initial_*.sql django_absurd/migrations/0001_initial_*.py tests/test_migrations.py
git commit -m "feat: initial Absurd schema migration (0001, from absurdctl bundled schema)"
```

---

## Task 3: Distribution boundary (Scenario 4)

**Files:**

- Test: `tests/test_packaging.py`

- [ ] **Step 1: Write the failing test (RED)** — `tests/test_packaging.py`

```python
import subprocess
import sys
import zipfile
from pathlib import Path


def test_wheel_ships_migration_sql_and_excludes_tests(tmp_path):
    root = Path(__file__).resolve().parent.parent
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(tmp_path)],
        check=True, cwd=root,
    )
    names = zipfile.ZipFile(next(tmp_path.glob("*.whl"))).namelist()
    assert any(n.startswith("django_absurd/migrations/") and n.endswith(".sql") for n in names)
    assert not any(n.startswith("tests/") for n in names)
```

- [ ] **Step 2: Confirm RED → GREEN**

Run: `docker compose run --rm app pytest tests/test_packaging.py -v`. If the `.sql` is
missing from the wheel, fix `[tool.setuptools.package-data]` (Task 1) to include
`django_absurd/migrations/*.sql`, then GREEN.

- [ ] **Step 3: Full gate + commit**

Run: `docker compose run --rm app pytest` (all green) and
`docker compose run --rm --no-deps app ruff check .` (clean).

```bash
git add tests/test_packaging.py
git commit -m "test: wheel ships migration sql, excludes tests"
```

---

## Deferred to a separate spec: Migration maintenance

NOT built here. Intention saved in
`docs/superpowers/specs/2026-06-17-migration-maintenance-design.md`:

- `gen_migrations` codegen (bootstrap + per-release deltas via
  `absurdctl migrate --from/--to --dump-sql`), append-only.
- Drift tests: sql↔migration bijection; head version == highest migration; offline
  upstream check (head == `absurdctl.ABSURD_SCHEMA_TARGET_VERSION`).
- Auto-maintain the `absurd-sdk` floor (rises with head) in `pyproject.toml`.
- Renovate-driven upgrade loop (bump `absurdctl==` pin → regen → tests gate).

---

## Self-Review

- **Scenario coverage:** Scenario 1 → Task 1 `test_app.py`; Scenario 2 → Task 2 install
  test; Scenario 3 → Task 2 reverse test; Scenario 4 → Task 3 wheel test. All covered.
- **No pre-written implementation:** tasks show RED tests + prose implementation only
  (Task 1/2/4 impl is prose; the one-off extraction in Task 2 Step 3 is an ops command,
  not shipped code).
- **Feedback applied:** no `src/` (root package); version in `__init__` (no
  `_version.py`); no `_sql.py`/underscore modules (inline read); Django ≥5.2; absurd-sdk
  runtime up front; psycopg dev-only, no `[binary]`; absurdctl hard-pinned; no
  `dj-database-url`; no Dockerfile `CMD`; prettier kept (md/yaml, not html); migration
  maintenance pulled out.

```

```
