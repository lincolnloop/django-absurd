# Configurable Absurd Database Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans.
> Steps use checkbox (`- [ ]`) syntax. Plans state SCENARIOS + RED tests + prose
> implementation — NOT finished implementation code. Write the test, watch it fail, then
> implement minimally.

**Goal:** Let a project run Absurd on a DB connection other than `default` via an
`ABSURD_DATABASE` setting + a `AbsurdRouter`, with the command/check/client honoring the
alias and a wrong-backend system check.

**Architecture:** A single `get_absurd_database()` reader (default `"default"`) drives
the router, `get_absurd_client`, `sync_queues`, the command, and the check.
`AbsurdRouter` routes only the `django_absurd` app to that alias (no-op at `"default"`).
The system check gains `E001` (wrong backend) and `W003` (alias set, router missing).

Multi-DB routing is exercised by a **separate, self-contained nested test suite**
(`tests/multidb/`) with its own `pytest.toml` + Django settings where
`ABSURD_DATABASE="absurd"` is the SESSION-GLOBAL value. This lets pytest-django
provision and reset the `absurd` test DB the normal way (`allow_migrate` is `True` at
session setup, so `0001` lands on `absurd`), so the routing tests assert against
already-provisioned state — NO in-test `migrate`/`migrate zero`/`DROP SCHEMA`, no
`django_migrations` scrub fixture, no `--reuse-db` state leak.

**Tech Stack:** Django ≥5.2 multi-DB (routers), absurd-sdk, psycopg3, pytest≥9 +
pytest-django via compose. pytest 9's auto-discovered `pytest.toml` (a `[pytest]` table)
selects the nested suite by running `pytest tests/multidb` — no `-c` needed; the root
run excludes it via `--ignore=tests/multidb`. Builds on merged specs 1–2
(`django_absurd/` at repo root). **Validated by spike:** `pytest` → single-DB suite
(nested skipped); `pytest tests/multidb` → nested suite under `tests.multidb.settings`.

## Global Constraints

- `ABSURD_DATABASE` — a `DATABASES` alias, default `"default"`. Read everywhere via
  `get_absurd_database() -> str` = `getattr(settings, "ABSURD_DATABASE", "default")`.
  Default `"default"` MUST be a full no-op (specs 1–2 unchanged).
- `AbsurdRouter` (in `django_absurd/routers.py`) routes ONLY `django_absurd`:
  `db_for_read`/`db_for_write` → `get_absurd_database()` for
  `app_label == "django_absurd"` else `None`; `allow_migrate(db, app_label, …)` →
  `db == get_absurd_database()` for `django_absurd` else `None`. No `allow_relation`.
  Non-prescriptive — never opines on other apps.
- `get_absurd_client`, `sync_queues`, command `--database`, and `check_absurd_queues`
  default the alias to `get_absurd_database()` (was literal `"default"`).
- System check states (queues declared): router-missing (`ABSURD_DATABASE != "default"`
  and `django_absurd.routers.AbsurdRouter` not in `settings.DATABASE_ROUTERS`) → `W003`
  (settings-only, checked first); can't connect (`OperationalError`) → `[]`; wrong
  backend (not `psycopg.Connection`) → `Error absurd.E001` (`BACKEND_ERR`); schema
  absent (`ProgrammingError`) → `W001`; drift → `W002`; in sync → `[]`. Backend
  validation precedes the queue query.
- Factor the connect+psycopg check into `validate_backend(using)` shared by
  `get_absurd_client` and the check (don't build an `Absurd` in the check just to
  validate).
- Migration state is per-DB (Django creates `django_migrations` on any migrated DB
  regardless of `allow_migrate`); two-step deploy `migrate` +
  `migrate --database <alias>`. (Deploy guidance only — NOT how the tests provision.)
