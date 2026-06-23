# django-absurd Queue Model + Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans.
> Steps use checkbox (`- [ ]`) syntax. Plans state SCENARIOS + RED tests + prose
> implementation — NOT finished implementation code. Write the test, watch it fail, then
> implement minimally.

**Goal:** A read-only `Queue` model over `absurd.queues`, plus declarative queue sync
via `manage.py absurd_sync_queues` (create + reconcile), with an untagged system check
that nudges you to run it. No `migrate`/`post_migrate`/`ready()` queue magic.

**Architecture:** `Queue` is `managed=False` mapped to `absurd"."queues` (table owned by
spec 1's `0001` migration); its `CreateModel(managed=False)` op is folded into `0001`.
`ABSURD_QUEUES` (typed via the SDK's `CreateQueueOptions`) is the source of truth; the
`absurd_sync_queues` command upserts it through the SDK on Django's connection; a
registered system check compares declared vs actual and nudges.

**Tech Stack:** Django ≥5.2, `absurd-sdk` (runtime), psycopg (dev), pytest +
pytest-django via compose. Builds on the merged spec-1 package (`django_absurd/` at repo
root).

## Global Constraints

- Postgres only. `DATABASES['default']` only — no multi-DB routing (the command takes
  `--database`, default `default`).
- `absurd-sdk` is the SDK for queue ops; reach it via
  `Absurd(connections[using].connection)` (reuse Django's connection,
  `ensure_connection()` first). NOT the dev-only `absurdctl` CLI.
- **psycopg3 required (validate it).** The SDK reuses Django's raw connection and needs
  psycopg (v3). `get_absurd_client(using)` MUST assert the underlying connection is a
  `psycopg.Connection` and raise a clear error otherwise (`ImproperlyConfigured`:
  "django-absurd requires the psycopg (v3) Postgres backend"). A test asserts
  `get_absurd_client` works on the psycopg3 connection. (TODO carried forward: if we
  later want broader support, revisit — for now, defend the assumption explicitly.)
- **Testing conventions (see `CLAUDE.md`):** function-based pytest; **no
  monkeypatching**; test management commands AND system checks by running them via
  `call_command(...)` + pytest `capsys`, asserting on the **full emitted message**;
  drive states with real DB conditions, not mocks.
- **No queue magic in `migrate`/`post_migrate`/`ready()`.** `migrate` only migrates. All
  queue mutation is the explicit `absurd_sync_queues` command.
- `ABSURD_QUEUES` — `Mapping[str, CreateQueueOptions]`, default `{"default": {}}`.
  `list[str]` accepted as shorthand (→ `{name: {}}`). `{}`/`[]` = no declared queues.
  Type via the SDK's exported `CreateQueueOptions` TypedDict (no hand-maintained
  duplicate).
- `Queue`: `managed=False`, `db_table='absurd"."queues'`, `queue_name` PK. Read-only —
  `save()`/`delete()` raise. Bulk `QuerySet.update()/delete()` NOT intercepted
  (documented).
- Queue option keys: `storage_mode` (creation-only), `partition_lookahead`,
  `partition_lookback`, `cleanup_ttl`, `cleanup_limit`, `detach_mode`, `detach_min_age`.
  `set_queue_policy` reconciles all EXCEPT `storage_mode`.
- Sync is **non-destructive**: undeclared queues are never dropped; a `storage_mode`
  change on an existing queue is reported, not applied.
- System check: registered, **untagged** (runs on `check`/`runserver`), **guarded**.
  Three states when queues declared — DB unreachable → silent; schema absent → "run
  `migrate` then `absurd_sync_queues`"; schema present + drift → "run
  `absurd_sync_queues`"; in-sync → silent.
- `Queue`'s `CreateModel(managed=False)` op lives in `0001_initial_0_4_0` (no separate
  `0002`). Pre-release, safe; no DDL. The maintenance codegen must never clobber it
  (`0001` frozen).
