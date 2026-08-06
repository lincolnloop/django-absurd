# django-absurd — test-authoring conventions

How to WRITE tests here. For how to RUN them (suite invocations, compose services, the
pre-commit gates), see [`../CLAUDE.md`](../CLAUDE.md).

- pytest, **function-based only** (never class-based).
- **Non-fixture test helpers live in a `utils.py`** module (never `support.py` or other
  invented names) — e.g. `tests/utils.py`, `tests/core/test_admin/utils.py`,
  `tests/pg_cron/utils.py`. Import the module (`from tests import utils`) and qualify.
- **Same for the fixture task modules**: `from tests import tasks` /
  `from tests import atasks`, then `tasks.add`, `tasks.routed`, `atasks.aecho`. Never
  `from tests.tasks import routed` — a bare adjective at the call site says nothing
  about what runs, and it forces rename-aliases like `make_group as make_group_task`.
- **Shared fixtures live in the parent `tests/conftest.py`**, inherited by all three
  suites via `--confcutdir=..` in each suite's `pytest.toml` (each suite's rootdir is
  its own dir, so without `confcutdir` a parent conftest isn't discovered). Do NOT
  re-import fixtures into a suite conftest — a suite `conftest.py` holds only
  suite-specific fixtures. Per-test pg_cron isolation is not a suite-local fixture; it
  comes from the mechanisms described in [`../CLAUDE.md`](../CLAUDE.md).
- An **autouse `_enable_db(db)` fixture** (in `tests/conftest.py`) gives every test DB
  access — do NOT decorate tests with `@pytest.mark.django_db`. Only add
  `@pytest.mark.django_db(transaction=True)` (or markers for multi-DB / reset-sequences)
  when a test needs transactions/commits or DDL (`migrate`, `create_queue`).
- **Any test that EXECUTES anything — enqueue, drain, a worker, cleanup deleting rows —
  freezes time through the `dj_absurd` fixture**, not through time-machine directly:
  `with dj_absurd.freeze_time() as frozen_time:`, then
  `frozen_time.shift(Δ)`/`move_to(instant)`, enqueueing INSIDE the block — **never
  `time.sleep`**. It moves Postgres and Python together, which is mandatory: Postgres
  ahead of Python is an unkillable deadlock for a sync task. The fixture works unchanged
  in an `async def` test. See
  [Testing — the `dj_absurd` fixture](../docs/web/testing.md#the-dj_absurd-fixture).
- **`time_machine.travel(..., tick=False)` directly is for pure-Python math only** —
  cron arithmetic (`get_next_datetime`) and the like, where no row, worker, or Absurd
  deadline is involved. Reaching for the fixture there would write a database GUC for
  nothing; reaching for time-machine on an executing test leaves Postgres on real time
  (that mistake shipped once — `test_cleanup.py` passed only because `cleanup_ttl` was
  0). The one sanctioned ticking use is `tests/core/test_scheduler.py`'s live worker
  crossing a `*/1` boundary, which needs real time to pass.
- **freezegun is banned** — it patches `time.monotonic`, which IS asyncio's event-loop
  clock, so a frozen freezegun deadlocks the burst drain unkillably. Do not reintroduce
  it. `pytest-asyncio` is a dev dependency for writing `async def` tests; nothing in
  `django_absurd/` may depend on it.
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
  - **Never unit-test an internal helper** (a merge function, a serializer, a builder).
    Assert its behavior through the real objects that use it — construct a `Task`,
    enqueue it, run the command, and check the outcome. A test that calls the helper
    directly is a hollow implementation defence: it re-states the code, survives a wrong
    design, and dies on any refactor. If a helper's behavior has no observable
    expression yet, the test belongs in the later task that adds the surface that
    expresses it.
  - Reuse existing fixtures/utilities rather than re-rolling equivalents; inventory a
    suite's `conftest.py` and a sibling test before writing new ones.
  - **Don't wrap two lines in a helper.** Inline short setup (claiming a task, opening a
    cursor) at each call site rather than hiding it behind an indirection.
  - **A function that is never invoked gets no real body.** When a task or a decorator
    target exists only for its object, signature, decorator, or import path — enqueued
    but never run, or only inspected — a working body is dead code and a coverage miss.
    Applies to `@task` fixtures and to throwaway `def send_report(...)` stubs in guard
    tests alike. Two forms:
    - **`raise NotImplementedError` with a reason** — the default. Write it as the
      two-line errmsg-lint idiom and annotate `-> t.Never`:

      ```python
      def capped(a: int, b: int) -> t.Never:
          msg = "path-resolved for its decorator; never run"
          raise NotImplementedError(msg)
      ```

      `[tool.coverage.report] exclude_also` in `pyproject.toml` carries a regex for
      exactly this shape, so **both** lines are excluded — it costs nothing in coverage
      and still fails loudly if something ever does call it.

    - **A docstring and no body** — fine for a throwaway local stub. Also costs no
      counted lines, but the return annotation must be `-> None` or mypy raises
      `[empty-body]`, and an accidental call silently returns `None`.

    Save real bodies for tasks a worker or the immediate backend actually executes.
    **Check across every suite before concluding a shared fixture is never invoked** —
    `tests/tasks.py` is imported by all of them but each suite is a separate coverage
    run, so a body that looks dead under `tests/core` may be executed by `tests/pg_cron`
    (`capped` and `on_reports` are). Codecov combines the runs; a single local suite
    does not.

  - Name a variable for the thing it holds (its type/role), not a generic placeholder.