- **Multi-DB tests live in a nested suite `tests/multidb/`** with its own
  auto-discovered `pytest.toml` (`[pytest]` table) and `tests/multidb/settings.py`
  (`from tests.settings import *` then `ABSURD_DATABASE="absurd"`, register
  `AbsurdRouter`, add the `absurd` Postgres alias **migrated normally** — no
  `TEST.MIGRATE: False`). Under these settings `ABSURD_DATABASE="absurd"` is
  session-global, so pytest-django provisions/resets `absurd` itself and routing tests
  assert against provisioned state (no in-test migrate/DDL, no scrub fixture). In
  `pytest.toml`, `addopts`/`pythonpath`/`testpaths` are TOML **lists**; rootdir is
  `tests/multidb/`, so `pythonpath = ["../.."]` (repo root on path for both
  `tests.multidb.settings` and `tests.settings`),
  `DJANGO_SETTINGS_MODULE = "tests.multidb.settings"`, `testpaths = ["."]`.
- The root pyproject `[tool.pytest.ini_options]` MUST add `"--ignore=tests/multidb"` to
  `addopts` AND set `testpaths = ["tests"]`, so bare `pytest` collects only the
  single-DB suite (router a no-op there at default) and never loads the nested test
  files under the wrong settings.
- Conventions (CLAUDE.md): `import typing as t`; absolute imports; verb-named functions;
  no monkeypatching; test commands/checks via `call_command(...)` + `capsys` asserting
  full messages; the autouse `_enable_db`/`_reset_absurd_queues` fixtures cover DB.
- Run in container: single-DB suite `docker compose run --rm app pytest`; multi-DB suite
  `docker compose run --rm app pytest tests/multidb`;
  `docker compose run --rm --no-deps app ruff check .`; `uvx pre-commit run --all-files`
  must pass (ruff + mypy + prettier).

> **Run convention:** prefix commands with `docker compose run --rm app`. `db`
> auto-starts.

---

## File Structure

- `django_absurd/queues.py` — add `get_absurd_database()`, `validate_backend(using)`;
  thread the alias default through `get_absurd_client`/`sync_queues`. (Task 1 — DONE,
  committed `c06aaba`; `_BACKEND_ERR` was renamed `BACKEND_ERR` per CLAUDE.md.)
- `django_absurd/routers.py` — new: `AbsurdRouter`.
- `django_absurd/management/commands/absurd_sync_queues.py` — `--database` defaults to
  the alias. (Task 1 — DONE.)
- `django_absurd/checks.py` — `W003` + `E001` + alias; reuse `validate_backend`.
- `tests/settings.py` — register `AbsurdRouter` (no-op while `ABSURD_DATABASE` defaults
  to `"default"`). Do NOT add the `absurd` alias here.
- `tests/multidb/` — NEW nested suite (its own world):
  - `pytest.toml` — `[pytest]` table; `DJANGO_SETTINGS_MODULE="tests.multidb.settings"`,
    `pythonpath=["../.."]`, `testpaths=["."]`, `addopts` list (carry
    `--allow-hosts`/`--disable-socket`/`--reuse-db`/`--strict-markers`).
  - `__init__.py`
  - `settings.py` — `from tests.settings import *` (for `INSTALLED_APPS` etc.);
    `ABSURD_DATABASE="absurd"`; register the router; **completely redefine `DATABASES`**
    (not derived) as two Postgres aliases (`default`, `absurd`) migrated normally — each
    with a distinct `_multidb`-affixed `TEST.NAME` so they never collide with the main
    suite's `--reuse-db` test DBs (`default` → `test_<name>_multidb`, `absurd` →
    `test_<name>_multidb_absurd`).
  - `conftest.py` — autouse queue reset on the `absurd` alias (the parent
    `tests/conftest.py` is above this rootdir and is NOT loaded); tests carry
    `pytestmark = pytest.mark.django_db(databases=["default", "absurd"])`.
  - `test_router.py` — hermetic routing tests (assert provisioned state).
- `pyproject.toml` `[tool.pytest.ini_options]` — add `"--ignore=tests/multidb"` to
  `addopts`; set `testpaths=["tests"]`.