- No underscore-private modules. Tests: pytest, function-based, outcome-focused. Run in
  container: `docker compose run --rm app <cmd>`.

> **Run convention:** every `pytest`/`python`/`manage.py` command runs in the container
> — prefix `docker compose run --rm app`. `db` auto-starts. Bare commands shown for
> brevity.

---

## File Structure

- `django_absurd/models.py` — `Queue` model, its `TextChoices`, `QueueReadOnlyError`.
- `django_absurd/queues.py` — `AbsurdQueues` type, `get_declared_queues()`,
  `get_absurd_client(using)`, `sync_queues(using)`. (Shared by command + check;
  non-underscore. Drift comparison lives inline in the check.)
- `django_absurd/management/__init__.py`,
  `django_absurd/management/commands/__init__.py`,
  `django_absurd/management/commands/absurd_sync_queues.py` — the command (shipped).
- `django_absurd/checks.py` — `check_absurd_queues`.
- `django_absurd/apps.py` — modify `ready()` to register the check.
- `django_absurd/migrations/0001_initial_0_4_0.py` — modify: add the
  `CreateModel(managed=False)` op.
- `tests/test_models.py`, `tests/test_queue_sync.py`, `tests/test_checks.py`.

---

## Scenarios

1. `Queue` is read-only and its `CreateModel` lives in `0001` (graph clean).
2. `migrate` alone creates no queue (no magic).
3. `absurd_sync_queues` creates declared queues (with options) and the `Queue` model
   reads their fields.
4. `absurd_sync_queues` reconciles changed options (upsert); idempotent.
5. Sync is non-destructive (undeclared survive; `storage_mode` change reported, not
   applied).
6. System check nudges across its three states.

---

## Task 1: Read-only `Queue` model (folded into `0001`)

**Files:**

- Modify: `tests/conftest.py` (autouse DB fixture)
- Create: `django_absurd/models.py`
- Modify: `django_absurd/migrations/0001_initial_0_4_0.py`
- Test: `tests/test_models.py`

**Interfaces:**

- Produces: autouse `_enable_db` fixture (all tests get DB without
  `@pytest.mark.django_db`); `Queue` (model; `managed=False`,
  `db_table='absurd"."queues'`, `queue_name` PK; fields `created_at`, `storage_mode`,
  `default_partition`, `partition_lookahead`, `partition_lookback`, `cleanup_ttl`,
  `cleanup_limit`, `detach_mode`, `detach_min_age`);
  `Queue.StorageMode`/`DefaultPartition`/`DetachMode` `TextChoices`;
  `QueueReadOnlyError(Exception)`.

- [ ] **Step 0: Autouse DB fixture** — `tests/conftest.py`

Add so tests don't each need `@pytest.mark.django_db`:

```python
import pytest


@pytest.fixture(autouse=True)
def _enable_db(db):  # noqa: PT004
    pass
```

Tests then only mark `@pytest.mark.django_db(transaction=True)` when they need
transactions (commits / DDL — `migrate`, `create_queue`), multiple DBs, or sequence
resets.

- [ ] **Step 1: Write the failing tests (RED)** — `tests/test_models.py`

```python
import pytest
from django.core.management import call_command

from django_absurd.models import Queue, QueueReadOnlyError


def test_queue_is_read_only():
    # Overrides raise before any DB access, so no django_db needed.
    q = Queue(queue_name="x")
    with pytest.raises(QueueReadOnlyError):
        q.save()
    with pytest.raises(QueueReadOnlyError):
        q.delete()


def test_queue_table_and_choices():
    assert Queue._meta.db_table == 'absurd"."queues'
    assert Queue._meta.managed is False
    assert set(Queue.StorageMode.values) == {"unpartitioned", "partitioned"}


def test_no_pending_migrations_for_app():
    # CreateModel op lives in 0001 — makemigrations sees no changes.
    call_command("makemigrations", "django_absurd", check=True, dry_run=True)
```

- [ ] **Step 2: Confirm RED**