- **Test management commands AND system checks by running them**:
  `call_command("check", "django_absurd")` / `call_command("absurd_sync_queues")`,
  capture output with pytest `capsys`, and **assert on the full emitted message text**
  (not on internal return values).
- Drive check/command states with real DB conditions (sync via the command; drop the
  schema; `override_settings` for an unreachable DB) — not mocks.
- HTTP mocking (when ever needed): the `responses` library, not `mock`.
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
- **Narrow `# type: ignore[...]` is expected when a test deliberately passes something
  the checker rejects** — our runtime error states are part of the public contract
  (users may not type-check at all), so they must be exercised. This is the one place
  ignores don't need asking for; keep them narrow (specific error code) and on the
  offending line only. `warn_unused_ignores` (on via `strict`) fails the build if the
  error stops occurring, so a stale ignore can't hide a regressed guard.
- **Always alphabetize** `@pytest.mark.parametrize` values and fixture `params`.
- **Alphabetize a test function's own fixture parameters** too (e.g.
  `def test_x(admin_user: User, client: Client)`, not `client` then `admin_user`) — no
  ruff/flake8-pytest-style rule enforces this (checked; no `PT0xx` rule covers parameter
  order), so it's a manual convention only.

## Fast iteration

Measured on this repo; the point is to spend the slow gate once, not per edit.

- **Iterate with a targeted, coverage-free run:** `uv run pytest <path> -q --no-cov`.
  Every suite's `pytest.toml` turns coverage on via `addopts`, and that instrumentation
  dominates a single-file run; `-q` keeps the output scannable.
- **Run `tox -e dev` once, before the commit** — not after every edit. It is ~2.5
  minutes because it builds three suites; nothing about a one-file change needs that
  loop.
- **`-n4` for a whole-suite run**, which every suite tolerates including
  `tests/pg_cron`. Skip it for a single file, where the worker spin-up costs more than
  it saves.
- **Reach for `--create-db` when failures stop making sense.** A killed frozen test can
  leave a database-level `absurd.fake_now` behind, which makes later durable tests
  unclaimable for reasons invisible in their own code. Rebuild before diagnosing.
- **A test asserting `Created: <queue>` needs `_isolate_queues`** or a queue name unique
  to its file. The catalog row outlives the per-test flush, so the second `--reuse-db`
  run of that file reports nothing created. Passes alone, fails on repeat.
- **A deadlock/duplicate-key storm across unrelated tests means a concurrent run**, not
  a code defect — suites from a worktree reach this checkout's Postgres on 5432. Confirm
  nothing else is running before bisecting.

### When an agent runs the gates

The full `tox -e dev` exceeds a subagent's foreground command limit, so the harness
backgrounds it and the subagent ends its turn reporting "waiting for the run" — a dead
cycle that costs more than the run. Have implementers run only the targeted tests and
`pre-commit`, and let the coordinator own the `tox` gate after the commit. Measured on
this repo, 2026-08-04: the same task shape took ~160s under that split versus 500-900s
when the implementer owned `tox`.
