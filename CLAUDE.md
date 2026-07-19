# django-absurd — project instructions

Django app wrapping [Absurd](https://earendil-works.github.io/absurd/) (Postgres-native
workflow engine). Package at repo root (`django_absurd/`, no `src/`). Specs live in
`docs/specs/`, plans in `docs/plans/`.

This file is about **maintaining** the project — conventions, testing, tooling. For
how-to / integration / usage (configuring the backend, enqueuing, workers, releasing),
see [`django_absurd/AGENTS.md`](django_absurd/AGENTS.md), the user-facing guide; don't
duplicate that material here.

## Naming

- **Functions must contain a verb** (`get_declared_queues`, `sync_queues`,
  `check_absurd_queues`) — never a bare noun (`queue_policies`, `absurd_client`). Avoid
  pointless `_`-prefixed helpers; if a helper exists, give it a real verb-name.
- Exception: autouse pytest fixtures never called directly (e.g. `_enable_db`) may keep
  the `_` + plain-name form.
- **No leading-underscore module constants or helpers** — use plain names
  (`MUTABLE_OPTION_KEYS`, not `_MUTABLE_OPTION_KEYS`).
- **Module layout:** put helper functions BELOW the public function(s) that use them.

## Imports

- **Always `import typing as t`** — never `from typing import X`. Use `t.Any`,
  `t.TYPE_CHECKING`, `t.Sequence`, etc.
- **Absolute imports only** — no relative imports. Enforced by ruff
  (`ban-relative-imports = "all"`).

## Django system-check messages

- `msg` states the PROBLEM only; `hint` states the RESOLUTION. Never duplicate fix text
  in both.

## Testing conventions

- pytest, **function-based only** (never class-based).
- **Non-fixture test helpers live in a `utils.py`** module (never `support.py` or other
  invented names) — e.g. `tests/utils.py`, `tests/core/test_admin/utils.py`,
  `tests/pg_cron/utils.py`. Import the module (`from tests import utils`) and qualify.
- **Shared fixtures live in the parent `tests/conftest.py`**, inherited by all three
  suites via `--confcutdir=..` in each suite's `pytest.toml` (each suite's rootdir is
  its own dir, so without `confcutdir` a parent conftest isn't discovered). Do NOT
  re-import fixtures into a suite conftest — a suite `conftest.py` holds only
  suite-specific fixtures (e.g. pg_cron's `_clear_owned_pg_cron_jobs`).
- An **autouse `_enable_db(db)` fixture** (in `tests/conftest.py`) gives every test DB
  access — do NOT decorate tests with `@pytest.mark.django_db`. Only add
  `@pytest.mark.django_db(transaction=True)` (or markers for multi-DB / reset-sequences)
  when a test needs transactions/commits or DDL (`migrate`, `create_queue`).
- **No monkeypatching / `unittest.mock.patch`.** Test observable behavior, not
  internals. If a test needs to patch our own functions to reach a branch, restructure
  so a real input drives that branch instead.
- **Test at a high, behavioral level — through real entrypoints, never helper units.**
  - **Admin features are HTTP-tested**: drive the real request cycle (log in, then
    `client.get`/`post` the admin URLs) and assert observable side effects, not by
    calling admin/helper methods directly.
  - **Side effects belong on `.save()`/`.delete()` signals so they fire centrally** for
    the ORM save/delete paths (admin, direct ORM) — don't expose a standalone emitter
    for callers or tests to invoke. Exercise the effect through the write path and
    assert the outcome; don't unit-test the emitter in isolation. (Caveat:
    `QuerySet.update()` / `bulk_*` send no signals — call that out where it matters.)
  - Reuse existing fixtures/utilities rather than re-rolling equivalents; inventory a
    suite's `conftest.py` and a sibling test before writing new ones.
  - Name a variable for the thing it holds (its type/role), not a generic placeholder.
- **Test management commands AND system checks by running them**:
  `call_command("check", "django_absurd")` / `call_command("absurd_sync_queues")`,
  capture output with pytest `capsys`, and **assert on the full emitted message text**
  (not on internal return values).
- Drive check/command states with real DB conditions (sync via the command; drop the
  schema; `override_settings` for an unreachable DB) — not mocks.
