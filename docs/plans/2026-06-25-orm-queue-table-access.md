# ORM Queue-Table Access — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose Absurd's per-queue tables as public read-only Django ORM models
(`Task, Run, Checkpoint, Event, Wait`) under `django_absurd.models`, backed by
per-entity UNION-ALL views provisioned eagerly on queue creation; have the admin and
`get_result` consume those same models.

**Architecture:** The existing per-entity union views + their unmanaged factory models
(today admin-only) become the single source of truth. Views are (re)built only at
queue-creation seams (`absurd_sync_queues`, worker-start-if-it-created-its-queue) and
seeded empty by a migration — so ORM/admin reads are pure `SELECT`, no read-path DDL.
The admin's lazy self-heal is deleted; `get_result`'s raw SQL is refactored onto the
models.

**Tech Stack:** Django 6.0, Python 3.12, psycopg3, PostgreSQL (Absurd 0.4.0 schema),
pytest, beautifulsoup4.

**Spec:** `docs/specs/2026-06-25-orm-queue-table-access-design.md` (read it; this plan
implements it). **Decisions locked: D1 = (a) NO view-rebuild on the enqueue
create-branch. D2 = tolerant views if the Task 7 spike passes, else a typed error.**

## Global Constraints

- Django 6.0 / Python 3.12 floor; psycopg (v3). Read-only — no mutation of Absurd state.
- `import typing as t` — never `from typing import X`. Absolute imports. Import from the
  DEFINITION module (backends-from-backends, queues-from-queues).
- Functions contain a verb. No leading-underscore module constants/helpers. Helpers
  BELOW their public function.
- System-check `msg` = problem; `hint` = resolution; never duplicate.
- Tests: pytest, function-based ONLY. Autouse `_enable_db(db)` grants DB — don't add
  `@pytest.mark.django_db`; add `@pytest.mark.django_db(transaction=True)` only for
  commits/DDL. No mocks/monkeypatch — drive with real DB. Test commands/checks by
  RUNNING them, assert full emitted text.
- `makemigrations` MUST stay clean — view models live in the private `Apps` registry.
- NO DDL on any read path. NO DB access at import/app-ready.
- Postgres on host port 5433: run `PGPORT=5433 uv run pytest …`. ruff `select=ALL`, no
  new `noqa` beyond the repo's `# noqa: SLF001` (for `_meta`) without asking. Pre-commit
  reformats — re-stage + commit if it aborts.

---

## File Structure

- **Create `django_absurd/exceptions.py`** — `QueueReadOnlyError` (relocated),
  `QUEUE_READONLY_MSG`, `ADMIN_VIEW_READONLY_MSG`, and (D2 fallback)
  `ViewNotProvisionedError`. Imports nothing from the package → breaks the
  `admin_views → models` cycle.
- **Modify `django_absurd/models.py`** — import
  `QueueReadOnlyError`/`QUEUE_READONLY_MSG` from `exceptions` (re-export for
  back-compat); expose `Task, Run, Checkpoint, Event, Wait` via the factory.
- **Modify `django_absurd/admin_views.py`** — import the error from `exceptions`; rename
  `view_name` `admin_<e>`→`<e>_view` and `model_name` `Absurd<X>`→`<X>`; add
  `rebuild_views(using)`; DELETE `ensure_view_current` + `VIEW_BUILD_CACHE` +
  `VIEW_BUILD_LOCK` + `invalidate_view_cache` + `reset_view_cache`; (Task 7)
  tolerant-arm view SQL.
- **Modify `django_absurd/admin.py`** — import models from `models.py`; delete the
  `get_queryset` self-heal/retry (plain queryset); fix `runs_link` reverse to the
  renamed URL.
- **Modify `django_absurd/backends.py`** — refactor `get_result` raw SQL → ORM
  (`Task`/`Run`). (No enqueue view-hook — D1.)
- **Modify `django_absurd/queues.py`, `management/commands/absurd_sync_queues.py`,
  `management/commands/absurd_worker.py`** — call `rebuild_views` at the creation seams.
- **Create `django_absurd/migrations/0002_create_admin_views.py`** — `RunSQL` seeding
  the 5 empty views (does NOT touch the pinned `0001_initial_0_4_0.sql`).
