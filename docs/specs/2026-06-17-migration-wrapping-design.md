# django-absurd — Spec 1: Package skeleton + initial migration

Date: 2026-06-17 Status: approved-for-planning

## Context

django-absurd = pip-distributable Django app wrapping
[Absurd](https://earendil-works.github.io/absurd/), a Postgres-native durable workflow
engine. Absurd objects live in a dedicated `absurd` Postgres schema, which records its
own version via `absurd.get_schema_version()`.

Whole project = sequential specs, each its own spec→plan→build cycle:

1. **Package skeleton + initial migration** ← THIS spec
2. Model representation (`managed=False` models over `absurd` schema)
3. Bind to Django Tasks API
4. Worker management command

- **Migration maintenance** (codegen, per-release deltas, drift/upstream tests,
  SDK-floor automation) — pulled OUT; intention saved in
  `2026-06-17-migration-maintenance-design.md`.

This spec = get the **initial** migration running. Outcome-focused. Maintenance + later
phases out of scope.

Database: targets `DATABASES['default']` only — no multi-DB routing.

## Source of truth: absurdctl (offline, no network)

`absurdctl` (PyPI, hard-pinned `==` dev dependency) is the single source of the Absurd
SQL, read entirely from the installed wheel — **no network of any kind, ever** (no
GitHub, `urllib`, `requests`). Its wheel is one `absurdctl/__init__.py` with the SQL
baked in:

- `absurdctl.BUNDLED_SCHEMA_SQL` — the full schema (its `get_schema_version()` body
  literal is `'main'`; release automation does not substitute it).
- `absurdctl.ABSURD_SCHEMA_TARGET_VERSION` — the concrete version (e.g. `"0.4.0"`); the
  reliable version string.

`absurdctl` is a **dev/build dependency only** — NEVER a client runtime dep.

## Approach

A single initial migration wraps Absurd's full bundled schema as a `RunSQL` operation:

- `0001_initial_<ver>` SQL = `absurdctl.BUNDLED_SCHEMA_SQL` + an appended concrete
  `get_schema_version()` stamp = `ABSURD_SCHEMA_TARGET_VERSION` (overriding the `'main'`
  body). `<ver>` = that version, dots→underscores.
- Generated once (a throwaway offline extraction), committed as a static `.sql` +
  `RunSQL` module. No shipped codegen in this spec.

Fresh `migrate` installs the schema at the pinned version. Per-release upgrade deltas
are the deferred migration-maintenance spec's job.

**Supported model = greenfield: django-absurd OWNS the `absurd` schema.** Django always
applies `0001` regardless of DB state, and the bundled schema is NOT fully idempotent
(e.g. plain `create function absurd.validate_queue_name`), so applying onto a
pre-existing `absurd` schema errors. Adopting a DB that already has Absurd installed =
manual `migrate --fake django_absurd 0001` first. Documented; tooling out of scope.

## Package structure

Package at repo **root** (no `src/` layout):

```
pyproject.toml
django_absurd/
  __init__.py                   # ABSURD_SCHEMA_VERSION = "<ver>"  (public, single source)
  apps.py                       # AbsurdConfig, label = "django_absurd"
  migrations/
    __init__.py
    0001_initial_<ver>.sql      # generated: bundled schema + concrete stamp
    0001_initial_<ver>.py       # RunSQL; reads the sibling .sql inline
tests/                          # excluded from wheel
docs/                           # excluded from wheel
```

- **No `_version.py`, no `_sql.py`, no underscore-private modules.**
  `ABSURD_SCHEMA_VERSION` lives in `django_absurd/__init__.py`. The migration reads its
  `.sql` inline via `importlib.resources` (no loader module).
- Wheel ships ONLY `django_absurd/**`, incl. `migrations/*.sql` as package data (declare
  in `pyproject.toml`; verify the built wheel contains it). `tests/`, `docs/` excluded.

## Migration module (Django compliance)

`0001_initial_<ver>.py` = standard `migrations.Migration`, `initial = True`,
`dependencies = []`, one `RunSQL` whose `sql` reads the sibling `.sql` inline and
`reverse_sql = "DROP SCHEMA IF EXISTS absurd CASCADE;"` (destructive — teardown only;
`migrate ... zero` drops all Absurd data). The bootstrap full-schema install is
transaction-safe → Django default `atomic = True` (any `concurrently` in the bundled
schema is only in comments/strings/plpgsql bodies, not top-level DDL). RunSQL is
paramless → psycopg3's simple-query protocol runs the multi-statement bundle. No Django
models in this app → no `state_operations`. Absurd is up-only → no real reverse exists
beyond the schema drop.

## Dependencies and version pinning

`ABSURD_SCHEMA_VERSION` (the schema the migration installs) is the anchor.

| Thing                            | Role                                                  | Pin                                        |
| -------------------------------- | ----------------------------------------------------- | ------------------------------------------ |
| schema (`ABSURD_SCHEMA_VERSION`) | what the migration installs                           | the anchor (in `__init__.py`)              |
| `Django`                         | runtime                                               | `>= 5.2` (LTS floor)                       |
| `absurd-sdk`                     | runtime (workers/tasks, later specs — added up front) | `>= ABSURD_SCHEMA_VERSION, < <next-minor>` |
| `absurdctl`                      | dev/build — SQL source                                | `== <ver>` hard pin                        |
| `psycopg`                        | dev only                                              | unpinned, NO `[binary]`                    |

- **`absurd-sdk` is a runtime dependency now** (workers come in a later spec, but the
  dep is declared up front). Floor = `ABSURD_SCHEMA_VERSION` (rises when
  migration-maintenance adds a migration); ceiling = next minor (conservative 0.x
  bound). `absurd-sdk` brings psycopg transitively for worker use.
- **psycopg is NOT forced as a runtime dep** — Django users choose their own driver.
  Listed in dev for our tests, without `[binary]`.

## Determinism

Client `migrate` is fully deterministic: the migration is committed static `.sql` +
`.py` executed verbatim — no absurdctl, no network at apply time. All extraction happens
once at maintainer time against the pinned wheel. The concrete stamp freezes
`get_schema_version()`.

## Tooling / dev env (cherry-picked from lincolnloop django-layout)

`ll:startproject` scaffolds a full web _application_ and refuses a non-empty repo — does
NOT fit a package. So **cherry-pick** the dev infra:

- `compose.yaml` — `db` (Postgres) + a lean python-only `app` service mounting the repo;
  DB config passed via `PG*` env (no `dj-database-url`).
- `docker/app/Dockerfile` — python + `uv` only (no node/tailwind/gunicorn). **No `CMD`**
  — runs are explicit via `docker compose run`.
- pyproject conventions from django-layout: ruff (`select=["ALL"]` + ignores, migrations
  excluded), pytest (`--reuse-db`, `--strict-markers`,
  `pytest-socket --disable-socket --allow-hosts=db,localhost,127.0.0.1`), coverage,
  mypy/django-stubs.
- `.pre-commit-config.yaml` mirrors django-layout — std hooks, yamllint,
  check-github-workflows, ruff, hadolint, pretty-format-toml, uv-sort, mypy,
  renovate-validator — **including prettier** scoped to docs/config (markdown, yaml,
  json), **excluding html**. Drops the README `cog` hook.
- Settings read DB from `PG*` env vars.
- **No Makefile.** Dev + CI run `docker compose run --rm app pytest`.

## pg_cron (optional, NOT in the migration)

Absurd's core schema installs/runs without pg_cron (cron calls guarded by
`if to_regclass('cron.job') is not null`); the bundled schema never runs
`CREATE EXTENSION`. django-absurd does NOT create pg_cron — server-level prerequisites a
migration can't satisfy. Operator concern; opt-in deferred to the worker spec.