- `tests/conftest.py` — `_reset_absurd_queues` also catches `ImproperlyConfigured`.
- Tests (main single-DB suite): additions to `tests/test_checks.py`,
  `tests/test_queue_sync.py`.

---

## Scenarios

1. Default `ABSURD_DATABASE` is a no-op (specs 1–2 unchanged).
2. `AbsurdRouter` routes `django_absurd` (migrations + ORM) to the alias.
3. Command/client honor the alias by default.
4. Check screams `E001` on a wrong backend and `W003` when the alias is set without the
   router.

---

## Task 1: `ABSURD_DATABASE` setting + thread the alias

**Files:**

- Modify: `django_absurd/queues.py`,
  `django_absurd/management/commands/absurd_sync_queues.py`
- Test: `tests/test_queue_sync.py` (additions)

**Interfaces:**

- Produces: `get_absurd_database() -> str`
  (`getattr(settings, "ABSURD_DATABASE", "default")`);
  `validate_backend(using: str) -> None` (ensure_connection + raise
  `ImproperlyConfigured(BACKEND_ERR)` if `connections[using].connection` is not
  `psycopg.Connection`); `get_absurd_client(using: str | None = None)` and
  `sync_queues(using: str | None = None)` resolving `using or get_absurd_database()`.
- Consumes: existing `get_absurd_client`, `sync_queues`, `BACKEND_ERR`.

- [ ] **Step 1: Write the failing tests (RED)** — add to `tests/test_queue_sync.py`

```python
def test_get_absurd_database_default_and_override(settings):
    from django_absurd.queues import get_absurd_database

    assert get_absurd_database() == "default"
    settings.ABSURD_DATABASE = "absurd"
    assert get_absurd_database() == "absurd"


def test_sync_command_uses_absurd_database_setting(settings):
    # With ABSURD_DATABASE pointing at the non-psycopg sqlite alias and no
    # --database flag, the command must resolve the alias and scream.
    settings.ABSURD_DATABASE = "sqlite"
    with pytest.raises(ImproperlyConfigured) as exc:
        call_command("absurd_sync_queues")
    assert str(exc.value) == (
        "django-absurd requires the psycopg (v3) PostgreSQL backend. "
        "See https://www.psycopg.org/psycopg3/docs/"
    )
```

(The second test also needs `databases=["default", "sqlite"]`; add that marker to the
function, overriding the module marker. Assert the literal message text — don't import
the `BACKEND_ERR` constant.)

- [ ] **Step 2: Confirm RED**

Run:
`docker compose run --rm app pytest tests/test_queue_sync.py -k "absurd_database or uses_absurd_database" -v`
Expected: FAIL — `get_absurd_database` missing / command still defaults to `"default"`.

- [ ] **Step 3: Implement (prose)**

In `queues.py`: add `get_absurd_database()` returning
`getattr(settings, "ABSURD_DATABASE", "default")`. Extract `validate_backend(using)`
from `get_absurd_client` (the `ensure_connection()` +
`isinstance(..., psycopg.Connection)` raise). Change
`get_absurd_client(using: str | None = None)` to
`using = using or get_absurd_database()`, then `validate_backend(using)`, then
`return Absurd(connections[using].connection)`. Change
`sync_queues(using: str | None = None)` to resolve
`using = using or get_absurd_database()` at the top. In the command:
`add_argument("--database", default=None, …)`; in `handle`,
`using = database or get_absurd_database()` and pass to `sync_queues(using=using)` (keep
`database` as the explicit kwarg, default now `None`).

- [ ] **Step 4: GREEN**