Run: `docker compose run --rm app pytest tests/test_models.py -v` Expected:
import/collection FAIL — `django_absurd.models` doesn't exist.
(`test_no_pending_migrations_for_app` will also fail later if the op isn't folded in —
`call_command(..., check=True)` raises `SystemExit`.)

- [ ] **Step 3: Implement the model (prose)**

In `django_absurd/models.py`: define `QueueReadOnlyError(Exception)`. Define
`Queue(models.Model)` with `managed = False`, `db_table = 'absurd"."queues'`. Fields per
inspectdb: `queue_name = TextField(primary_key=True)`, `created_at = DateTimeField()`,
`storage_mode`/`default_partition`/`detach_mode` as `TextField(choices=...)`,
`partition_lookahead`/`partition_lookback`/`cleanup_ttl`/`detach_min_age = DurationField()`,
`cleanup_limit = IntegerField()`. Add inner `TextChoices`: `StorageMode`
(`UNPARTITIONED="unpartitioned"`, `PARTITIONED="partitioned"`), `DefaultPartition`
(`ENABLED`/`DISABLED`), `DetachMode` (`NONE="none"`, `EMPTY="empty"`). `__str__` returns
`queue_name`. Override `save(self, *a, **k)` and `delete(self, *a, **k)` to
`raise QueueReadOnlyError("Queue is read-only; manage queues via ABSURD_QUEUES + 'manage.py absurd_sync_queues', or the absurd-sdk.")`
before any super() call.

- [ ] **Step 4: Fold the model op into `0001` (prose)**

Run `docker compose run --rm app python -m django makemigrations django_absurd` — Django
generates `0002_queue.py` containing
`migrations.CreateModel(name="Queue", fields=[...], options={"managed": False, "db_table": 'absurd"."queues'})`.
Move that `CreateModel(...)` operation into
`django_absurd/migrations/0001_initial_0_4_0.py`'s `operations` list (append after the
existing `RunSQL`), then delete `0002_queue.py`. `0001` keeps `initial = True`,
`dependencies = []`. (The op is `managed=False` → no DDL; order vs `RunSQL` is
irrelevant.)

- [ ] **Step 5: GREEN**

Run: `docker compose run --rm app pytest tests/test_models.py -v` Expected: PASS (3).
`test_no_pending_migrations_for_app` passes only if the op is correctly folded in (no
stray migration).

- [ ] **Step 6: Commit**

```bash
git add django_absurd/models.py django_absurd/migrations/0001_initial_0_4_0.py tests/test_models.py
git commit -m "feat: read-only Queue model mapped to absurd schema (CreateModel folded into 0001)"
```

---

## Task 2: `ABSURD_QUEUES` + `absurd_sync_queues` command

**Files:**

- Create: `django_absurd/queues.py`, `django_absurd/management/__init__.py`,
  `django_absurd/management/commands/__init__.py`,
  `django_absurd/management/commands/absurd_sync_queues.py`
- Test: `tests/test_queue_sync.py`

**Interfaces:**

- Consumes: `Queue` (Task 1).
- Produces:
  - `AbsurdQueues = Mapping[str, CreateQueueOptions]` (alias; `CreateQueueOptions` from
    `absurd_sdk`).
  - `get_declared_queues() -> dict[str, dict]` — reads `settings.ABSURD_QUEUES` (default
    `{"default": {}}`), normalizes `list[str]` → `{name: {}}`.
  - `get_absurd_client(using: str = "default") -> Absurd` — `ensure_connection()`,
    assert the raw connection is a `psycopg.Connection` (else `ImproperlyConfigured`),
    then `Absurd(connections[using].connection)`.
  - `sync_queues(using: str = "default") -> SyncResult` — does the upsert; returns a
    small dataclass
    `SyncResult(created: list[str], reconciled: list[str], storage_warnings: list[str])`.
  - Command `absurd_sync_queues` with `--database` (default `default`).