## Testing (pytest, function-based, real Postgres via compose — scenario/outcome-focused)

1. Package loads: app registered; `ABSURD_SCHEMA_VERSION` concrete semver.
2. Fresh `migrate` → `absurd` schema exists; `absurd.get_schema_version()` ==
   `ABSURD_SCHEMA_VERSION`; `absurd.queues` table exists.
3. Reverse: `migrate django_absurd zero` drops the `absurd` schema.
4. Built wheel ships `migrations/*.sql`, excludes `tests/`.

No granular unit tests — outcomes only.

## Out of scope

Models, Django Tasks API, worker command, async client, queue/task runtime config,
pre-existing-DB adoption tooling, and **migration maintenance**
(codegen/deltas/drift/upstream — separate spec).

Note: `migrate` installs the schema but creates NO queues. A fresh install has zero
queues; the worker spec must create one (`absurd.create_queue` /
`absurdctl create-queue default`) before workers run.

Deployment prerequisites (inherited from upstream Absurd schema, not introduced here) —
document in eventual install docs alongside the greenfield/`--fake` adoption note:

- The bundled schema runs `create extension if not exists "uuid-ossp"` → `migrate` needs
  a role able to `CREATE EXTENSION` and the contrib extension available (fine on
  `postgres:16-alpine` and most managed Postgres). Distinct from the pg_cron point (the
  schema never creates pg_cron).
- The schema name **`absurd` is hardcoded** in the bundled SQL (`create schema absurd`,
  `absurd.queues`, …) and is NOT relocatable via Django config:
  `DATABASES['default']['NAME']` is the _database_, not a schema; a DB router selects a
  connection/database; `search_path` only sets default lookup — none rewrite the
  `absurd.`-qualified DDL. So Absurd always installs into a schema literally named
  `absurd` in the default database, beside `public` (which holds `django_migrations`,
  auth, and the user's tables).
- Installing therefore needs the default role to have **CREATE on the database** (for
  `create schema absurd`). On locked-down single-schema deployments where the role
  cannot `CREATE SCHEMA`, an admin must pre-create the `absurd` schema and grant the
  role `CREATE`/`USAGE` on it (then `create schema if not exists` no-ops). Making the
  schema configurable would require post-processing the verbatim upstream SQL — rejected
  (breaks the determinism + source-of-truth model).