Run: `docker compose run --rm app pytest tests/test_queue_sync.py -v` → all pass
(existing + 2 new). Default behavior unchanged.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/queues.py django_absurd/management/commands/absurd_sync_queues.py tests/test_queue_sync.py
git commit -m "feat: ABSURD_DATABASE setting threaded through client/sync/command"
```

---

## Task 2: `AbsurdRouter` + nested multi-DB test suite

Routing is exercised by a **nested suite** (`tests/multidb/`) whose Django settings make
`ABSURD_DATABASE="absurd"` session-global, so pytest-django provisions/resets the
`absurd` test DB normally and the tests assert against provisioned state (no in-test
`migrate`, no DDL, no scrub fixture). The default no-op is proved by a tiny main-suite
test plus the existing 25 tests staying green with the router registered.

**Files:**

- Create: `django_absurd/routers.py`
- Create: `tests/multidb/pytest.toml`, `tests/multidb/__init__.py`,
  `tests/multidb/settings.py`, `tests/multidb/conftest.py`,
  `tests/multidb/test_router.py`
- Create: `tests/test_router_default.py` (main suite — default no-op)
- Modify: `tests/settings.py` (register `AbsurdRouter`); `pyproject.toml`
  (`--ignore=tests/multidb` + `testpaths`)

**Interfaces:**

- Consumes: `get_absurd_database` (Task 1), `Queue`.
- Produces: `AbsurdRouter` with `db_for_read(self, model, **hints)`,
  `db_for_write(self, model, **hints)`,
  `allow_migrate(self, db, app_label, model_name=None, **hints)` — all reading
  `get_absurd_database()`.

- [ ] **Step 1: Scaffold the nested suite + write RED tests**

`tests/multidb/pytest.toml` (TOML lists; spike-validated — rootdir is `tests/multidb/`):

```toml
[pytest]
DJANGO_SETTINGS_MODULE = "tests.multidb.settings"
pythonpath = ["../.."]
testpaths = ["."]
addopts = [
  "--allow-hosts=db,localhost,127.0.0.1",
  "--disable-socket",
  "--reuse-db",
  "--strict-markers",
]
```

`tests/multidb/__init__.py` — empty.

`tests/multidb/settings.py`:

```python
import os

from tests.settings import *  # noqa: F401,F403

ABSURD_DATABASE = "absurd"
DATABASE_ROUTERS = ["django_absurd.routers.AbsurdRouter"]

# DATABASES is COMPLETELY redefined here (not derived from tests.settings): two Postgres
# aliases on the same compose server, each with its own _multidb-affixed TEST.NAME so this
# suite's test DBs never collide with the main suite's (--reuse-db leftovers). The main
# suite migrates django_absurd onto its default test DB; here "default" must stay clean of
# it, which the distinct test DB guarantees. No sqlite (this suite never uses it).
pg = {
    "ENGINE": "django.db.backends.postgresql",
    "USER": os.environ.get("PGUSER", "postgres"),
    "PASSWORD": os.environ.get("PGPASSWORD", "postgres"),
    "HOST": os.environ.get("PGHOST", "localhost"),
    "PORT": os.environ.get("PGPORT", "5432"),
    "NAME": os.environ.get("PGDATABASE", "postgres"),
}
DATABASES = {
    "default": pg | {"TEST": {"NAME": f"test_{pg['NAME']}_multidb"}},
    "absurd": pg | {"TEST": {"NAME": f"test_{pg['NAME']}_multidb_absurd"}},
}
```

(`pg` is lowercase so Django ignores it as a setting — only UPPERCASE names are
settings.)

`tests/multidb/conftest.py` — own autouse fixtures (the parent `tests/conftest.py` is
ABOVE this rootdir and is NOT loaded). Mirror `_reset_absurd_queues` (autouse, calls
`get_absurd_client().list_queues()` + `drop_queue`,
`except (OperationalError, ProgrammingError, ImproperlyConfigured): pass`).
`get_absurd_client()` resolves to the `absurd` alias here via `get_absurd_database()`.

`tests/multidb/test_router.py` (RED — `django_absurd.routers` doesn't exist yet):

```python
import pytest
from django.core.management import call_command
from django.db import connections

from django_absurd.models import Queue
from django_absurd.routers import AbsurdRouter

pytestmark = pytest.mark.django_db(databases=["default", "absurd"])


