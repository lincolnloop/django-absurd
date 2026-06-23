# django-absurd — Spec: Configurable Absurd database

Date: 2026-06-19 Status: approved-for-planning

Lets a project run Absurd on a DB connection other than `default` — e.g. `default` is
sqlite and Absurd lives on a separate Postgres alias — via a setting + a DB router.
Default (`"default"`) keeps specs 1–2 behaving exactly as today (single DB, router a
no-op).

## Setting

`ABSURD_DATABASE` — a `DATABASES` alias, default `"default"`. Single source of truth
read by the router, `get_absurd_client`, `sync_queues`, the command, and the system
check. A small reader `get_absurd_database() -> str` returns
`getattr(settings, "ABSURD_DATABASE", "default")`.

## Router (`django_absurd/routers.py`)

`AbsurdRouter` — the developer adds it to `DATABASE_ROUTERS`. **Non-prescriptive: it
routes ONLY the `django_absurd` app; it never opines on other apps.**

- `db_for_read(model, **hints)` / `db_for_write(model, **hints)` →
  `get_absurd_database()` when `model._meta.app_label == "django_absurd"`, else `None`.
- `allow_migrate(db, app_label, model_name=None, **hints)` →
  `db == get_absurd_database()` when `app_label == "django_absurd"`; otherwise `None`
  (no opinion — whether other apps live on the Absurd alias is the developer's choice).
  Signature matches Django's; gating is per-app, so all of `0001`'s ops
  (`RunPython`/`RunSQL`/`CreateModel`, the latter two with `model_name=None`) are gated
  identically.
- `allow_relation` not defined (→ `None`).
- When `ABSURD_DATABASE == "default"`: a no-op — `django_absurd` migrates/reads on
  `default`, others unaffected. Single-DB users (specs 1–2) need no router and see no
  change.

## Honor the alias everywhere

Replace the literal `"default"` defaults with `get_absurd_database()`:

- `get_absurd_client(using=None)` → uses `get_absurd_database()` when `using` is None.
- `sync_queues(using=None)` → same.
- `absurd_sync_queues` command: `--database` default = `get_absurd_database()`.
- `check_absurd_queues`: queries the `get_absurd_database()` connection.

## Migration state is per-DB (no extra work)

`django_migrations` is per-database in Django and is created by the `MigrationRecorder`
on any DB you migrate — it is NOT gated by the router's `allow_migrate`. So
`migrate --database <absurd-alias>` creates `django_migrations` on the Absurd DB and
records `django_absurd`'s `0001` there; migration state lives with the schema it tracks.
Deploy is two steps: `migrate` (default DB) + `migrate --database <alias>` (Absurd DB).
With the non-exclusive router, the second step also creates other apps' tables on the
Absurd DB — the developer's choice.

## System check: `E001` (wrong backend) + alias

The check runs against the `get_absurd_database()` connection. States (when queues
declared):

- **Alias set but router missing** (`ABSURD_DATABASE != "default"` and `AbsurdRouter`
  not in `settings.DATABASE_ROUTERS` — detected tolerant of both the import-path string
  and an `AbsurdRouter` instance, since Django accepts either) →
  `[Warning(..., id="absurd.W003")]` — catches the footgun where the setting is
  configured but routing isn't. Checked first (pure settings inspection, no DB access).
- **Can't connect** (`OperationalError`) → `[]` (silent; transient).
- **Wrong backend** (connected, `conn.connection` not `psycopg.Connection`) →
  `[Error(BACKEND_ERR, id="absurd.E001")]` — surfaces a sqlite/psycopg2
  `ABSURD_DATABASE` at `check`/`runserver`, fixing today's silent-masking gap.
  (`BACKEND_ERR` is the existing psycopg-link message; `E001_MSG = BACKEND_ERR`.)
- **Schema absent** (`ProgrammingError`) → `W001`.
- **Drift** → `W002`.
- **In sync** → `[]`.

Backend validation precedes the queue queries (fail-fast). Factor the connect+psycopg
check into a small helper (e.g. `validate_backend(using)`) shared by `get_absurd_client`
and the check — don't construct an `Absurd` client in the check just to validate.

## Resilience

The autouse `_reset_absurd_queues` test fixture also catches `ImproperlyConfigured` (in
addition to `OperationalError`/`ProgrammingError`) — so a test that sets
`ABSURD_DATABASE` to a non-PG alias doesn't blow up in fixture setup (nothing to reset
on a wrong backend).