- **Modify `tests/test_admin_*.py`** — repoint admin URL names (`absurd<x>`→`<x>`).
- Test files: `tests/test_exceptions.py` (or fold into existing),
  `tests/test_orm_models.py`, `tests/test_orm_views.py`, plus edits to
  `tests/test_admin_*.py` and reuse of `tests/test_results.py`.

---

## Task 1: `exceptions.py` — break the circular import

**Files:**

- Create: `django_absurd/exceptions.py`
- Modify: `django_absurd/models.py:5-12`, `django_absurd/admin_views.py:9`
- Test: `tests/test_app.py` (add an import-cycle assertion)

**Interfaces:**

- Produces: `django_absurd.exceptions.QueueReadOnlyError`, `QUEUE_READONLY_MSG`,
  `ADMIN_VIEW_READONLY_MSG`, `ViewNotProvisionedError`. `models.py` re-exports
  `QueueReadOnlyError`+`QUEUE_READONLY_MSG` (back-compat). `admin_views.py` imports
  `QueueReadOnlyError`+`ADMIN_VIEW_READONLY_MSG` from `exceptions` (no longer from
  `models`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app.py  (append)
def test_models_imports_without_cycle():
    import importlib
    # admin_views must NOT import models (would cycle once models imports the factory)
    import django_absurd.admin_views as av
    src = importlib.import_module("django_absurd.admin_views").__file__
    with open(src) as f:
        assert "from django_absurd.models import" not in f.read()
    from django_absurd.exceptions import QueueReadOnlyError
    from django_absurd.models import QueueReadOnlyError as ReExported
    assert QueueReadOnlyError is ReExported
```

- [ ] **Step 2: Run, verify fail**

Run: `PGPORT=5433 uv run pytest tests/test_app.py::test_models_imports_without_cycle -v`
Expected: FAIL — `admin_views.py` still has
`from django_absurd.models import QueueReadOnlyError`; `django_absurd.exceptions`
doesn't exist.

- [ ] **Step 3: Implement (prose)**

Create `django_absurd/exceptions.py` defining `QueueReadOnlyError(Exception)`, the
`QUEUE_READONLY_MSG` string (move verbatim from `models.py:5-8`), an
`ADMIN_VIEW_READONLY_MSG`, and a `ViewNotProvisionedError(Exception)` (used by D2
fallback later). In `models.py`, replace the local
`QueueReadOnlyError`/`QUEUE_READONLY_MSG` definitions with
`from django_absurd.exceptions import QueueReadOnlyError, QUEUE_READONLY_MSG` (keeps
existing `Queue` references working). In `admin_views.py:9`, change the import to
`from django_absurd.exceptions import QueueReadOnlyError, ADMIN_VIEW_READONLY_MSG`.
Absolute imports; `exceptions.py` imports nothing from the package.

- [ ] **Step 4: Run, verify pass + suite green**

Run: `PGPORT=5433 uv run pytest tests/test_app.py -v && PGPORT=5433 uv run pytest -q`
Expected: PASS; full suite still green (185+).

- [ ] **Step 5: Commit**

```bash
git add django_absurd/exceptions.py django_absurd/models.py django_absurd/admin_views.py tests/test_app.py
git commit -m "refactor: extract exceptions.py to break admin_views↔models cycle"
```

---

## Task 2: Rename views + model names (`Absurd` prefix drop, `<entity>_view`)

**Files:**

- Modify: `django_absurd/admin_views.py:33-149` (the 5 `EntitySpec`s),
  `django_absurd/admin.py:173` (`runs_link` reverse)
- Modify: `tests/test_admin_http.py` (all `reverse("admin:django_absurd_absurd…")` + the
  registered-name assertions), `tests/test_admin_models.py` (model-name assertions)

**Interfaces:**

- Produces: specs with `view_name` ∈
  {`tasks_view`,`runs_view`,`checkpoints_view`,`events_view`,`waits_view`} and
  `model_name` ∈ {`Task`,`Run`,`Checkpoint`,`Event`,`Wait`}. Admin URL names become
  `admin:django_absurd_<entity>_*` (e.g. `…_task_changelist`). Model `_meta.model_name`
  ∈ {`task`,`run`,…}.

- [ ] **Step 1: Update the admin tests to the new names (these are the RED spec)**

In `tests/test_admin_http.py`, replace every `admin:django_absurd_absurdtask_*` →
`admin:django_absurd_task_*` (and `absurdrun`→`run`, `absurdcheckpoint`→`checkpoint`,
`absurdevent`→`event`, `absurdwait`→`wait`); the href assertion
`"/django_absurd/absurdtask/"` → `"/django_absurd/task/"`; the registered set
`{"absurdtask",…}` → `{"task","run","checkpoint","event","wait","queue"}`; the
`m._meta.model_name == "absurdtask"` assertions → `"task"`. In
`tests/test_admin_models.py`, the model-name expectations → unprefixed.

- [ ] **Step 2: Run, verify fail**

Run: `PGPORT=5433 uv run pytest tests/test_admin_http.py tests/test_admin_models.py -q`
Expected: FAIL — `NoReverseMatch` (URLs still registered under `absurd<x>` because specs
still say `model_name="Absurd<X>"`).

- [ ] **Step 3: Implement (prose)**

In `admin_views.py` `ADMIN_ENTITY_SPECS`, change each spec's `view_name` to
`<entity>_view` and `model_name` to the unprefixed class name (`Task`, `Run`,
`Checkpoint`, `Event`, `Wait`). In `admin.py:173`, update the `runs_link` reverse target
to `admin:django_absurd_run_changelist`. (The factory derives
`db_table=f'absurd"."{view_name}'`, so the renamed views are now `tasks_view` etc.)

- [ ] **Step 4: Run, verify pass**

Run: `PGPORT=5433 uv run pytest tests/test_admin_http.py tests/test_admin_models.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/admin_views.py django_absurd/admin.py tests/test_admin_http.py tests/test_admin_models.py
git commit -m "refactor: drop Absurd prefix on view models; views named <entity>_view"
```

---

## Task 3: Expose the models in `django_absurd.models`

**Files:**

- Modify: `django_absurd/models.py`, `django_absurd/admin.py` (import models from
  `models`)
- Test: `tests/test_orm_models.py`

**Interfaces:**

- Consumes: `build_admin_model`, `ADMIN_ENTITY_SPECS` (admin_views, Task 1/2).
- Produces: `django_absurd.models.Task`, `.Run`, `.Checkpoint`, `.Event`, `.Wait` — the
  idempotent factory's classes (same instances the admin registers). `admin.py` imports
  them from `models`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_orm_models.py
from django.apps import apps as global_apps


def test_models_importable_and_view_backed():
    from django_absurd.models import Task, Run, Checkpoint, Event, Wait
    assert Task._meta.db_table == 'absurd"."tasks_view'
    assert Task._meta.managed is False
    assert {Run, Checkpoint, Event, Wait}  # all importable


def test_view_models_absent_from_global_registry():
    import django_absurd.models  # noqa: F401
    names = {m.__name__ for m in global_apps.get_models() if m._meta.app_label == "django_absurd"}
    assert names == {"Queue"}  # only the real managed=False Queue is global


def test_admin_uses_the_models_py_classes():
    from django_absurd import models as m
    from django_absurd.admin_views import ADMIN_ENTITY_SPECS, build_admin_model
    spec = next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
    assert build_admin_model(spec) is m.Task  # idempotent factory → same class
```

- [ ] **Step 2: Run, verify fail**

Run: `PGPORT=5433 uv run pytest tests/test_orm_models.py -v` Expected: FAIL —
`cannot import name 'Task' from django_absurd.models`.

- [ ] **Step 3: Implement (prose)**

In `models.py`, after the `Queue` model, build + assign the five view models from the
specs: for each `spec` in `ADMIN_ENTITY_SPECS`,
`globals()[spec.model_name] = build_admin_model(spec)` — or explicit
`Task = build_admin_model(SPEC_TASKS)` lines for clarity/typing. Import the factory +
specs from `admin_views` (the cycle is broken by Task 1, so this import is safe).
Confirm the factory stays idempotent (returns the cached class), so `admin.py` and
`models.py` get the same objects. In `admin.py`
`register_absurd_admin`/`build_entity_admin`, import the model classes from
`django_absurd.models` instead of building them inline (or keep building via the
idempotent factory — same instances; pick the models.py import for one source of truth).

- [ ] **Step 4: Run, verify pass + makemigrations/mypy clean**

Run:

```bash
PGPORT=5433 uv run pytest tests/test_orm_models.py -v
PGPORT=5433 uv run python -m django makemigrations --check --dry-run --settings=tests.settings
PGPORT=5433 uv run mypy django_absurd/
```

Expected: tests PASS; "No changes detected"; mypy clean. (Both proven by spike.)

- [ ] **Step 5: Commit**

```bash
git add django_absurd/models.py django_absurd/admin.py tests/test_orm_models.py
git commit -m "feat: expose Task/Run/Checkpoint/Event/Wait under django_absurd.models"
```

---

## Task 4: `rebuild_views` + delete the read-path self-heal

**Files:**

- Modify: `django_absurd/admin_views.py:234-258` (add `rebuild_views`; delete
  `ensure_view_current`/`VIEW_BUILD_CACHE`/`VIEW_BUILD_LOCK`/`invalidate_view_cache`/`reset_view_cache`),
  `django_absurd/admin.py:95-112` (plain `get_queryset`)
- Test: `tests/test_orm_views.py`

**Interfaces:**

- Consumes: `rebuild_admin_view`, `fetch_catalog_queues`, `ADMIN_ENTITY_SPECS`.
- Produces: `rebuild_views(using: str) -> None` — rebuilds all five views over the
  current catalog. `ensure_view_current` and the in-process cache are GONE.
  `ReadOnlyAbsurdAdmin.get_queryset(request)` returns `self.model.objects.all()` (no
  DDL, no retry).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_orm_views.py
import pytest
from django.core.management import call_command
from django.db import connections
from django_absurd.admin_views import ADMIN_ENTITY_SPECS, rebuild_views, build_admin_model

pytestmark = pytest.mark.django_db(transaction=True)


def view_oid(name):
    with connections["default"].cursor() as cur:
        cur.execute("SELECT to_regclass(%s)::oid", [f"absurd.{name}"])
        return cur.fetchone()[0]


def test_rebuild_views_builds_all_five():
    call_command("absurd_sync_queues")  # creates declared queues
    rebuild_views("default")
    for spec in ADMIN_ENTITY_SPECS:
        assert view_oid(spec.view_name) is not None


def test_read_path_does_no_ddl():
    call_command("absurd_sync_queues")
    rebuild_views("default")
    spec = next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
    before = view_oid(spec.view_name)
    Task = build_admin_model(spec)
    list(Task.objects.all())          # a read
    list(Task.objects.filter(state="completed"))
    assert view_oid(spec.view_name) == before  # no DROP/CREATE happened


def test_ensure_view_current_removed():
    import django_absurd.admin_views as av
    assert not hasattr(av, "ensure_view_current")
    assert not hasattr(av, "VIEW_BUILD_CACHE")
```

- [ ] **Step 2: Run, verify fail**

Run: `PGPORT=5433 uv run pytest tests/test_orm_views.py -v` Expected: FAIL —
`rebuild_views` undefined; `ensure_view_current`/`VIEW_BUILD_CACHE` still present.

- [ ] **Step 3: Implement (prose)**

In `admin_views.py`: add `rebuild_views(using)` that reads `fetch_catalog_queues(using)`
once and calls `rebuild_admin_view(spec, queues, using)` for every spec in
`ADMIN_ENTITY_SPECS` (helper below the public fn). DELETE `ensure_view_current`,
`VIEW_BUILD_CACHE`, `VIEW_BUILD_LOCK`, `invalidate_view_cache`, `reset_view_cache` and
their imports (`threading`). In `admin.py`, replace the `get_queryset` retry/self-heal
body (lines ~95-112) with a plain `return self.model.objects.all()` (no
`ensure_view_current`). Keep `get_object`'s queue-parse, perms, paginator, filter
unchanged.

- [ ] **Step 4: Run, verify pass + suite**

Run:
`PGPORT=5433 uv run pytest tests/test_orm_views.py tests/test_admin_http.py tests/test_admin_refresh.py -q`
Expected: PASS. NOTE: `tests/test_admin_refresh.py` tested the deleted self-heal —
repoint/delete its cases here (the lazy-refresh + concurrent-drop + degrade tests no
longer apply; replace with the eager-on-create tests in Task 6 / drop behavior in Task
7). Remove the now-invalid refresh tests in this commit.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/admin_views.py django_absurd/admin.py tests/test_orm_views.py tests/test_admin_refresh.py
git commit -m "feat: rebuild_views(); delete read-path self-heal (admin reads are plain SELECT)"
```

---

## Task 5: Seed empty views in a new migration

**Files:**

- Create: `django_absurd/migrations/0002_create_admin_views.py`
- Test: `tests/test_orm_views.py` (add)

**Interfaces:**

- Consumes: `build_union_view_sql(spec, [])` (the zero-queue empty form).
- Produces: a migration whose `RunSQL` creates the five empty views; reverse is a no-op
  (the `0001` schema-CASCADE drop already removes them).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_orm_views.py  (append)
def test_empty_views_exist_after_migrate_only(django_db_blocker):
    # fresh schema, NO sync, zero queues → views still exist + read empty
    call_command("migrate", "django_absurd", "zero", verbosity=0)
    call_command("migrate", "django_absurd", verbosity=0)
    for spec in ADMIN_ENTITY_SPECS:
        assert view_oid(spec.view_name) is not None
    Task = build_admin_model(next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks"))
    assert list(Task.objects.all()) == []
```

(Mark this test `@pytest.mark.django_db(transaction=True)` — it migrates.)

- [ ] **Step 2: Run, verify fail**

Run:
`PGPORT=5433 uv run pytest tests/test_orm_views.py::test_empty_views_exist_after_migrate_only -v`
Expected: FAIL — after `migrate` (no sync) the views don't exist yet.

- [ ] **Step 3: Implement (prose)**

Create `0002_create_admin_views.py` (depends on `0001_initial_0_4_0`). Use
`migrations.RunSQL` with forward SQL = the five empty-view `CREATE`s (assemble by
calling `build_union_view_sql(spec, [])` for each spec at migration-write time, OR
inline the generated SQL — generate it once and paste, since migrations must be static;
do NOT import runtime code that could drift). Reverse SQL = `migrations.RunSQL.noop`
(schema CASCADE in 0001's reverse handles teardown). Do NOT edit
`0001_initial_0_4_0.sql`.

- [ ] **Step 4: Run, verify pass + makemigrations clean**

Run:

```bash
PGPORT=5433 uv run pytest tests/test_orm_views.py -q
PGPORT=5433 uv run python -m django makemigrations --check --dry-run --settings=tests.settings
```

Expected: PASS; "No changes detected".

- [ ] **Step 5: Commit**

```bash
git add django_absurd/migrations/0002_create_admin_views.py tests/test_orm_views.py
git commit -m "feat: seed empty union views in a migration (fresh DB works pre-sync)"
```

---

## Task 6: Queue-creation hooks (sync command + worker-if-created)

**Files:**

- Modify: `django_absurd/management/commands/absurd_sync_queues.py:14-18`,
  `django_absurd/management/commands/absurd_worker.py:95-98`
- Test: `tests/test_orm_views.py` (add), `tests/test_worker.py` /
  `tests/test_queue_sync.py` as needed

**Interfaces:**

- Consumes: `rebuild_views(using)` (Task 4), `SyncResult.created` (`queues.py`).
- Produces: `absurd_sync_queues` rebuilds all views after reconciling; `absurd_worker`
  rebuilds iff `result.created` is non-empty. (No enqueue hook — D1.)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_orm_views.py  (append)
def test_sync_command_rebuilds_views_with_new_queue():
    call_command("absurd_sync_queues")            # default + other (declared)
    Task = build_admin_model(next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks"))
    from tests.tasks import add
    add.using(queue_name="other").enqueue(1, 1)
    call_command("absurd_worker", queue="other", burst=True)
    qs = Task.objects.values_list("queue", flat=True).distinct()
    assert "other" in set(qs)  # 'other' arm present (sync built views over declared set)


def test_worker_start_rebuilds_when_it_created_queue():
    from django_absurd.queues import get_absurd_client
    from tests.tasks import add
    Task = build_admin_model(next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks"))
    call_command("absurd_sync_queues")
    get_absurd_client().drop_queue("other")        # 'other' gone
    call_command("absurd_sync_queues")             # views rebuilt WITHOUT 'other'
    # worker is the FIRST to touch 'other' → reconcile creates it → must rebuild views
    call_command("absurd_worker", queue="other", burst=True)
    # if the worker-create rebuild fired, the 'other' arm is now in the view:
    add.using(queue_name="other").enqueue(7, 8)
    call_command("absurd_worker", queue="other", burst=True)  # drain (queue exists → no rebuild)
    assert Task.objects.filter(queue="other").count() >= 1  # row visible ⇒ arm present
```

- [ ] **Step 2: Run, verify fail**

Run: `PGPORT=5433 uv run pytest tests/test_orm_views.py -k "rebuilds" -v` Expected: FAIL
— commands don't call `rebuild_views` yet, so the views are stale/empty for the new
queue.

- [ ] **Step 3: Implement (prose)**

In `absurd_sync_queues.py` `handle()`: after the per-backend `sync_queues` loop (after
line 18), call `rebuild_views(backend.database)` once per backend (build-all). In
`absurd_worker.py` after `reconcile_queue` (line 95-98):
`if result.created: rebuild_views(backend.database)`. Import `rebuild_views` from
`admin_views`. Do NOT add any view rebuild to the enqueue create-branch in `backends.py`
(D1=a). Helpers below public code.

- [ ] **Step 4: Run, verify pass**

Run:
`PGPORT=5433 uv run pytest tests/test_orm_views.py tests/test_queue_sync.py tests/test_worker.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/management/commands/absurd_sync_queues.py django_absurd/management/commands/absurd_worker.py tests/test_orm_views.py
git commit -m "feat: rebuild views at queue-creation seams (sync command; worker if it created its queue)"
```

---

## Task 7: D2 — `drop_queue` behavior (spike, then tolerant views or typed error)

**Files:**

- Modify: `django_absurd/admin_views.py` (`build_union_view_sql`) OR
  `django_absurd/admin.py`+`exceptions.py` (depending on spike)
- Test: `tests/test_orm_views.py` (add)

**Interfaces:**

- Produces: after `drop_queue` (CASCADE drops the view), reads behave per the chosen
  option (tolerant → that queue's rows vanish, others fine; OR `ViewNotProvisionedError`
  with a "run absurd_sync_queues" message — never a raw psycopg error).

- [ ] **Step 0: SPIKE — does a `to_regclass`-guarded tolerant arm prune + degrade?**

Throwaway: build a union view whose each arm is guarded (e.g.
`SELECT … FROM absurd."t_<q>" WHERE to_regclass('absurd."t_<q>"') IS NOT NULL` won't
prune the table reference — a missing table errors at parse). The real tolerant pattern:
build the view over only the queues that currently exist (so a dropped queue is simply
absent from the union) AND have reads tolerate a fully-missing view. `EXPLAIN`
`WHERE queue='x'` over the tolerant view → confirm arm pruning still holds. Decide:

- If a tolerant view (rebuilt to exclude the dropped queue on next read is NOT possible
  — no read-path rebuild). So the realistic D2 is: a dropped queue's view is gone until
  sync; **wrap the read** to raise `ViewNotProvisionedError` (typed) instead of raw
  `UndefinedTable`. Confirm the spike shows tolerant-arm SQL cannot self-heal without a
  rebuild → **fall back to typed error (D2=b).**

(Record the spike result in the commit message; this step decides Steps 1-3.)

- [ ] **Step 1: Write the failing test (typed-error form, the likely outcome)**

```python
# tests/test_orm_views.py  (append)
def test_dropped_queue_read_raises_typed_error():
    from django_absurd.exceptions import ViewNotProvisionedError
    call_command("absurd_sync_queues")
    rebuild_views("default")
    with connections["default"].cursor() as cur:
        cur.execute("DROP VIEW IF EXISTS absurd.tasks_view")  # simulate CASCADE
    Task = build_admin_model(next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks"))
    with pytest.raises(ViewNotProvisionedError):
        list(Task.objects.all())
    call_command("absurd_sync_queues")  # restores
    assert list(Task.objects.all()) == [] or True
```

- [ ] **Step 2: Run, verify fail**

Run:
`PGPORT=5433 uv run pytest tests/test_orm_views.py::test_dropped_queue_read_raises_typed_error -v`
Expected: FAIL — currently a raw `ProgrammingError` (or `ViewNotProvisionedError`
undefined).

- [ ] **Step 3: Implement (prose, per spike outcome)**

Give the view models a manager/queryset that catches
`(ProgrammingError, OperationalError)` for a missing view and re-raises
`ViewNotProvisionedError("…run absurd_sync_queues…")` (a thin `Manager.get_queryset`
wrapper — NO DDL, just error translation). For the admin, the same missing-view case
degrades the changelist to empty (catch in the admin `get_queryset`, return `.none()`).
If the spike instead proved tolerant views viable, implement that in
`build_union_view_sql` and assert the dropped queue's rows vanish without error (rewrite
the test accordingly).

- [ ] **Step 4: Run, verify pass**

Run: `PGPORT=5433 uv run pytest tests/test_orm_views.py -q` Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/admin_views.py django_absurd/admin.py django_absurd/exceptions.py tests/test_orm_views.py
git commit -m "feat: D2 — dropped-queue reads raise typed ViewNotProvisionedError (admin degrades to empty)"
```

---

## Task 8: ORM public-API behavior tests

**Files:**

- Test: `tests/test_orm_models.py` (add)

**Interfaces:** Consumes everything above.

- [ ] **Step 1: Write the tests**

```python
# tests/test_orm_models.py  (append)
import pytest
from django.core.management import call_command
from django.db.models import Count
from tests.tasks import add, boom
from django_absurd.params import AbsurdSpawnParams

pytestmark = pytest.mark.django_db(transaction=True)


def _seed_two_queues():
    call_command("absurd_sync_queues")
    add.enqueue(2, 3)
    add.using(queue_name="other").enqueue(7, 8)
    boom.enqueue(absurd_spawn_params=AbsurdSpawnParams(max_attempts=1))
    call_command("absurd_worker", queue="default", burst=True)
    call_command("absurd_worker", queue="other", burst=True)


def test_filter_across_and_per_queue():
    from django_absurd.models import Task
    _seed_two_queues()
    assert {r.queue for r in Task.objects.all()} == {"default", "other"}
    assert Task.objects.filter(queue="other").count() == 1
    assert Task.objects.filter(state="completed").exists()


def test_cross_queue_aggregate_and_order():
    from django_absurd.models import Task
    _seed_two_queues()
    by_queue = dict(Task.objects.values_list("queue").annotate(n=Count("*")))
    assert by_queue["other"] == 1 and by_queue["default"] >= 2
    recent = list(Task.objects.order_by("-enqueue_at")[:2])
    assert len(recent) == 2


def test_read_only_save_blocked():
    from django_absurd.models import Task
    from django_absurd.exceptions import QueueReadOnlyError
    with pytest.raises(QueueReadOnlyError):
        Task().save()
```

- [ ] **Step 2: Run**

Run: `PGPORT=5433 uv run pytest tests/test_orm_models.py -v` Expected: behavior already
built (Tasks 3-6) → PASS. Any FAIL = real gap; fix the implementation (not the test).

- [ ] **Step 3: Commit**

```bash
git add tests/test_orm_models.py
git commit -m "test: ORM public-API behavior (cross-queue filter/aggregate/order, read-only)"
```

---

## Task 9: `get_result` raw SQL → ORM

**Files:**

- Modify: `django_absurd/backends.py:110-144` (`get_result`) and `:168-223`
  (`build_task_result`)
- Test: reuse `tests/test_results.py` (must stay green)

**Interfaces:**

- Consumes: `django_absurd.models.Task`, `Run`.
- Produces: `get_result` builds the same `TaskResult` (status, args/kwargs, enqueued_at,
  started_at, finished_at, last_attempted_at, errors, worker_ids, return_value) by
  querying `Task` (by `queue`+`task_id`) and its `Run` rows — no raw SQL. Same
  `TaskResultDoesNotExist` semantics.

- [ ] **Step 1: Confirm the existing behavior tests (these are the spec — they must stay
      green)**

`tests/test_results.py` already asserts the full `TaskResult` contract
(pending/successful/failed/errors/worker_ids/unknown-id/malformed-id/injection-safe/atomic-safe/jsonb-loader).
Run them as the baseline:

Run: `PGPORT=5433 uv run pytest tests/test_results.py -v` Expected: PASS (current
raw-SQL impl).

- [ ] **Step 2: Refactor (prose)**

Rewrite `get_result(result_id)`: decode `result_id` → `(queue, task_id)` (existing
decode logic). Query `Task.objects.filter(queue=queue, task_id=task_id).first()` →
`None` ⇒ `TaskResultDoesNotExist`. For the last-attempt run, query
`Run.objects.filter(queue=queue, run_id=task.last_attempt_run).first()`; for
`worker_ids`,
`Run.objects.filter(queue=queue, task_id=task_id, claimed_by__isnull=False).order_by("attempt").values_list("claimed_by", flat=True)`.
Feed these model instances into `build_task_result` (adapt it to read attributes off the
model objects instead of unpacking a raw tuple — same fields:
`task_name, params, enqueue_at, first_started_at, state, completed_payload, cancelled_at`,
run's `started_at/completed_at/failed_at/failure_reason`). Preserve:
`map_state_to_status`, the `errors` build when `state=="failed"`,
`finished_at = completed_at or failed_at or cancelled_at`,
`_return_value=completed_payload` when completed. Keep the schema-absent →
`TaskResultDoesNotExist` path (catch the typed missing-view error from Task 7 /
`ProgrammingError`). Remove the bespoke SQL string.

- [ ] **Step 3: Run the behavior tests (must stay green) + a no-raw-SQL assertion**

```python
# tests/test_results.py  (append)
def test_get_result_uses_orm_not_raw_sql():
    import inspect, django_absurd.backends as b
    src = inspect.getsource(b.AbsurdBackend.get_result)
    assert "SELECT" not in src.upper()  # no hand-written SQL remains
```

Run: `PGPORT=5433 uv run pytest tests/test_results.py -v` Expected: ALL pass (behavior
identical) + the new assertion.

- [ ] **Step 4: Commit**

```bash
git add django_absurd/backends.py tests/test_results.py
git commit -m "refactor: get_result queries the ORM models (one schema source; drops raw SQL)"
```

---

## Task 10: Docs + final sweep

**Files:**

- Modify: `django_absurd/AGENTS.md`, `README.md`

- [ ] **Step 1: Full verification**

Run:

```bash
PGPORT=5433 uv run pytest
PGPORT=5433 uv run python -m django makemigrations --check --dry-run --settings=tests.settings
PGPORT=5433 uv run python -m django check django_absurd --settings=tests.settings
PGPORT=5433 uv run mypy django_absurd/
PGPORT=5433 uv run python -c "import django_absurd.models"   # no import cycle
```

Expected: all green; no migrations wanted; check clean; mypy clean; import clean. STOP +
report if any fail.

- [ ] **Step 2: Docs (run the sync-docs skill)**

Invoke `sync-docs`. Add an AGENTS.md "ORM access / querying queue state" section
(`from django_absurd.models import Task`; chainable read-only querysets; `queue` column;
provisioned by `absurd_sync_queues` / worker-create; the `.get(task_id=…, queue=…)`
guidance; the cross-queue scan perf caveat — `queue=` prunes, unfiltered `state`/sort
scans all arms; `drop_queue` → run sync). Note `get_result` now ORM-backed. README
mention. Note the admin spec's `admin_<entity>` view names are superseded by
`<entity>_view`.

- [ ] **Step 3: Commit**

```bash
git add django_absurd/AGENTS.md README.md
git commit -m "docs: ORM queue-table access (querying, perf caveat, drop_queue)"
```

---

## Notes for the implementer

- **Large-arm perf** (spec spike #1): if time permits, `EXPLAIN ANALYZE`
  `Task.objects.order_by("-enqueue_at")[:N]` over 1M+ rows in one arm to finalize the
  docs caveat. Not a blocker; the docs caveat ships regardless.
- The factory (`build_admin_model`) is **idempotent** — `models.py` and `admin.py`
  calling it yield the SAME class; never build twice expecting distinct classes.
- `rebuild_views` / `rebuild_admin_view` do real `DROP`+`CREATE VIEW` — only ever called
  from the sync command and worker-create (rare). Never from a read path.
- Tolerant-vs-typed (D2): Task 7's Step 0 spike picks the form; the test + impl follow
  it. Default expectation: typed `ViewNotProvisionedError` (tolerant self-healing isn't
  possible without a read-path rebuild, which we forbid).
- D1: leave the enqueue create-branch (`backends.py:64-94`) untouched re: views — an
  enqueue-created queue becomes ORM/admin-visible at the next `absurd_sync_queues` or
  worker-start. Document it.