def absurd_schema_present(alias):
    with connections[alias].cursor() as cur:
        cur.execute("SELECT to_regnamespace('absurd') IS NOT NULL")
        return cur.fetchone()[0]


def test_orm_routes_to_alias():
    # ABSURD_DATABASE="absurd" session-global → router sends Queue to the alias, and the
    # schema was provisioned there by session setup, so the query succeeds.
    assert Queue.objects.db == "absurd"
    assert list(Queue.objects.all()) == []


def test_schema_provisioned_on_alias_not_default():
    # Session setup migrated django_absurd onto "absurd" (allow_migrate True) and NOT onto
    # "default" (allow_migrate False) — the real positive AND negative, no in-test migrate.
    assert absurd_schema_present("absurd") is True
    assert absurd_schema_present("default") is False


def test_allow_migrate_contract():
    router = AbsurdRouter()
    assert router.allow_migrate("absurd", "django_absurd") is True
    assert router.allow_migrate("default", "django_absurd") is False
    assert router.allow_migrate("absurd", "auth") is None


def test_db_for_read_write_route_django_absurd():
    router = AbsurdRouter()
    assert router.db_for_read(Queue) == "absurd"
    assert router.db_for_write(Queue) == "absurd"


def test_sync_command_honors_alias(settings):
    # Scenario 3, positive end-to-end: no --database flag → the command resolves
    # ABSURD_DATABASE="absurd", creates the queue on the absurd DB, and the router reads
    # it back from there. (The autouse reset fixture drops it afterward.)
    settings.ABSURD_QUEUES = {"routed": {}}
    call_command("absurd_sync_queues")
    assert Queue.objects.get(queue_name="routed").queue_name == "routed"
```

`tests/test_router_default.py` (main suite — proves the no-op at the default alias):

```python
from django_absurd.models import Queue
from django_absurd.routers import AbsurdRouter


def test_router_is_noop_at_default():
    # ABSURD_DATABASE defaults to "default" in the main suite → router routes
    # django_absurd to "default" (a no-op).
    router = AbsurdRouter()
    assert router.allow_migrate("default", "django_absurd") is True
    assert router.db_for_read(Queue) == "default"
    assert Queue.objects.db == "default"
```

- [ ] **Step 2: Confirm RED**

Run `docker compose run --rm app pytest tests/multidb -q` → FAIL (no module
`django_absurd.routers`). Run
`docker compose run --rm app pytest tests/test_router_default.py -q` → FAIL (same
import).

- [ ] **Step 3: Implement the router (prose)**

`django_absurd/routers.py`, absolute imports, `AbsurdRouter`:
`db_for_read(self, model, **hints)` / `db_for_write(self, model, **hints)` →
`get_absurd_database()` if `model._meta.app_label == "django_absurd"` else `None`.
`allow_migrate(self, db, app_label, model_name=None, **hints)` →
`db == get_absurd_database()` if `app_label == "django_absurd"` else `None` (signature
matches Django's exactly; `model_name` unused — gating is per-app, so
`RunSQL`/`RunPython`/`CreateModel` in `0001` are gated identically). No
`allow_relation`.

- [ ] **Step 4: Register the router in the main suite + exclude the nested suite**

In `tests/settings.py` add `DATABASE_ROUTERS = ["django_absurd.routers.AbsurdRouter"]`
(no-op at default; do NOT add the `absurd` alias here). In `pyproject.toml`
`[tool.pytest.ini_options]`: append `"--ignore=tests/multidb"` to `addopts` and add
`testpaths = ["tests"]`.

- [ ] **Step 5: Handle the two router ripples in the main suite (FIX with targeted
      markers — never weaken or use a global hook)**

Registering the router in `tests/settings.py` ripples into exactly two existing tests.
Fix each at the call site; do NOT delete/weaken assertions and do NOT add a global
`pytest_collection_modifyitems` marker-injection hook (that hides DB access from readers
and violates the project's explicit-marker convention).

(a) **Migrate guard** — `allow_migrate("sqlite", "django_absurd")` is `False` at
`ABSURD_DATABASE="default"`, so `test_migrate_screams_on_non_postgres_backend` (in
`tests/test_queue_sync.py`) has its `--database sqlite` migrate routed away → the
`require_psycopg` guard never fires. Make the misconfigured alias the routed target so
the guard runs:

```python
@pytest.mark.django_db(databases=["default", "sqlite"], transaction=True)
def test_migrate_screams_on_non_postgres_backend(settings):
    settings.ABSURD_DATABASE = "sqlite"
    with pytest.raises(ImproperlyConfigured):
        call_command("migrate", "django_absurd", database="sqlite", verbosity=0)