## Testing (pytest, function-based, real Postgres via compose)

**Two suites.** Routing is exercised by a separate nested suite `tests/multidb/` with
its own auto-discovered `pytest.toml` (pytest 9 `[pytest]` table) and
`tests/multidb/settings.py` where `ABSURD_DATABASE="absurd"` is the SESSION-GLOBAL
value, the router is registered, and `DATABASES` is redefined as two Postgres aliases
(`default`, `absurd`) — each with a distinct `_multidb`-affixed `TEST.NAME` so its test
DBs never collide with the main suite's `--reuse-db` test DBs. Because the setting is
session-global, pytest-django provisions/resets the `absurd` DB normally
(`allow_migrate` is `True` at setup, so `0001` lands on `absurd`), so the routing tests
assert against already-provisioned state — **NO in-test
`migrate`/`migrate zero`/`DROP SCHEMA`, no `django_migrations` scrub fixture, no state
leak.** Run: `pytest tests/multidb` (root `pytest` excludes it via
`--ignore=tests/multidb` + `testpaths=["tests"]`). The main suite registers
`AbsurdRouter` too (no-op at default).

Multi-DB suite (`tests/multidb/`):

- **Routing to alias:** `Queue.objects.db == "absurd"` and `Queue.objects.all()`
  succeeds (schema present on `absurd`).
- **Provisioned on alias not default:** `absurd` schema present on the `absurd`
  connection, ABSENT on `default` — the real positive AND negative, no in-test migrate.
- **`allow_migrate` contract (direct):** `True` for `("absurd","django_absurd")`,
  `False` for `("default","django_absurd")`, `None` for `("absurd","auth")`
  (non-prescriptive).
- **`db_for_read`/`db_for_write`** return `"absurd"` for `Queue`.
- **Command honors the alias:** `absurd_sync_queues` (no `--database`) creates a queue
  on `absurd` and the ORM reads it back from there.
- **W002 drift on the non-default alias:** locks in that interval parsing runs on
  `connections["absurd"]`, not the bare `default` connection.

Main suite (`tests/`):

- **Default no-op:** `test_router_default.py` (router routes `django_absurd` →
  `"default"`; `Queue.objects.db == "default"`) + all specs-1–2 tests green with the
  router registered.
- **`E001` wrong backend:** `settings.ABSURD_DATABASE = "sqlite"` →
  `call_command("check", "django_absurd")` emits `absurd.E001` (full `BACKEND_ERR`); the
  reset fixture stays green (catches `ImproperlyConfigured`).
- **`W003` router-missing:** `settings.ABSURD_DATABASE = "absurd"` +
  `settings.DATABASE_ROUTERS = []` → check emits `absurd.W003` (settings-only, no DB).
- **Backend scream** at `migrate`/`absurd_sync_queues` (existing guard tests; the
  migrate one sets `settings.ABSURD_DATABASE = "sqlite"` so the router doesn't route it
  away).

Two router ripples in the main suite from registering the router are fixed with targeted
per-test markers (never a global collection hook): the migrate-guard test sets
`ABSURD_DATABASE="sqlite"`, and `test_no_pending_migrations_for_app` gets
`databases=["default","sqlite"]` (Django 6 gates `makemigrations` consistency checks on
`DATABASE_ROUTERS`).

## Out of scope

Auto-running the two-step migrate (developer/deploy concern); managing/creating the
Absurd `DATABASES` entry itself (the developer configures it); cross-DB relations.
