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
- An **autouse `_enable_db(db)` fixture** in `tests/conftest.py` gives every test DB
  access — do NOT decorate tests with `@pytest.mark.django_db`. Only add
  `@pytest.mark.django_db(transaction=True)` (or markers for multi-DB / reset-sequences)
  when a test needs transactions/commits or DDL (`migrate`, `create_queue`).
- **No monkeypatching / `unittest.mock.patch`.** Test observable behavior, not
  internals. If a test needs to patch our own functions to reach a branch, restructure
  so a real input drives that branch instead.
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
  `docker compose up -d db db_pg_cron`.
- Full Python×Django matrix + min-max mypy: `uvx --with tox-uv tox`.
- Each suite runs with `--reuse-db` (addopts); add `--create-db` to rebuild from scratch
  after a migration change. Exception: `--create-db` does NOT work for `tests/pg_cron` —
  the pg_cron launcher holds a persistent session on `cron.database_name`
  (`absurd_test_pg_cron`), so Django cannot drop/recreate that DB; use `--reuse-db` (the
  default) for the pg_cron suite.
- **Comment hygiene:** don't write comments that restate code or justify
  obviously-needed lines — let tests validate necessity. Remove noisy/distracting test
  comments.

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