```

(b) **makemigrations consistency check** — Django 6 gates the `makemigrations`
consistency check on `DATABASE_ROUTERS` being set, so with the router registered it
queries all configured aliases. `test_no_pending_migrations_for_app` (in
`tests/test_models.py`) then needs DB access to both — add the marker (and
`import pytest` if missing):

```python
@pytest.mark.django_db(databases=["default", "sqlite"])
def test_no_pending_migrations_for_app():
    ...
```

These are the only two existing tests that change; everything else passes unchanged.

- [ ] **Step 6: GREEN**

Run `docker compose run --rm app pytest tests/multidb -q` → pass. Run
`docker compose run --rm app pytest` → pass (main suite, incl. `test_router_default.py`
and the fixed guard test; all other specs 1–2 tests unchanged — router no-op at
`"default"`, nested suite ignored).

- [ ] **Step 7: Commit**

```bash
git add django_absurd/routers.py tests/multidb tests/test_router_default.py tests/settings.py tests/test_queue_sync.py tests/test_models.py pyproject.toml
git commit -m "feat: AbsurdRouter + nested tests/multidb suite (ABSURD_DATABASE=absurd settings)"
```

---

## Task 3: Check `E001` (wrong backend) + `W003` (router missing) + resilience

**Files:**

- Modify: `django_absurd/checks.py`, `tests/conftest.py`
- Test: `tests/test_checks.py` (additions)

**Interfaces:**

- Consumes: `get_absurd_database`, `validate_backend` (Task 1), `BACKEND_ERR`.
- Produces: check emits `Error absurd.E001` (wrong backend) and `Warning absurd.W003`
  (alias set, router missing), in addition to existing `W001`/`W002`; queries the
  `get_absurd_database()` connection. Message constants `E001_MSG = BACKEND_ERR`,
  `W003_MSG`/`W003_HINT`.

> **These tests stay in the MAIN suite** (`tests/test_checks.py`) — they need NO
> `absurd` DB. `E001` drives off the existing `sqlite` alias; `W003` is settings-only
> (the check returns before any DB access, so `ABSURD_DATABASE="absurd"` need not be a
> configured alias). Nothing here belongs in `tests/multidb/`.

- [ ] **Step 1: Write the failing tests (RED)** — add to `tests/test_checks.py`

```python
from django_absurd.checks import W003_MSG  # noqa: add to existing import line


def test_check_errors_on_wrong_backend(settings, capsys):
    settings.ABSURD_QUEUES = {"x": {}}
    settings.ABSURD_DATABASE = "sqlite"
    out = run_absurd_check(capsys)
    assert "absurd.E001" in out
    assert "psycopg" in out


def test_check_warns_when_router_missing(settings, capsys):
    settings.ABSURD_QUEUES = {"x": {}}
    settings.ABSURD_DATABASE = "absurd"
    settings.DATABASE_ROUTERS = []
    out = run_absurd_check(capsys)
    assert "absurd.W003" in out
    assert W003_MSG in out