- `_MUTABLE_OPTION_KEYS = ("partition_lookahead", "partition_lookback", "cleanup_ttl", "cleanup_limit", "detach_mode", "detach_min_age")`
  (everything except `storage_mode`).

- [ ] **Step 1: Write the failing tests (RED)** — `tests/test_queue_sync.py`

```python
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.db import connection

from django_absurd.models import Queue

# All tests here need real transactions (migrate / create_queue DDL) — one module marker
# instead of decorating each (CLAUDE.md). The autouse _enable_db fixture covers plain DB.
pytestmark = pytest.mark.django_db(transaction=True)


def table_exists(name):
    with connection.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", [f"absurd.{name}"])
        return cur.fetchone()[0]


def test_get_absurd_client_uses_psycopg3_connection():
    import psycopg
    from django_absurd.queues import get_absurd_client
    get_absurd_client()  # must not raise; reuses Django's psycopg3 connection
    assert isinstance(connection.connection, psycopg.Connection)


def test_migrate_creates_no_queue(settings):
    settings.ABSURD_QUEUES = {"alpha": {}}
    call_command("migrate", "django_absurd", verbosity=0)
    assert not Queue.objects.filter(queue_name="alpha").exists()


def test_sync_creates_with_options_and_model_maps(settings):
    settings.ABSURD_QUEUES = {"x": {"storage_mode": "partitioned", "cleanup_ttl": "90 days"}}
    call_command("absurd_sync_queues")
    q = Queue.objects.get(queue_name="x")
    assert q.storage_mode == "partitioned"
    assert q.cleanup_ttl == timedelta(days=90)
    assert table_exists("t_x")


def test_list_shorthand(settings):
    settings.ABSURD_QUEUES = ["alpha"]
    call_command("absurd_sync_queues")
    assert Queue.objects.filter(queue_name="alpha").exists()


def test_sync_reconciles_changed_option_idempotent(settings):
    settings.ABSURD_QUEUES = {"q": {"cleanup_limit": 100}}
    call_command("absurd_sync_queues")
    settings.ABSURD_QUEUES = {"q": {"cleanup_limit": 250}}
    call_command("absurd_sync_queues")
    assert Queue.objects.get(queue_name="q").cleanup_limit == 250
    call_command("absurd_sync_queues")  # idempotent, no error
    assert Queue.objects.get(queue_name="q").cleanup_limit == 250


def test_non_destructive(settings):
    settings.ABSURD_QUEUES = {"keep": {}}
    call_command("absurd_sync_queues")
    settings.ABSURD_QUEUES = {}
    call_command("absurd_sync_queues")
    assert Queue.objects.filter(queue_name="keep").exists()  # not dropped
```

- [ ] **Step 2: Confirm RED**

Run: `docker compose run --rm app pytest tests/test_queue_sync.py -v` Expected: FAIL —
`absurd_sync_queues` command unknown / `django_absurd.queues` missing.

- [ ] **Step 3: Implement `queues.py` (prose)**

`AbsurdQueues = Mapping[str, CreateQueueOptions]` (import `CreateQueueOptions` from
`absurd_sdk`). `get_declared_queues()`: read
`getattr(settings, "ABSURD_QUEUES", {"default": {}})`; if it's a list/tuple, normalize
to `{name: {} for name in it}`; return a plain `dict`. `get_absurd_client(using)`:
`connections[using].ensure_connection()`; assert
`isinstance(connections[using].connection, psycopg.Connection)` else raise
`ImproperlyConfigured("django-absurd requires the psycopg (v3) Postgres backend")`;
`return Absurd(connections[using].connection)`. `sync_queues(using="default")`: load
`existing = {q.queue_name: q for q in Queue.objects.using(using)}`;
`client = get_absurd_client(using)`; for `name, opts in get_declared_queues().items()`:
if `name not in existing` → `client.create_queue(name, **opts)` (record created); else →
call
`client.set_queue_policy(name, **{k: v for k, v in opts.items() if k in _MUTABLE_OPTION_KEYS})`
(record reconciled), and if
`"storage_mode" in opts and opts["storage_mode"] != existing[name].storage_mode` →
append a storage warning (do NOT apply). Return `SyncResult`. Never drop undeclared
queues.

