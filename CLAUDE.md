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
- **Test fixture tasks read at their call site, not their definition.** The shared ones
  in `tests/tasks.py` / `tests/atasks.py` are always reached module-qualified
  (`tasks.capped`, `tasks.routed` — see Testing conventions), so a terse adjective name
  is fine there: the module supplies the missing noun. A task defined **locally in a
  test module** has no such prefix, so it must carry the verb itself
  (`make_group_on_immediate_backend`, `echo_int`), never a bare property or provenance
  (`off_backend`, `defined_elsewhere`, `plain`, `folded`). When a test binds a resulting
  `Task` _object_ to a local name, prefix it `task_` (`task_with_folded_defaults`) — the
  object is a noun, the function is not.
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

## Exception hierarchy

- django-absurd raises its **own** exception types for its own failure modes, all under
  `DjangoAbsurdError`, defined in `django_absurd/exceptions.py`. Prefer a specific type
  over a bare stdlib/Django one when the condition is specific to this package.
- The type name carries the condition (`QueueNotDeclaredError`,
  `QueueNotProvisionedError`), and **the exception owns its message** — constructors
  take the data, callers never assemble text and no `format_*` helper is imported to
  build one.
- Named for the distributing package, not the upstream SDK: `DjangoAbsurdError`, never
  `AbsurdError`, because modules import from both `absurd_sdk` and `django_absurd` and
  the short name reads as the SDK's.
- Be honest about coverage: `except DjangoAbsurdError` catches the typed errors, not
  every error the package can raise — plain `ImproperlyConfigured`/`RuntimeError`/
  `TypeError` remain in `checks.py`, `connection.py`, `queues.py`, and `test.py`'s
  guards for now.

## Exception chaining

- Re-raising a curated error inside an `except` always chains with `from exc` — never
  `from None`. Add `as exc` to the handler if it doesn't already bind a name.
  `from None` hides the real cause exactly when the curated message turns out to be the
  wrong guess.
- Pair this with narrowing the catch: classify first, re-raise the original untouched
  when the error isn't about what your curated message claims, chain with `from exc`
  when it is. `from exc` is not a licence to relabel broadly — see `names_a_queue_table`
  in `django_absurd/queues.py` for the worked example of both together.

## Testing

Test-authoring conventions live in [`tests/CLAUDE.md`](tests/CLAUDE.md) — read it before
writing or editing any test file. Running the suites:

- Tests run on the HOST via uv/tox (no app container). Three suites, each with its own
  `pytest.toml` and settings; invoke explicitly (a bare `uv run pytest` at repo root
  collects nothing and exits code 5 — intentional):
  - `uv run pytest tests/core` — core django-absurd; `django_absurd.pg_cron` NOT
    installed; plain `db` service (`PGPORT`, default 5432).
  - `uv run pytest tests/pg_cron` — pg_cron app installed; requires the `db_pg_cron`
    service (`PGPORT_PGCRON`, default 5434); an ORDINARY test DB (`test_absurd_pg_cron`)
    with no extension — the central `cron.database_name` on that server is `postgres`, a
    different database entirely, and jobs reach it cross-database.
  - `uv run pytest tests/multidb` — multi-DB router suite; plain `db`.
- Two compose services: `db` (plain `postgres:18`) and `db_pg_cron`
  (`Dockerfile.pg_cron` + `shared_preload_libraries=pg_cron`). Start both:
  `docker compose up -d db db_pg_cron`. **These must be running before any suite.** If a
  connection is refused / `pg_isready` fails, the container is stopped (they don't
  survive a machine restart or a new session) — bring it up FIRST; don't diagnose it as
  anything cleverer.
- **The two gates to run before a commit** — not five separate commands:
  - `uvx --with tox-uv tox -e dev` — all three suites against the dev env only. Reach
    for the bare `uvx --with tox-uv tox` (full Python×Django matrix + min-max mypy) only
    when a change could plausibly break on another version, not while iterating.
  - `uv run pre-commit run --all-files` — owns ruff-check, ruff-format, **mypy**, and
    prettier. Never invoke `ruff` or `mypy` directly; pre-commit already runs them, and
    a hand-rolled invocation drifts from the hook's flags and exclusions.
  - Iterating on one file is still `uv run pytest <path> -v`.