```

(`test_check_errors_on_wrong_backend` needs `databases=["default", "sqlite"]`; mark the
function accordingly.)

- [ ] **Step 2: Confirm RED**

Run:
`docker compose run --rm app pytest tests/test_checks.py -k "wrong_backend or router_missing" -v`
Expected: FAIL — no `E001`/`W003`; `W003_MSG` missing.

- [ ] **Step 3: Implement (prose)**

In `checks.py`: add `W003_MSG` (e.g.
`"django-absurd: ABSURD_DATABASE is set but AbsurdRouter is not in DATABASE_ROUTERS."`) +
`W003_HINT` (add `"django_absurd.routers.AbsurdRouter"` to `DATABASE_ROUTERS`). In
`check_absurd_queues`, after the empty-declared early return:

1. `alias = get_absurd_database()`; if `alias != "default"` and the router is NOT
   installed → `return [Warning(W003_MSG, hint=W003_HINT, id="absurd.W003")]`
   (settings-only, no DB). Detect installation **tolerant of both forms Django accepts**
   via a small `router_installed() -> bool`: an import-path string
   `== "django_absurd.routers.AbsurdRouter"` OR an `AbsurdRouter` instance in
   `settings.DATABASE_ROUTERS` (docs list strings, but Django also accepts instances).
2. `try: validate_backend(alias)` `except OperationalError: return []`
   `except ImproperlyConfigured: return [Error(BACKEND_ERR, id="absurd.E001")]`.
3. Then the existing query against `alias` (`Queue.objects.using(alias).filter(...)`),
   keeping the `ProgrammingError → W001` and drift `→ W002` branches. (Replace the bare
   `Queue.objects.filter(...)` with `.using(alias)`.) Import `Error` from
   `django.core.checks`.

- [ ] **Step 4: Resilience (prose)** — `tests/conftest.py`

Add `ImproperlyConfigured` (from `django.core.exceptions`) to the `_reset_absurd_queues`
`except` tuple, so a test setting `ABSURD_DATABASE` to a non-PG alias doesn't error in
fixture setup.

- [ ] **Step 5: GREEN**

Run: `docker compose run --rm app pytest tests/test_checks.py -v` → pass (incl. existing
in-sync/drift/schema-absent, now via the alias). Full suite + ruff +
`uvx pre-commit run --all-files` clean.

- [ ] **Step 6: Commit**

```bash
git add django_absurd/checks.py tests/conftest.py tests/test_checks.py
git commit -m "feat: check E001 (wrong backend) + W003 (router missing); honor ABSURD_DATABASE"
```

---

## Self-Review

- **Spec coverage:** setting + `get_absurd_database` (T1 — DONE); `validate_backend`
  factor (T1); client/sync/command honor alias (T1); `AbsurdRouter` +
  db_for_read/write + allow_migrate (T2); default no-op via `test_router_default.py` +
  green main suite with router registered (T2); positive routing + negative
  (provisioned-on-alias-not-default) asserted against session-provisioned state in the
  nested `tests/multidb/` suite — NO in-test migrate (T2); check `E001` + `W003` + alias
  query (T3, main suite); reset-fixture `ImproperlyConfigured` resilience (T3). Covered.
- **Testing approach:** nested `tests/multidb/` suite with own auto-discovered
  `pytest.toml` + `tests.multidb.settings` (`ABSURD_DATABASE="absurd"` session-global,
  `absurd` alias migrated normally); `pytest tests/multidb` runs it, root `pytest`
  ignores it. Spike-validated. No `migrate`/`migrate zero`/`DROP SCHEMA`/scrub fixture.
- **Placeholder scan:** none — tests + config concrete, implementation prose with exact
  names.
- **Type consistency:**
  `get_absurd_database`/`validate_backend`/`get_absurd_client(using=None)`/`sync_queues(using=None)`
  consistent T1↔T2↔T3; `AbsurdRouter` dotted path
  `"django_absurd.routers.AbsurdRouter"` identical in `tests/settings.py` (T2),
  `tests/multidb/settings.py` (T2), and the W003 check (T3);
  `W003_MSG`/`E001`/`BACKEND_ERR` consistent T3↔tests.
