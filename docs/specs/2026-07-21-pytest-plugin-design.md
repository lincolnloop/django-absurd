# Distributable Absurd pytest fixtures — Design

**Goal:** ship django-absurd's own test-isolation logic as a public, installable pytest
plugin (issue #53), dogfooded via a brand-new, isolated test suite in `examples/web` —
proving the plugin genuinely works as an external consumer would experience it, not just
inside this dev repo's own scaffolding. Gates on, and cross-references, the
already-shipped `SYNC_SCHEDULES_ON_MIGRATE`/`SYNC_SCHEDULES_ON_TEST_DB` safety net.

Partially addresses #53 (this increment; see Deferred below for what #53 asked for but
isn't built yet).

> **Shipped mechanism (this design's fixture/marker was superseded).** The isolation
> logic ships as an automatic monkeypatch of
> `django.test.TransactionTestCase._post_teardown`
> (`django_absurd.test.install_absurd_cleanup`, wired by the `pytest11` plugin's
> `pytest_configure`): truncate-only cleanup after every DB-committing test, with **no
> fixture to request and no marker**. The `absurd_db` fixture +
> `@pytest.mark.absurd_db(drop_schema=…)` marker this design proposes were built, then
> replaced during review — a shipped autouse fixture's teardown runs _after_
> pytest-django re-blocks the DB, a hard error; the `_post_teardown` hook instead runs
> inside pytest-django's db-unblock window. The **durable rationale below carries over
> unchanged** (Django's own flush never reaches Absurd's raw-DDL per-queue tables;
> consumer _truncate_ vs internal _drop_; the `examples/web` external-consumer dogfood).
> For the shipped surface see `django_absurd/test.py`, `django_absurd/AGENTS.md`
> (Testing), and `docs/web/testing.md`; the non-pytest `manage.py test` path is deferred
> ([#96](https://github.com/lincolnloop/django-absurd/issues/96)).

## Why one shared flush function, mode-selected via a marker

This project's own `tests/conftest.py`/`tests/pg_cron/conftest.py` reset by **dropping**
queue tables and unscheduling pg_cron jobs, **before** each test (autouse) — because
this repo's own suites do "funky," framework-testing things (defining queues on the fly,
exercising undeclared-queue rejection, etc.) that a normal consumer's test suite never
does. A consumer's queues are typically declared once in settings and synced ahead of
the suite (`absurd_sync_queues`, matching this project's own suites) — they don't need
their queue _schema_ dropped, only the _rows_ a test wrote flushed back to empty,
matching the ergonomics of Django's own `db`/`transactional_db` fixtures (structure
persists, data resets). (Confirmed by direct experiment: a task enqueued under plain
`pytest.mark.django_db(transaction=True)`, with zero absurd-specific cleanup, is still
present — `Task.objects.count() == 1` — in the next test in the same session. Django's
own flush only knows about tables in its migration/app registry; Absurd's per-queue
tables are raw SQL DDL outside that registry, so Django's flush never reaches them.)

Rather than maintaining the internal drop-based reset as a fully separate, parallel
implementation from the shipped truncate-based one, this project **dogfoods its own
shipped fixture**: one shared function,
`flush_absurd_state(*, drop_schema: bool = False)`, does the queue iteration and the
pg_cron-cleanup branch exactly once; only the per-queue operation is mode-selected —
`client.drop_queue(name)` (schema-level, already clears everything, this repo's own
internal need) when `drop_schema=True`, or the raw `TRUNCATE ... CASCADE` (data-level
only) when `drop_schema=False` (the shipped default). Never both against the same queue
— `drop_queue` already clears the rows a `TRUNCATE` would also clear, so running both
would be pure waste, not extra safety.

**Mode selection is a pytest marker, not an ini option or a fixture override.**
`@pytest.mark.absurd_db(drop_schema=True)` mirrors `pytest-django`'s own established
`@pytest.mark.django_db(transaction=True)` shape exactly — a marker with the same name
as the fixture it configures, read inside the fixture via
`request.node.get_closest_marker("absurd_db")`, registered (so `--strict-markers`
doesn't reject it in any consuming project) via a `pytest_configure` hook adding it to
recognized markers. This is public API, same as `django_db`'s own marker is — a consumer
who applies it gets a real schema drop with no automatic re-sync, and the Testing docs
(below) must say so plainly. This repo's own suites don't apply the marker broadly, or
to every test — only a small number of **dedicated proof tests** (see Scope, Dogfood
target 2) apply `pytestmark = pytest.mark.absurd_db(drop_schema=True)` at module level,
specifically to prove the marker/fixture combination itself works. The suite-wide
autouse pre-test reset (crash-guard, described next) is a **separate, unrelated**
mechanism — it does not use `absurd_db` at all, so there's no double-flush or
mode-mismatch to reconcile between the two.

This project's own suites additionally need **pre-test** timing (autouse, guarding
against a crashed prior process under `--reuse-db` never having torn down) — a property
`absurd_db` itself doesn't have (it's post-test, opt-in, by design, matching a
consumer's expectations). `tests/conftest.py`/`tests/pg_cron/conftest.py` keep a thin,
internal-only autouse fixture that calls `flush_absurd_state(drop_schema=True)`
**directly** (not through `absurd_db` at all — this is plain-function reuse, not fixture
reuse) — sharing the exact same underlying function `absurd_db` uses, just at a
different point in the test lifecycle. This is the primary hygiene mechanism for these
suites, unchanged in effect from today's behavior; the dedicated proof tests (marker +
fixture) are a separate, additional, narrow proof that the shipped public surface itself
works, not a replacement for the autouse wrapper.

**The pg_cron side of the flush is also mode-scoped, for a real safety reason found
during review**: Absurd's own schema supports per-queue maintenance cron jobs
(partition/cleanup/detach, installed via `absurd.enable_cron`/`absurdctl cron --enable`
— entirely separate from django-absurd's own `OPTIONS["CLEANUP"]` job) and
`cron.job_run_details` is pg_cron's own **global**, cluster-wide run-history audit table
— not scoped to django-absurd's own jobs, and not scoped to any one database's
"application-level" concerns. A blanket `select cron.unschedule(jobid) from cron.job` or
`TRUNCATE cron.job_run_details` is correct and safe for `drop_schema=True` ("the test DB
is ours" — matches this project's own existing `tests/pg_cron/conftest.py` precedent
exactly), but wrong for `drop_schema=False`: it would silently strip an operator's
`absurdctl`-enabled maintenance jobs, or another pg_cron consumer's run history, from
what's supposed to be a narrow, safe-for-a-shared-database test flush. So
`drop_schema=False` instead calls the existing, already-tested
`teardown_crons(include_admin=True)` (`django_absurd/pg_cron/reconcile.py`) — scoped to
django-absurd's own `_dj:%`-namespaced jobs plus its own `absurd_cleanup_all` job — and
never touches `cron.job_run_details` at all in that mode (an audit-log table, out of
scope for a safe, non-destructive flush of django-absurd's own data).

**Schema-absent databases stay a harmless no-op, not an error**, matching the existing
`tests/conftest.py::_reset_absurd_queues` precedent exactly: `flush_absurd_state`
catches `(OperationalError, ProgrammingError, ImproperlyConfigured)` around both the
queue step and the pg_cron step, so a consumer whose Absurd schema (or
`django_absurd_pg_cron`'s own tables) isn't migrated yet sees `absurd_db` do nothing,
not raise.

**`absurd_flush` (the existing destructive management command,
`django_absurd/management/commands/absurd_flush.py`) already ships the identical
drop-all-queues loop** (`list_queues()` + `client.drop_queue(name)` per queue) as a
separate, parallel implementation. It becomes a thin wrapper — confirmation prompt +
stdout messaging stay in the command, but the actual drop loop delegates to
`flush_absurd_state(drop_schema=True)` — matching this project's established "thin
command over a shared plain function" pattern (`absurd_sync_queues`/`provision_backend`,
`absurd_sync_crons`/`sync_crons`+`sync_admin_crons`) and genuinely eliminating the
parallel implementation, not just relocating it.

## Scope — this increment

IN:

- A `pytest11` entry point (new, none exists today) registering `django_absurd`'s
  fixtures automatically once the package is installed — no explicit import/conftest
  wiring needed by a consumer, matching how `pytest-django` registers `db`.
- **`absurd_db`** — the one shipped fixture. **Teardown-only, opt-in** (not autouse),
  **independent of `db`/`transactional_db`** (a test requests both explicitly — this
  fixture only flushes, it doesn't grant DB access, mirroring pytest-django's own small,
  composable-fixture philosophy) and **DB-mode-agnostic** (its flush runs regardless of
  whether the test used savepoint rollback or `transaction=True`; harmless
  double-cleanup under rollback, since there's nothing left to flush there anyway).
  After the test, calls `flush_absurd_state()` with `drop_schema` read from the
  `@pytest.mark.absurd_db(drop_schema=...)` marker (default `False` if the marker is
  absent or the kwarg is omitted) — see "Why one shared flush function" above for the
  full mode mechanism:
  - For every queue name in `client.list_queues()`: `drop_schema=True` calls
    `client.drop_queue(name)` (schema-level); `drop_schema=False` (the shipped default)
    runs one identifier-quoted
    `TRUNCATE absurd.t_<queue>, absurd.r_<queue>, absurd.c_<queue>, absurd.e_<queue>, absurd.w_<queue>, absurd.i_<queue> CASCADE`
    (the six per-queue tables `drop_queue` itself targets, per the vendored SQL —
    `t_`/`r_`/`c_`/`e_`/`w_`/`i_` prefix + queue name) — data-only, queue _schema_ (the
    `absurd.queues` catalog row, per-queue policy) untouched. A consumer whose queues
    aren't yet synced, or whose Absurd schema isn't migrated yet, sees this no-op
    harmlessly (nothing to truncate/drop, or a caught schema-absent error), not a raised
    exception.
  - If `django_absurd.pg_cron` is installed (`apps.is_installed(PG_CRON_APP_NAME)`):
    `drop_schema=True` does the blanket `select cron.unschedule(jobid) from cron.job`
    (reusing the exact pattern already in
    `tests/pg_cron/conftest.py::_clear_pg_cron_jobs`) plus `TRUNCATE` the
    `ScheduledTask` table and `TRUNCATE cron.job_run_details` — matching "the test DB is
    ours" reasoning. `drop_schema=False` instead calls the existing
    `teardown_crons(include_admin=True)` (`django_absurd/pg_cron/reconcile.py`) — scoped
    to django-absurd's own `_dj:%` jobs and its `absurd_cleanup_all` cleanup job — and
    never touches `cron.job_run_details` (pg_cron's own global, cluster-wide audit
    table; out of scope for a safe-for-a-shared-database flush). Both modes catch
    `(OperationalError, ProgrammingError, ImproperlyConfigured)` for an
    unmigrated/absent `django_absurd_pg_cron` schema, matching the queue-side no-op
    behavior.

    **Interacts with the already-shipped `SYNC_SCHEDULES_ON_MIGRATE`/
    `SYNC_SCHEDULES_ON_TEST_DB` gate**: that gate defaults `SYNC_SCHEDULES_ON_TEST_DB`
    to `False`, so a consumer's settings-declared `SCHEDULE` is **not** synced into
    pg_cron during a test-database `migrate` unless they opt in. `absurd_db`'s flush
    doesn't care how a `ScheduledTask` row/`cron.job` entry got there — settings-synced
    (only if the consumer opted in), admin-authored, or directly created by the test
    itself — it clears whatever is present (within its mode's scope, per above). The
    Testing docs (below) must say this explicitly: a consumer who wants a `SCHEDULE`
    entry to land in pg_cron for real during a test needs
    `OPTIONS["SYNC_SCHEDULES_ON_TEST_DB"] = True` (or an explicit
    `call_command("absurd_sync_crons")`) — that's a separate decision from whether
    `absurd_db` cleans up afterward.
- **`absurd_drain_queue`** — yields a callable
  `drain(queue: str = "default", *, concurrency: int = 1) -> None`. Backed by a new,
  **internal** (not documented as stable public API — it's plumbing shared between this
  fixture and the `absurd_worker` command, not a consumer-facing surface itself)
  function
  `run_burst_worker(queue: str = "default", *, concurrency: int = 1, claim_timeout: int = 120, poll_interval: float = 0.25, batch_size: int | None = None, worker_id: str | None = None) -> SyncResult`
  in `django_absurd/worker.py`. **Not** named `drain_queue` — that name is already taken
  by the existing low-level async burst-claim loop
  (`async def drain_queue(client, *, concurrency, claim_timeout, batch_size, worker_id) -> int`,
  called from `arun_worker` when `burst=True`); the new function is a different,
  higher-level thing (resolve the backend, validate the queue is declared, provision,
  then run the existing burst worker) and needs a name that doesn't collide with it.
  There's also no isolated "burst-only" section of `absurd_worker`'s `handle()` to lift
  out as-is: `resolve_backend()`, the `--burst`/`--beat` mutual-exclusion check, the
  beat-under-pg_cron check, the queue-declared validation, `provision_backend`, and the
  "Started worker..." message are all shared across burst/blocking/beat today — only
  `run_worker(..., burst=options["burst"], ...)` itself branches internally.
  `run_burst_worker()` wraps the queue-declared validation, `provision_backend`, and
  `run_worker(backend, queue, burst=True, options=WorkerOptions(...))`, and **returns
  the `SyncResult`** from provisioning so its caller can still report it —
  `Command.handle()`'s `--burst` branch becomes a thin wrapper: call
  `run_burst_worker(...)`, translate a raised error to `CommandError`, then
  `self.report_sync_result(result)` and the unchanged `"Started worker on queue '...'."`
  message — preserving the exact stdout contract existing tests already assert (e.g.
  `tests/core/test_worker.py`'s
  `"Reconciled: default\nStarted worker on queue 'default'.\n"` assertions). The shared
  `--burst`/`--beat` and beat-under-pg_cron checks, plus the non-burst/blocking/beat
  path, are genuinely unaffected — they stay exactly where they are today, ahead of the
  burst/non-burst branch. This is the same "thin command wrapping an already-plain
  function" shape `absurd_sync_queues`/`absurd_sync_crons` already use.
- Both fixtures are **opt-in, not autouse** — available the moment the plugin is
  installed, but inert until a test actually requests one by name.
- **Dogfood target 1: `examples/web`.** A brand-new, fully isolated pytest suite (own
  `pytest.toml`, own Django settings module, own coverage config) proving the plugin
  works purely through the `pytest11` entry point — no reliance on this dev repo's own
  `tests/conftest.py` (naturally satisfied: `examples/` is a sibling of `tests/`, not a
  descendant, so pytest's ancestor-only conftest walk-up never reaches it regardless of
  `confcutdir`). At minimum: a test that enqueues `add`, drains via
  `absurd_drain_queue`, asserts the result, and uses `absurd_db` (no marker applied —
  the real out-of-the-box consumer default, `drop_schema=False`) to prove the next test
  starts clean — real proof the fixtures work end-to-end for an external-feeling
  consumer. This is where the `drop_schema=False` (truncate / `teardown_crons`) branch
  gets its dedicated test coverage.
- **Dogfood target 2: dedicated proof tests in this repo's own `tests/core`/
  `tests/pg_cron` suites.** A small number of tests (not a suite-wide default) apply
  `pytestmark = pytest.mark.absurd_db(drop_schema=True)` and explicitly request
  `absurd_db`/`absurd_drain_queue` — proving the marker-driven `drop_schema=True` branch
  and the shipped fixtures themselves work, independent of and in addition to the
  suite's own pre-existing autouse hygiene (see "Why one shared flush function" above —
  the autouse wrapper calls `flush_absurd_state` directly, not through `absurd_db`, so
  there's no double-flush or mode-mismatch between the two). This is where the
  `drop_schema=True` branch gets its dedicated test coverage.
- **`absurd_flush` delegates to the shared function.**
  `django_absurd/management/commands/absurd_flush.py`'s existing drop-all-queues loop is
  replaced with a call to `flush_absurd_state(drop_schema=True)` — confirmation prompt
  and stdout messaging stay in the command, only the drop loop itself is shared (see
  "Why one shared flush function" above).
- **Docs**: a short "Testing" section (AGENTS.md + docs/web) documenting `absurd_db` +
  `absurd_drain_queue`, including the `@pytest.mark.absurd_db(drop_schema=True)` marker
  and its consequence (schema drop, no automatic re-sync) — the same audience/mirroring
  convention already established for every other public surface in this project. Must
  include the `SYNC_SCHEDULES_ON_TEST_DB` note above for `absurd_db`'s pg_cron branch.
- **Coverage & CI for `examples/web`.** `examples/` is already a fully separate uv
  project (own `pyproject.toml` — already editable-installs `django-absurd` from the
  local checkout — own `.venv`, own Dockerfile) with zero coverage/CI wiring to the root
  repo today. Rather than fight tox's single-root-project assumption (folding a separate
  uv project into the tox env matrix needs `package = skip`, duplicated deps,
  `changedir` tricks), this ships as a **dedicated GitHub Actions job** (not a tox env):
  `cd examples/web && uv sync && uv run pytest --cov`, using the plain `db` Postgres
  service CI already starts, with its own Codecov flag/upload — so the plugin module's
  patch coverage genuinely gates PRs, not just local `--cov-report=term` feedback.
  **Runs on the GHA runner (host) via `uv run`, exactly like the existing `tox` job** —
  only Postgres comes from `docker compose up -d db`; pytest itself never runs inside
  `examples/web`'s own app `Dockerfile`/`compose.yaml` (that pair is the runtime demo,
  unrelated to this test job). This keeps `coverage.xml` paths host-relative, matching
  what Codecov already expects from the other three suites — running pytest inside the
  app container instead would emit container-relative paths (e.g. `/app/...`) that
  Codecov can't match against the checkout.
- **Known, open question, deliberately deferred to implementation**:
  `django_absurd/pytest_plugin.py` is imported by pytest's own plugin bootstrap
  (`pytest11` entry point) before pytest-cov starts tracking coverage, so its top-level
  `def`/ `@pytest.fixture`/`pytest_configure` lines may show as permanently uncovered in
  every suite's report even though the function _bodies_ get real coverage whenever a
  test actually uses the fixtures. This needs empirical validation during implementation
  (run the suites, look at the actual coverage report) before deciding whether a
  targeted `pragma: no cover` on the affected signature/decorator lines is warranted —
  no pragma is added preemptively.

OUT (this increment — deferred, tracked as follow-up, not built now):

- **`absurd_live_worker`** — a background-thread, continuously-polling worker fixture
  for integration-style tests. Real, feasible (sketched: a thread running its own
  `asyncio.run(arun_worker(..., burst=False))`, torn down via `run_coroutine_threadsafe`
  calling `client.stop_worker()`), but meaningfully heavier (thread lifecycle,
  `poll_interval` tuning, polling-style assertions instead of deterministic burst
  semantics) — its own follow-up.
- **`AbsurdTestMixin`** — the unittest/`TestCase` counterpart, doing the same flush
  automatically in `tearDown` (inheriting it is the opt-in signal, unlike the fixture).
  Same shape as `absurd_db`, just class-based. Deferred so this increment stays
  pytest-only and reviewable on its own.
- **The default-on "block Absurd operations unless a fixture was requested" guard** —
  matching `pytest-django`'s DB-access block exactly (hooked at
  `django_absurd.connection.build_absurd_client`/`validate_backend`, the one chokepoint
  every Absurd operation shares — confirmed via grep that `django_absurd/pg_cron/*.py`
  never touches it, so this guard would cover Absurd queue operations only, not raw
  pg_cron `cron.job` access; accepted scope boundary, not a gap to engineer around,
  since the two subsystems are architecturally independent). Real, wanted, explicitly
  deferred to its own increment given it's a default-on breaking change for any existing
  downstream test suite the moment it ships — deserves its own focused implementation +
  review, not bundled into the first fixture release.
- **"Tick"-like fine-grained worker stepping** — ruled out earlier in discussion; the
  `absurd_live_worker`/`absurd_drain_queue` split already covers the two real testing
  modes (deterministic burst vs. live background) without needing a third, finer-grained
  primitive.
- **Time control** (#53's extended-scope ask — advancing the DB clock / host clock past
  `sleep_for`/`await_event` timeouts so durable-workflow tests don't need real
  `time.sleep`) — real, wanted, its own increment. Genuinely separate concern from the
  flush/drain fixtures here (it's about controlling Absurd's notion of "now," not
  tearing down state), and deserves its own design given the durable-sleep/Events work
  already has its own pinned-timing test conventions to reconcile with.
- **A documented, consumer-facing utility for syncing schedules into pg_cron from a
  test.** `sync_crons`/`sync_admin_crons` (`django_absurd/pg_cron/reconcile.py`) already
  exist and already work for this — a consumer can already reach the same effect via
  `call_command("absurd_sync_crons")`, which is already documented. No new API surface
  needed; not part of this increment's scope.

## Module layout

- `django_absurd/pytest_plugin.py` — new module: the `absurd_db` and
  `absurd_drain_queue` fixtures, the shared
  `flush_absurd_state(*, drop_schema: bool = False)` function `absurd_db` delegates to
  (module-level, below the fixture per this project's layout convention — helpers below
  the public thing that uses them), and a `pytest_configure` hook registering the
  `absurd_db(drop_schema=False)` marker via `config.addinivalue_line("markers", ...)`.
  Registered via `[project.entry-points.pytest11]` in `pyproject.toml` (new section).
  **Must be import-safe before `django.setup()`**: a pytest11 plugin is imported at
  pytest startup, before pytest-django configures Django, for _every_ pytest run in
  _any_ project with django-absurd installed — Django project or not. A top-level
  `from django_absurd.queues import get_absurd_client` would chain into
  `django_absurd.models` (a model class definition), which raises `AppRegistryNotReady`
  if Django settings are configured but apps aren't populated yet, or
  `ImproperlyConfigured` if settings aren't configured at all (the more likely case for
  "not a Django project") — either way, a top-level import breaks every consumer's
  pytest invocation outright. All `django`/`django_absurd` imports go inside the fixture
  bodies. (`django_absurd/events.py` is the existing precedent for deferring
  app-registry/model-touching imports specifically — not a blanket "defer every Django
  import" rule; module-safe imports like `django.db.transaction` stay at its top level.
  This plugin module takes the stricter "everything inside the body" rule since none of
  its imports need to be module-level.)
- `django_absurd/worker.py` — add
  `run_burst_worker(queue: str = "default", *, concurrency: int = 1, claim_timeout: int = 120, poll_interval: float = 0.25, batch_size: int | None = None, worker_id: str | None = None) -> SyncResult`
  (see Scope above for exactly what it wraps and why it's not named `drain_queue`).
  Internal — shared by `Command.handle()` and the `absurd_drain_queue` fixture, not
  documented as its own stable API.
- `django_absurd/management/commands/absurd_worker.py` — `Command.handle()`'s `--burst`
  branch becomes a thin wrapper: call `run_burst_worker(...)`, translate a raised error
  into `CommandError`, then `report_sync_result` + the unchanged "Started worker..."
  message. The shared validation (queue-declared, `--burst`/`--beat` exclusivity,
  beat-under-pg_cron) and the non-burst/beat path are genuinely unaffected.
- `django_absurd/management/commands/absurd_flush.py` — the existing drop-all-queues
  loop is replaced with a call to `flush_absurd_state(drop_schema=True)`; confirmation
  prompt and stdout messaging are unaffected.
- `django_absurd/pg_cron/reconcile.py` — no changes; `teardown_crons` is reused as-is by
  `flush_absurd_state`'s `drop_schema=False` pg_cron branch.
- `tests/conftest.py`/`tests/pg_cron/conftest.py` — the existing autouse pre-test reset
  fixture(s) call `django_absurd.pytest_plugin.flush_absurd_state(drop_schema=True)`
  directly (unchanged pre-test timing and effect — this is a plain-function call, not a
  request for the `absurd_db` fixture). A small number of new, dedicated tests are added
  that apply `pytestmark = pytest.mark.absurd_db(drop_schema=True)` and explicitly
  request `absurd_db`/`absurd_drain_queue` — proving the shipped fixtures/marker
  themselves work inside this repo's own suites too (see Scope, Dogfood target 2).
- `examples/web/tests/` — new package: `test_add.py` (or similar) using the distributed
  fixtures; `examples/web/tests/settings.py` (plain Django settings module, reusing the
  same `PGDATABASE`/`PGUSER`/etc. env vars `app.py`'s inline `Django(...)` config
  already uses — decoupled from nanodjango's single-file wrapper so
  `DJANGO_SETTINGS_MODULE` points at something pytest-django can load directly);
  `examples/web/pytest.toml` (own `DJANGO_SETTINGS_MODULE`, `testpaths`, coverage
  addopts, mirroring `tests/core/pytest.toml`'s shape minus `--confcutdir=..` and minus
  `pythonpath = ["../.."]` — there's no parent conftest to inherit here, and keeping
  that `pythonpath` entry would put the dev repo root back on `sys.path`, making the
  repo's own `tests/` package importable from the dogfood suite and quietly undermining
  the isolation this suite exists to prove).
- `examples/web/pyproject.toml` — add `pytest`/`pytest-django`/`pytest-cov` as dev
  dependencies (currently has none — this app has never had a test suite).
- `.github/workflows/test.yml` — new, dedicated job (not a tox env) running
  `examples/web`'s suite against the plain `db` Postgres service, with its own Codecov
  flag/upload.

## Constraints carried over

Mirror existing SDK/naming conventions (`import typing as t`, absolute imports,
verb-named functions — the verb rule doesn't apply to the two fixture names themselves:
pytest fixtures name resources, not actions, matching this project's own existing noun
fixtures like `admin_user`/`staff_user` and `pytest-django`'s own `db`); no
monkeypatching; `flush_absurd_state`'s per-queue operation is always exactly one of
`client.drop_queue(name)` (`drop_schema=True`) or raw, identifier-quoted
`TRUNCATE ... CASCADE` (`drop_schema=False`) — never both against the same queue; the
pg_cron branch is mode-scoped (blanket `cron.job`/`cron.job_run_details` clear only for
`drop_schema=True`; the existing `teardown_crons(include_admin=True)` — never a
hand-rolled parallel implementation — for `drop_schema=False`, and
`cron.job_run_details` is never touched in that mode); both the queue and pg_cron steps
catch `(OperationalError, ProgrammingError, ImproperlyConfigured)` for an
unmigrated/absent schema, matching the existing `_reset_absurd_queues` precedent;
`absurd_drain_queue` delegates to the new internal `run_burst_worker()` function, not
`call_command("absurd_worker", ...)` (skips argparse/`CommandError` overhead for a
programmatic caller) and not the existing lower-level `drain_queue()` (a different,
already-taken name for a different, lower-level thing); `absurd_flush` delegates to
`flush_absurd_state(drop_schema=True)` rather than keeping its own parallel drop loop.
No new consumer-facing utility functions — the plugin's only public surface is the two
fixtures plus the `absurd_db` marker.

## Coverage strategy

`flush_absurd_state`'s two modes get dedicated coverage from two different callers, per
"Why one shared flush function" and Scope above: `drop_schema=False` (the shipped
default) from `examples/web`'s own suite; `drop_schema=True` (this repo's own need) from
`tests/core`/`tests/pg_cron`'s existing autouse wrapper (direct function call,
unaffected in effect) plus their new, dedicated marker-driven proof tests — genuine
dogfood on both sides, not a dedup-via-refactor of a functionally different internal
implementation (there no longer is one — internal and shipped code share the one
function, and `absurd_flush` no longer keeps its own parallel drop loop either).
`run_burst_worker()`'s command-side (`--burst`) tests and its own dedicated tests share
assertions via parametrization (mirroring `test_pg_cron_post_migrate.py`'s
`run_cron_sync` pattern) rather than duplicating them per entrypoint. `examples/web`'s
coverage is wired into CI as its own job with its own Codecov flag (see Module layout),
so the plugin module's patch coverage genuinely gates PRs rather than only surfacing in
local `--cov-report=term` output. The pytest11-entry-point coverage question (see
Scope's "Known, open question" bullet) is validated empirically during implementation,
not resolved speculatively here.