- `pytest-xdist` is a dev dependency; every suite — including `tests/pg_cron`, now that
  its test DB is ordinary and cross-DB — runs safely under `-n<N>` (e.g.
  `uv run pytest tests/pg_cron -n4`). Pass `-n` directly; it isn't baked into any
  suite's `addopts`.
- Each suite runs with `--reuse-db` (addopts); add `--create-db` to rebuild after a
  migration change — including `tests/pg_cron`: its test DB is an ordinary one that
  pg_cron's launcher holds no session on (the launcher only ever connects to the central
  `cron.database_name` database, `postgres`), so `--create-db`'s DROP+CREATE just works,
  same as any other suite. No eviction dance needed. (Per-test isolation is separate and
  automatic: the pytest plugin's auto-cleanup hook
  (`django_absurd.test.install_absurd_cleanup`, wrapping
  `TransactionTestCase._post_teardown`) calls `flush_absurd_state()` after every
  DB-committing test, whose pg_cron branch runs the SCOPED
  `teardown_crons(include_admin=True)` — unschedules django-absurd's own
  settings-and-admin-authored jobs, never touching an unrelated cluster job. Files that
  vary queue/schema topology additionally apply the non-autouse `_isolate_queues`
  fixture (`tests/conftest.py`), which hard-drops the schema before AND after via
  `flush_absurd_state(drop_schema=True)`.)

## Typing is an extra layer, not the contract

- **Type checking is optional for our users.** They may run mypy, pyright, or nothing at
  all. Downstream projects are under no obligation to type-check.
- **We are on mypy specifically**, not by preference: `django-stubs` ships a mypy plugin
  (`plugins = ["mypy_django_plugin.main"]`) that resolves settings and models, and no
  other checker can load it. So our own gate is mypy strict.
- That asymmetry means public API typing should stay **checker-agnostic** where it can —
  plain overloads and explicit signatures rather than mypy-specific behavior — so
  pyright users get the same errors. Verify with `uvx pyright` when designing a typed
  surface.
- So **runtime behavior is the contract.** Every rule we enforce must raise a correct,
  self-explanatory Python error on its own — never rely on a checker having caught it
  first. Annotations, overloads, and `Never`/`NoReturn` tricks are a bonus layer that
  catches mistakes earlier for the users who opt in.
- When a nicer static message and a nicer runtime message conflict, **the runtime
  message wins**. Errors state the rule and show the fix (see the system-check
  convention above: problem in `msg`, resolution in `hint`).
- Don't lie to the checker to buy a tidier static error (e.g. hiding a method behind
  `if not t.TYPE_CHECKING` so it looks absent). Prefer a construction that is true at
  both layers.
- **Validate where it's important; don't go crazy.** Worth validating: configuration
  (`absurd.E009` on `DEFAULT_MAX_ATTEMPTS`), user-authored data that persists (pg_cron
  schedule grammar, model `full_clean`), and anything whose failure is silent or lands
  far from its cause. Not worth it: re-stating the SDK's own types at a call site when a
  wrong value blows up loudly on its own — a wrong signature or wrong type should fail
  the way Python or Postgres fails it, and duplicated policy drifts from the pinned SQL.
- Curated errors are for a **different category**: the caller is at the wrong door, not
  holding the wrong data — a param that belongs on `.using()`, a per-invocation field
  used at a definition site. Python's own message can't point at the right API, so those
  get a message naming the rule and showing the fix. Wrong _data_ gets no such
  treatment.

## Runtime

- Floor: **Django 6.0 / Python 3.12**.
- Requires the **psycopg (v3)** Django backend — the absurd SDK reuses Django's
  connection and needs psycopg3. Validate/assert this where we hand the connection to
  the SDK.
- One `AbsurdBackend` per project (deliberate). A non-default `DATABASES` alias requires
  `django_absurd.routers.AbsurdRouter` in `DATABASE_ROUTERS` (`absurd.E005`).
- No network at migrate time; Absurd SQL comes only from the pinned `absurdctl` wheel
  (dev dep).

## Workflow

- `superpowers:brainstorming` → `writing-plans` →
  `executing-plans`/`subagent-driven-development` on any non-trivial feature or bugfix.