- [ ] **Step 4: Implement the command (prose)**

`django_absurd/management/commands/absurd_sync_queues.py`: a `BaseCommand` with
`add_arguments` adding `--database` (default `"default"`). `handle`:
`result = sync_queues(using=options["database"])`; write a summary to `self.stdout`
(created/reconciled counts + names); write each `result.storage_warnings` line to
`self.stderr` (`self.style.WARNING`). Add the two empty `__init__.py` files so the
command is discoverable.

- [ ] **Step 5: GREEN**

Run: `docker compose run --rm app pytest tests/test_queue_sync.py -v` Expected: PASS
(5).

- [ ] **Step 6: Commit**

```bash
git add django_absurd/queues.py django_absurd/management tests/test_queue_sync.py
git commit -m "feat: ABSURD_QUEUES setting + absurd_sync_queues command (create + reconcile, non-destructive)"
```

---

## Task 3: Drift system check

**Files:**

- Create: `django_absurd/checks.py`
- Modify: `django_absurd/apps.py`
- Test: `tests/test_checks.py`

**Interfaces:**

- Consumes: `get_declared_queues` (Task 2), `Queue` (Task 1).
- Produces: `check_absurd_queues(app_configs, **kwargs) -> list[CheckMessage]`,
  registered untagged in `apps.ready()`. Module constants for the exact messages —
  `W001_MSG = "django-absurd: run 'migrate' then 'manage.py absurd_sync_queues' to provision declared queues."`
  (id `absurd.W001`, schema absent) and
  `W002_MSG = "django-absurd: declared queues are out of sync — run 'manage.py absurd_sync_queues'."`
  (id `absurd.W002`, drift). Tests import + assert the **full** messages. The check
  queries the DB directly; tests drive **real** states (no monkeypatch).

- [ ] **Step 1: Write the failing tests (RED)** — `tests/test_checks.py`

```python
import pytest
from django.core.management import call_command
from django.db import connection, connections

from django_absurd.checks import W001_MSG, W002_MSG

# All tests here need real transactions (sync DDL, migrate, mid-flight DB swap).
pytestmark = pytest.mark.django_db(transaction=True)


def run_absurd_check(capsys):
    capsys.readouterr()  # clear anything prior
    call_command("check", "django_absurd")  # warnings print, exit 0
    cap = capsys.readouterr()
    return cap.out + cap.err


def test_in_sync_no_warning(settings, capsys):
    settings.ABSURD_QUEUES = {"synced": {}}
    call_command("absurd_sync_queues")
    out = run_absurd_check(capsys)
    assert W001_MSG not in out
    assert W002_MSG not in out


def test_drift_warns_run_sync(settings, capsys):
    settings.ABSURD_QUEUES = {"synced": {}}
    call_command("absurd_sync_queues")
    settings.ABSURD_QUEUES = {"synced": {}, "missing": {}}  # 'missing' not provisioned
    out = run_absurd_check(capsys)
    assert "absurd.W002" in out
    assert W002_MSG in out  # full message, not just a token


def test_schema_absent_warns_migrate_first(settings, capsys):
    settings.ABSURD_QUEUES = {"a": {}}
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS absurd CASCADE")
    try:
        out = run_absurd_check(capsys)
        assert "absurd.W001" in out
        assert W001_MSG in out  # full message
    finally:  # restore schema for other tests (django_migrations still records 0001 applied)
        call_command("migrate", "django_absurd", "zero", verbosity=0)
        call_command("migrate", "django_absurd", verbosity=0)


def test_db_unreachable_is_silent(settings, capsys):
    # Point the default connection at a database that doesn't exist -> connecting raises
    # OperationalError; the guarded check must swallow it and emit nothing. No mocks.
    settings.ABSURD_QUEUES = {"a": {}}
    settings.DATABASES = {
        **settings.DATABASES,
        "default": {**settings.DATABASES["default"], "NAME": "absurd_nope_missing_db"},
    }
    connections["default"].close()  # force reconnect with the bad NAME
    try:
        out = run_absurd_check(capsys)
        assert W001_MSG not in out
        assert W002_MSG not in out
    finally:
        connections["default"].close()  # settings fixture restores NAME; reconnect clean
```