- HTTP mocking (when ever needed): the `responses` library, not `mock`.
- Tests run on the HOST via uv/tox (no app container). Three suites, each with its own
  `pytest.toml` and settings; invoke explicitly (a bare `uv run pytest` at repo root
  collects nothing and exits code 5 — intentional):
  - `uv run pytest tests/core` — core django-absurd; `django_absurd.pg_cron` NOT
    installed; plain `db` service (`PGPORT`, default 5432; `.envrc` reserves 5433).
  - `uv run pytest tests/pg_cron` — pg_cron app installed; requires the `db_pg_cron`
    service (`PGPORT_PGCRON`, default 5434); test DB `absurd_test_pg_cron` matches
    `cron.database_name`.
  - `uv run pytest tests/multidb` — multi-DB router suite; plain `db`.
- Two compose services: `db` (plain `postgres:18`) and `db_pg_cron`
  (`Dockerfile.pg_cron` + `shared_preload_libraries=pg_cron`). Start both:
  `docker compose up -d db db_pg_cron`. **These must be running before any suite.** If a
  connection is refused / `pg_isready` fails, the container is stopped (they don't
  survive a machine restart or a new session) — bring it up FIRST; don't diagnose it as
  anything cleverer.
- Full Python×Django matrix + min-max mypy: `uvx --with tox-uv tox`.
- Each suite runs with `--reuse-db` (addopts); add `--create-db` to rebuild after a
  migration change. For `tests/pg_cron`, `--create-db`'s DROP is blocked because
  pg_cron's launcher (pointed at `cron.database_name` = `absurd_test_pg_cron`) holds a
  session on it and reconnects on startup (a plain restart just lets it re-grab the DB).
  Don't drop the DB or wipe volumes — instead **block new connections + terminate the
  existing one** so pg_cron can't re-grab during the drop window, then run `--create-db`
  (its own DROP+CREATE succeeds; pg_cron reconnects to the fresh DB):
  ```
  docker exec django-absurd-db_pg_cron-1 psql -U postgres \
    -c "ALTER DATABASE absurd_test_pg_cron WITH ALLOW_CONNECTIONS false" \
    -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='absurd_test_pg_cron' AND pid <> pg_backend_pid()"
  uv run pytest tests/pg_cron --create-db
  ```
  (Per-test isolation is separate and automatic: the autouse `_clear_owned_pg_cron_jobs`
  fixture unschedules every `absurd:%` job after each test.)
- **Comment hygiene:** don't write comments that restate code or justify
  obviously-needed lines — let tests validate necessity. Remove noisy/distracting test
  comments.
- **Multi-entrypoint rule tests (validators):** one case table per rule, **parametrized
  over the real enforcing entrypoints** (`validate_<source>` subjects, e.g. the system
  check + `full_clean`), integration-style — never re-assert the same rule per
  entrypoint. Validators are pure functions raising `ValidationError`, enforced
  **model-first** (on the model + reused by the checks); a plain `VALID` baseline dict
  so a single override isolates one rule.
- **Assert the COMPLETE error message, never a fragment** (fragments are unreadable and
  brittle); assert the full stable portion up to any volatile tail.
- **Always alphabetize** `@pytest.mark.parametrize` values and fixture `params`.

## Runtime

- Floor: **Django 6.0 / Python 3.12**.
- Requires the **psycopg (v3)** Django backend — the absurd SDK reuses Django's
  connection and needs psycopg3. Validate/assert this where we hand the connection to
  the SDK.
- Targets `DATABASES['default']` only (no multi-DB routing yet).
- No network at migrate time; Absurd SQL comes only from the pinned `absurdctl` wheel
  (dev dep).

## Tooling available here

- **superpowers** skills drive the workflow: `brainstorming` (design dialogue) →
  `writing-plans` → `executing-plans`/`subagent-driven-development`, plus
  `test-driven-development`, `systematic-debugging`, and
  `requesting-`/`receiving-code-review`. Reach for them on any non-trivial feature or
  bugfix — design before code, plan before building.
- **revdiff** — TUI inline diff/file review (`/revdiff`, `/revdiff <ref>`,
  `/revdiff <file>`). Use to get human annotations on a diff or doc.
- **caveman** — compressed response mode; toggle with `/caveman` (levels `lite`/`full`/
  `ultra`), `stop caveman` to exit. Keeps full technical accuracy while cutting tokens;
  code, commits, and security text are always written normally.
- **`/dream`** — distills the project's `docs/specs` + `docs/plans` into `docs/WHY.md`
  (the durable "why") and retires consumed docs into `docs/HISTORY.md`. Run it when the
  specs/plans accumulate.