- [ ] **Step 2: Confirm RED**

Run: `docker compose run --rm app pytest tests/test_checks.py -v` Expected: FAIL —
`django_absurd.checks` missing.

- [ ] **Step 3: Implement `checks.py` (prose)**

`check_absurd_queues(app_configs, **kwargs)`: `declared = get_declared_queues()`; if not
declared → `return []`. Query actual config directly inside a guard:
`try: actual = {q.queue_name: q for q in Queue.objects.all()}`
`except OperationalError: return []` (DB unreachable — skip; covered by
`test_db_unreachable_is_silent`)
`except ProgrammingError: return [Warning(W001_MSG, hint=..., id="absurd.W001")]`
(define `W001_MSG`/`W002_MSG` as module constants — see Interfaces — so tests assert the
exact text). Then compute drift: a declared queue missing from `actual`, OR a declared
mutable option ≠ the queue's field (normalize declared duration strings → `timedelta`
for comparison; `cleanup_limit` int compares directly; `detach_mode`/`storage_mode`
str), OR `storage_mode` mismatch (note creation-only). If any →
`return [Warning(W002_MSG, hint=<names>, id="absurd.W002")]`. Else `return []`.
(Duration normalization helper: parse `"90 days"`/`"2 days"` → `timedelta`; small,
covered by the drift test via `missing` queue + the `cleanup_limit` upsert path.) The
check uses the default DB. Imports `OperationalError`/`ProgrammingError` from
`django.db.utils`.

- [ ] **Step 4: Register the check (prose)**

In `django_absurd/apps.py`, `AbsurdConfig.ready()`:
`from django.core.checks import register; from . import checks; register(checks.check_absurd_queues)`
(untagged — runs on `check`/`runserver`). Keep `ready()` free of DB access (registration
only; the check itself does the guarded query when invoked).

- [ ] **Step 5: GREEN**

Run: `docker compose run --rm app pytest tests/test_checks.py -v` Expected: PASS (4).

- [ ] **Step 6: Full gate + commit**

Run: `docker compose run --rm app pytest` (all green) and
`docker compose run --rm --no-deps app ruff check .` (clean).

```bash
git add django_absurd/checks.py django_absurd/apps.py tests/test_checks.py
git commit -m "feat: queue drift system check (untagged, guarded, 3-state nudge)"
```

---

## Self-Review

- **Spec coverage:** read-only Queue + db_table + choices (T1); CreateModel folded into
  `0001` + `makemigrations --check` clean (T1); no-migrate-magic (T2
  `test_migrate_creates_no_queue`); create-with-options + model-maps via
  settings+command (T2); list shorthand + empty disable (T2); upsert/reconcile +
  idempotent (T2); non-destructive + storage_mode-reported (T2 `sync_queues` +
  `test_non_destructive`); `ABSURD_QUEUES` typed via SDK `CreateQueueOptions` (T2
  `AbsurdQueues`); system check 3 states + untagged + guarded (T3). All covered.
- **Placeholder scan:** none — tests are concrete; implementation is prose with exact
  names/signatures (no finished impl blocks, per the TDD-plan rule).
- **Type consistency:**
  `get_declared_queues`/`get_absurd_client`/`sync_queues`/`SyncResult`/`check_absurd_queues`
  names match across T2↔T3 and the tests (all verb-named per CLAUDE.md); `Queue` +
  `QueueReadOnlyError` + `StorageMode` consistent T1↔T2↔T3; `_MUTABLE_OPTION_KEYS`
  excludes `storage_mode` consistently with the reconcile/drift logic.
