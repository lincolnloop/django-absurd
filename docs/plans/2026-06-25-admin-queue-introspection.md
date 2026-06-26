# Admin Queue Introspection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read-only Django admin over Absurd's per-queue tables — six admin entries
(Tasks, Runs, Checkpoints, Events, Waits, Queues) spanning all queues, queue as a
filter.

**Architecture:** One Postgres `UNION ALL` view per entity type (Approach D) stitches
every queue's `<prefix>_<queue>` table together with a synthesized `queue` column and a
single text surrogate pk `admin_pk`. Each view is mapped by an unmanaged Django model
defined against a PRIVATE `Apps()` registry (invisible to `makemigrations`), registered
with a read-only `ModelAdmin`. Views are rebuilt lazily (`DROP`+`CREATE`) when the live
queue catalog drifts.

**Tech Stack:** Django 6.0, Python 3.12, psycopg3, PostgreSQL (Absurd 0.4.0 schema),
pytest.

## Global Constraints

- Django 6.0 / Python 3.12 floor; psycopg (v3) backend only.
- `import typing as t` — never `from typing import X`. Absolute imports only (no
  relative).
- Functions contain a verb. No leading-underscore module constants/helpers. Helpers go
  BELOW the public function that uses them.
- System-check `msg` = the PROBLEM only; `hint` = the RESOLUTION only. Never duplicate.
- Tests: pytest, function-based only (never class-based). Autouse `_enable_db(db)`
  already grants DB access — do NOT add `@pytest.mark.django_db`; add
  `@pytest.mark.django_db(transaction=True)` only when a test needs commits/DDL. No
  `unittest.mock` / monkeypatch — drive branches with real DB conditions. Test
  commands/checks by RUNNING them and asserting full emitted text.
- READ-ONLY feature: no mutations of Absurd state.
- All DB work is lazy (admin-request time). No DB access at import / app-ready.
- `makemigrations` MUST stay clean.

**Spec:** `docs/specs/2026-06-25-admin-queue-introspection-design.md` (read it; this
plan implements it).

---

## File Structure

- **Create `django_absurd/admin_views.py`** — the DB/data layer: `EntitySpec` dataclass,
  `ADMIN_ENTITY_SPECS` constant, the private-`Apps` model factory (`build_admin_model` /
  `build_admin_models`), the view SQL builder (`build_union_view_sql` /
  `rebuild_admin_view`), the catalog read (`fetch_catalog_queues`) and the lazy refresh
  (`VIEW_BUILD_CACHE` + `ensure_view_current`).
- **Create `django_absurd/admin.py`** — the Django-admin layer: `ReadOnlyAbsurdAdmin`
  base, `AbsurdQueueListFilter`, `BoundedCountPaginator`, `register_absurd_admin`, and
  the autodiscovered module-level registration that reads OPTIONS via
  `get_absurd_backend()`. Imports from `admin_views`.
- **Modify `django_absurd/backends.py`** — add `ENABLE_ADMIN` + `ADMIN_SITE` to
  `AbsurdBackendOptions`; add `get_absurd_backend()`.
- **Modify `django_absurd/checks.py`** — add `check_absurd_admin_config` →
  `absurd.E006`.
- **Modify `tests/settings.py`**; **create `tests/urls.py`**; **modify
  `tests/conftest.py`** (admin scaffolding + superuser/staff fixtures); **create
  `tests/admin.py`** (a custom `AdminSite` for the ADMIN_SITE test).
- **Modify `django_absurd/AGENTS.md`, `README.md`** (docs ripple).

Test files: `tests/test_admin_backend_resolve.py`, `tests/test_admin_models.py`,
`tests/test_admin_views.py`, `tests/test_admin_refresh.py`, `tests/test_admin_http.py`,
`tests/test_admin_checks.py`.

---

## Task 0: Admin test scaffolding

**Files:**

- Modify: `tests/settings.py`
- Create: `tests/urls.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_admin_http.py` (smoke only this task)

**Interfaces:**

- Produces: settings with `django.contrib.admin/sessions/messages`, MIDDLEWARE,
  TEMPLATES, `ROOT_URLCONF="tests.urls"`, non-empty `SECRET_KEY`; pytest fixtures
  `admin_user` (superuser) and `staff_user` (is_staff, no perms); `tests/urls.py`
  exposing `admin.site.urls` at `/admin/`.

- [ ] **Step 1: Write the failing smoke test**

```python
# tests/test_admin_http.py
import pytest

pytestmark = pytest.mark.django_db(transaction=True)


def test_admin_login_page_renders(client):
    resp = client.get("/admin/login/")
    assert resp.status_code == 200
```

- [ ] **Step 2: Run it, verify it fails**

Run:
`PGPORT=5433 uv run pytest tests/test_admin_http.py::test_admin_login_page_renders -v`
Expected: FAIL (no `ROOT_URLCONF` / admin not installed → 404 or
`ImproperlyConfigured`).

- [ ] **Step 3: Add the scaffolding (no production code)**

In `tests/settings.py`: add `django.contrib.admin`, `django.contrib.sessions`,
`django.contrib.messages` to `INSTALLED_APPS`; add `MIDDLEWARE` (`SessionMiddleware`,
`CommonMiddleware`, `AuthenticationMiddleware`, `MessageMiddleware`); add a `TEMPLATES`
entry (`APP_DIRS=True`, context processors `request`, `auth`, `messages`); add
`ROOT_URLCONF = "tests.urls"`; add `SECRET_KEY = "test-only-not-secret"`. Create
`tests/urls.py` with `urlpatterns = [path("admin/", admin.site.urls)]`. In
`tests/conftest.py` add fixtures:

```python
@pytest.fixture
def admin_user(_enable_db):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.create_superuser("admin", "a@x.com", "pw")


@pytest.fixture
def staff_user(_enable_db):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.create_user("staff", "s@x.com", "pw", is_staff=True)
```

- [ ] **Step 4: Run it, verify it passes**

Run:
`PGPORT=5433 uv run pytest tests/test_admin_http.py::test_admin_login_page_renders -v`
Expected: PASS.

- [ ] **Step 5: Run the FULL existing suite — scaffolding must not regress anything**

Run: `PGPORT=5433 uv run pytest` Expected: all existing tests still PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/settings.py tests/urls.py tests/conftest.py tests/test_admin_http.py
git commit -m "test: admin scaffolding (settings, urlconf, user fixtures)"
```

---

## Task 1: `get_absurd_backend()` resolver + OPTIONS keys

**Files:**

- Modify: `django_absurd/backends.py:22` (TypedDict) and add `get_absurd_backend()`
- Test: `tests/test_admin_backend_resolve.py`

**Interfaces:**

- Produces:
  - `AbsurdBackendOptions` gains `ENABLE_ADMIN: bool` and `ADMIN_SITE: tuple[str, ...]`
    (both optional — TypedDict is `total=False`).
  - `get_absurd_backend() -> AbsurdBackend | None` — of the backends from
    `get_absurd_backends()`, returns the one whose
    `.database == resolve_absurd_database()`, taking the FIRST in `TASKS` insertion
    order when several share that DB; `None` if no `AbsurdBackend` is configured.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_admin_backend_resolve.py
import pytest
from django.test import override_settings

from django_absurd.backends import AbsurdBackend, get_absurd_backend

BACKEND = "django_absurd.backends.AbsurdBackend"


def test_returns_single_backend():
    be = get_absurd_backend()
    assert isinstance(be, AbsurdBackend)


@override_settings(TASKS={
    "a": {"BACKEND": BACKEND, "QUEUES": ["default"], "OPTIONS": {"ENABLE_ADMIN": False}},
    "b": {"BACKEND": BACKEND, "QUEUES": ["default"]},
})
def test_first_in_order_wins_when_sharing_db():
    # both on "default" → first declared ("a") wins
    be = get_absurd_backend()
    assert be.options.get("ENABLE_ADMIN") is False


@override_settings(TASKS={"x": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}})
def test_returns_none_without_absurd_backend():
    assert get_absurd_backend() is None
```

- [ ] **Step 2: Run, verify fail**

Run: `PGPORT=5433 uv run pytest tests/test_admin_backend_resolve.py -v` Expected: FAIL —
`ImportError: cannot import name 'get_absurd_backend'`.

- [ ] **Step 3: Implement (prose, no solution block)**

Add `ENABLE_ADMIN: bool` and `ADMIN_SITE: tuple[str, ...]` keys to the
`AbsurdBackendOptions` TypedDict at `backends.py:22`. Add a verb-named
`get_absurd_backend()` near `get_absurd_backends()`: read `resolve_absurd_database()`,
iterate `get_absurd_backends().values()` (insertion-ordered), return the first whose
`.database` equals it; return `None` if none. Import `resolve_absurd_database` (mind
import cycles — `backends.py` is imported by `queues.py`; if a cycle arises, do the
import inside the function body).

- [ ] **Step 4: Run, verify pass**

Run: `PGPORT=5433 uv run pytest tests/test_admin_backend_resolve.py -v` Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/backends.py tests/test_admin_backend_resolve.py
git commit -m "feat: get_absurd_backend resolver + admin OPTIONS keys"
```

---

## Task 2: Entity specs + private-registry model factory

**Files:**

- Create: `django_absurd/admin_views.py`
- Test: `tests/test_admin_models.py`

**Interfaces:**

- Produces:
  - `EntitySpec` (frozen dataclass): `name: str` (e.g. `"tasks"`), `prefix: str`
    (`"t"`), `view_name: str` (`"admin_tasks"`), `model_name: str` (`"AbsurdTask"`),
    `verbose: str` (`"task"`), `natural_key_sql: str` (the SQL after `queue || ':' ||`,
    e.g. `"task_id::text"` or `"task_id::text || ':' || checkpoint_name"`),
    `columns: tuple[tuple[str, str], ...]` (column name, one of
    `"uuid"|"text"|"int"|"jsonb"|"timestamptz"`), `has_state: bool`, `has_status: bool`,
    `list_display: tuple[str, ...]`, `search_fields: tuple[str, ...]`.
  - `ADMIN_ENTITY_SPECS: tuple[EntitySpec, ...]` — five specs
    (tasks/runs/checkpoints/events/waits) with columns copied from the spec's "Column
    specs" section.
  - `build_admin_model(spec: EntitySpec) -> type[models.Model]` — synthesizes an
    unmanaged model: `Meta.managed=False`, `Meta.app_label="django_absurd"`,
    `Meta.db_table=f'absurd"."{spec.view_name}'`, `Meta.apps=PRIVATE_ADMIN_APPS` (a
    module-level `Apps()`), `Meta.verbose_name`/`verbose_name_plural` from `spec`,
    fields = `admin_pk` (TextField primary_key), `queue` (TextField), plus one Django
    field per `spec.columns` entry (map `uuid→UUIDField`, `text→TextField`,
    `int→IntegerField`, `jsonb→JSONField`, `timestamptz→DateTimeField`; all `null=True`
    except the pk). `save`/`delete` raise (reuse `QueueReadOnlyError` from `models.py`,
    or a local `AdminViewReadOnlyError`).
  - `build_admin_models() -> dict[str, type[models.Model]]` — `spec.name → model` for
    all five.
  - `PRIVATE_ADMIN_APPS: Apps` — module-level private registry.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_admin_models.py
import pytest
from django.apps import apps as global_apps
from django.db import models

from django_absurd.admin_views import (
    ADMIN_ENTITY_SPECS,
    build_admin_model,
    build_admin_models,
)


def test_specs_cover_five_entities():
    names = {s.name for s in ADMIN_ENTITY_SPECS}
    assert names == {"tasks", "runs", "checkpoints", "events", "waits"}


def test_model_maps_schema_quoted_view_unmanaged():
    Tasks = build_admin_model(next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks"))
    assert Tasks._meta.db_table == 'absurd"."admin_tasks'
    assert Tasks._meta.managed is False
    assert Tasks._meta.pk.name == "admin_pk"
    assert isinstance(Tasks._meta.get_field("params"), models.JSONField)


def test_models_absent_from_global_registry():
    build_admin_models()
    names = {m.__name__ for m in global_apps.get_models() if m._meta.app_label == "django_absurd"}
    # only the real Queue model is global; synthesized models are private
    assert "AbsurdTask" not in names


def test_makemigrations_stays_clean():
    build_admin_models()
    from django.db.migrations.autodetector import MigrationAutodetector
    from django.db.migrations.loader import MigrationLoader
    from django.db.migrations.state import ProjectState

    loader = MigrationLoader(None, ignore_no_migrations=True)
    ad = MigrationAutodetector(loader.project_state(), ProjectState.from_apps(global_apps))
    changes = ad.changes(graph=loader.graph)
    assert changes.get("django_absurd", []) == []


def test_save_is_blocked():
    Tasks = build_admin_model(next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks"))
    with pytest.raises(Exception):
        Tasks().save()
```

- [ ] **Step 2: Run, verify fail**

Run: `PGPORT=5433 uv run pytest tests/test_admin_models.py -v` Expected: FAIL — module
`django_absurd.admin_views` does not exist.

- [ ] **Step 3: Implement (prose)**

Create `django_absurd/admin_views.py`. Define `PRIVATE_ADMIN_APPS = Apps()` at module
level. Define the `EntitySpec` frozen dataclass and the `ADMIN_ENTITY_SPECS` tuple,
transcribing columns/PK expressions from the spec (§Column specs, §Synthesized surrogate
pk). Implement `build_admin_model(spec)` per the Interfaces block — build a fresh dict
of field INSTANCES per call (Django fields bind to one model, so never share instances
across models), assemble a `Meta` via `type(...)`, then
`type(spec.model_name, (models.Model,), attrs)`. Implement `build_admin_models()`
looping the specs. Place helpers (field-type map, etc.) BELOW `build_admin_model`.
`import typing as t`, absolute imports.

- [ ] **Step 4: Run, verify pass**

Run: `PGPORT=5433 uv run pytest tests/test_admin_models.py -v` Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/admin_views.py tests/test_admin_models.py
git commit -m "feat: entity specs + private-registry admin model factory"
```

---

## Task 3: Union view SQL builder + rebuild

**Files:**

- Modify: `django_absurd/admin_views.py`
- Test: `tests/test_admin_views.py`

**Interfaces:**

- Consumes: `ADMIN_ENTITY_SPECS`, `build_admin_model` (Task 2).
- Produces:
  - `fetch_catalog_queues(using: str) -> list[str]` —
    `SELECT queue_name FROM absurd.queues ORDER BY queue_name`.
  - `build_union_view_sql(spec: EntitySpec, queues: list[str]) -> str` — emits
    `DROP VIEW IF EXISTS absurd.<view>; CREATE VIEW absurd.<view> AS ...`. Non-empty:
    one `UNION ALL` arm per queue, selecting `'<q>'::text AS queue`, the synthesized
    `admin_pk`, then `spec.columns`, `FROM absurd."<prefix>_<q>"`. Empty: typed-NULL
    columns + `WHERE false`. Identifiers quoted; queue literals are SQL string literals.
  - `rebuild_admin_view(spec: EntitySpec, queues: list[str], using: str) -> None` —
    execute the SQL inside `transaction.atomic(using=using)` via a cursor.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_admin_views.py
import uuid

import pytest
from django.core.management import call_command
from django.db import connections

from django_absurd.admin_views import (
    ADMIN_ENTITY_SPECS,
    build_admin_model,
    fetch_catalog_queues,
    rebuild_admin_view,
)
from tests.tasks import add

pytestmark = pytest.mark.django_db(transaction=True)

TASKS_SPEC = next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
CHECKS_SPEC = next(s for s in ADMIN_ENTITY_SPECS if s.name == "checkpoints")


def seed_two_queues():
    call_command("absurd_sync_queues")
    add.enqueue(2, 3)
    add.using(queue_name="other").enqueue(7, 8)
    call_command("absurd_worker", queue="default", burst=True)
    call_command("absurd_worker", queue="other", burst=True)


def test_zero_queue_view_is_empty():
    call_command("absurd_sync_queues")  # tables exist but no tasks
    rebuild_admin_view(TASKS_SPEC, [], "default")
    Tasks = build_admin_model(TASKS_SPEC)
    assert Tasks.objects.count() == 0


def test_union_spans_queues_and_filters():
    seed_two_queues()
    rebuild_admin_view(TASKS_SPEC, fetch_catalog_queues("default"), "default")
    Tasks = build_admin_model(TASKS_SPEC)
    assert {t.queue for t in Tasks.objects.all()} == {"default", "other"}
    assert Tasks.objects.filter(queue="other").count() == 1


def test_jsonb_decodes_and_pk_prefixed():
    seed_two_queues()
    rebuild_admin_view(TASKS_SPEC, fetch_catalog_queues("default"), "default")
    Tasks = build_admin_model(TASKS_SPEC)
    row = Tasks.objects.filter(queue="default", task_name="tests.tasks.add").first()
    assert isinstance(row.params, dict)
    assert row.admin_pk.startswith("default:")


def test_composite_pk_detail_lookup():
    seed_two_queues()
    tid = uuid.uuid4()
    with connections["default"].cursor() as cur:
        cur.execute(
            'INSERT INTO absurd."c_default" (task_id, checkpoint_name, state, status)'
            " VALUES (%s, %s, %s, 'committed')",
            [tid, "step/a:b c", '{"x": 1}'],
        )
    rebuild_admin_view(CHECKS_SPEC, fetch_catalog_queues("default"), "default")
    Checks = build_admin_model(CHECKS_SPEC)
    pk = f"default:{tid}:step/a:b c"
    assert Checks.objects.get(pk=pk).status == "committed"


def test_rebuild_after_drop_excludes_queue():
    seed_two_queues()
    rebuild_admin_view(TASKS_SPEC, ["default", "other"], "default")
    from django_absurd.queues import get_absurd_client
    get_absurd_client().drop_queue("other")
    rebuild_admin_view(TASKS_SPEC, fetch_catalog_queues("default"), "default")
    Tasks = build_admin_model(TASKS_SPEC)
    assert {t.queue for t in Tasks.objects.all()} == {"default"}
```

- [ ] **Step 2: Run, verify fail**

Run: `PGPORT=5433 uv run pytest tests/test_admin_views.py -v` Expected: FAIL —
`fetch_catalog_queues` / `rebuild_admin_view` not defined.

- [ ] **Step 3: Implement (prose)**

Add the three functions to `admin_views.py` per Interfaces. `build_union_view_sql`
assembles arms from `spec.columns` and `spec.natural_key_sql`; use `psycopg.sql`-style
quoting for table identifiers (`absurd"."<prefix>_<q>` pattern, mirroring `models.py`)
and proper SQL string literals for the `queue` constant. Zero-queue branch emits typed
NULLs matching each column's SQL type (so a later populated rebuild is type-compatible —
though DROP+CREATE means this is belt-and-braces). `rebuild_admin_view` wraps
`DROP`+`CREATE` in `transaction.atomic(using=using)`. Helpers below the public
functions.

- [ ] **Step 4: Run, verify pass**

Run: `PGPORT=5433 uv run pytest tests/test_admin_views.py -v` Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/admin_views.py tests/test_admin_views.py
git commit -m "feat: union view SQL builder + rebuild"
```

---

## Task 4: Lazy refresh (`ensure_view_current`)

**Files:**

- Modify: `django_absurd/admin_views.py`
- Test: `tests/test_admin_refresh.py`

**Interfaces:**

- Consumes: `fetch_catalog_queues`, `rebuild_admin_view` (Task 3).
- Produces:
  - `VIEW_BUILD_CACHE: dict[str, frozenset[str]]` — view_name → last-built queue set,
    per process.
  - `ensure_view_current(spec: EntitySpec, using: str) -> None` — read live catalog; if
    it differs from `VIEW_BUILD_CACHE.get(spec.view_name)`, `rebuild_admin_view` and
    update the cache. Guarded by a module-level `threading.Lock` so concurrent requests
    in one process don't double-build.
  - `reset_view_cache() -> None` — clears `VIEW_BUILD_CACHE` (test seam so a fresh
    process state can be simulated with real DB drift, NOT a mock).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_admin_refresh.py
import pytest
from django.core.management import call_command
from django.db import connections

from django_absurd.admin_views import (
    ADMIN_ENTITY_SPECS,
    build_admin_model,
    ensure_view_current,
    reset_view_cache,
)
from django_absurd.queues import get_absurd_client
from tests.tasks import add

pytestmark = pytest.mark.django_db(transaction=True)
TASKS_SPEC = next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")


def view_exists():
    with connections["default"].cursor() as cur:
        cur.execute("SELECT to_regclass('absurd.admin_tasks') IS NOT NULL")
        return cur.fetchone()[0]


def test_first_call_builds_view():
    reset_view_cache()
    call_command("absurd_sync_queues")
    assert view_exists() is False
    ensure_view_current(TASKS_SPEC, "default")
    assert view_exists() is True


def test_new_queue_picked_up_on_next_call():
    reset_view_cache()
    call_command("absurd_sync_queues")
    get_absurd_client().drop_queue("other")  # start with catalog = {default}
    ensure_view_current(TASKS_SPEC, "default")
    add.enqueue(2, 3)
    call_command("absurd_worker", queue="default", burst=True)
    Tasks = build_admin_model(TASKS_SPEC)
    assert {q for (q,) in Tasks.objects.values_list("queue").distinct()} == {"default"}
    # 'other' reappears in the catalog → next ensure rebuilds to include it
    call_command("absurd_sync_queues")
    add.using(queue_name="other").enqueue(7, 8)
    call_command("absurd_worker", queue="other", burst=True)
    ensure_view_current(TASKS_SPEC, "default")
    assert {q for (q,) in Tasks.objects.values_list("queue").distinct()} == {"default", "other"}


def test_dropped_queue_rebuild_excludes_it():
    reset_view_cache()
    call_command("absurd_sync_queues")
    ensure_view_current(TASKS_SPEC, "default")
    get_absurd_client().drop_queue("other")
    ensure_view_current(TASKS_SPEC, "default")  # catalog changed → rebuild
    assert view_exists() is True
```

- [ ] **Step 2: Run, verify fail**

Run: `PGPORT=5433 uv run pytest tests/test_admin_refresh.py -v` Expected: FAIL —
`ensure_view_current` / `reset_view_cache` not defined.

- [ ] **Step 3: Implement (prose)**

Add `VIEW_BUILD_CACHE`, a module-level `threading.Lock`, `ensure_view_current`, and
`reset_view_cache` to `admin_views.py`. `ensure_view_current`: under the lock, read
`fetch_catalog_queues(using)` as a `frozenset`; if it != cache entry,
`rebuild_admin_view` and store. Helpers below.

- [ ] **Step 4: Run, verify pass**

Run: `PGPORT=5433 uv run pytest tests/test_admin_refresh.py -v` Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/admin_views.py tests/test_admin_refresh.py
git commit -m "feat: lazy view refresh on catalog drift"
```

---

## Task 5: Read-only ModelAdmin (queryset/object/filter/paginator)

**Files:**

- Create: `django_absurd/admin.py`
- Test: `tests/test_admin_http.py` (extend)

**Interfaces:**

- Consumes: `ensure_view_current`, `build_admin_model`, `ADMIN_ENTITY_SPECS`,
  `fetch_catalog_queues` (Tasks 2-4); `get_absurd_backend` (Task 1).
- Produces:
  - `ReadOnlyAbsurdAdmin(admin.ModelAdmin)` —
    `has_add_permission`/`has_change_permission`/`has_delete_permission` → False;
    `has_view_permission` → True; `has_module_permission` → True; `get_readonly_fields`
    → all field names; `ordering = ("admin_pk",)`; `show_full_result_count = False`;
    `paginator = BoundedCountPaginator`. `get_queryset(request)`: call
    `ensure_view_current(spec, using)`, then return the model queryset; on
    `(ProgrammingError, OperationalError)` rebuild-once-and-retry, else return
    `model.objects.none()`. `get_object(request, object_id, ...)`: parse the leading
    `queue` segment off `object_id` and add a `queue=` filter alongside the pk lookup
    (arm pruning). Each admin instance carries its `EntitySpec` + `using`.
  - `AbsurdQueueListFilter(admin.SimpleListFilter)` — `parameter_name="queue"`, lookups
    from `fetch_catalog_queues(using)`, `queryset` applies `queue=value`.
  - `BoundedCountPaginator(Paginator)` — `count` bounded (e.g. `min(real, CAP)` via a
    `LIMIT CAP+1` subquery) so `COUNT(*)` never scans the whole union; `CAP` a module
    constant.
  - Tasks admin only: a readonly display method `runs_link(obj)` returning a safe `<a>`
    to the Runs changelist filtered to that task
    (`reverse(admin:django_absurd_absurdrun_changelist) + "?q=<task_id>"`), added to
    `readonly_fields`. Runs `search_fields` includes `task_id` so the link filters.

- [ ] **Step 1: Write the failing tests** (HTTP, exercises the ModelAdmin via a
      temporary registration in the test)

```python
# add to tests/test_admin_http.py
import pytest
from django.contrib import admin as djadmin
from django.core.management import call_command
from django.urls import reverse

from tests.tasks import add, boom

pytestmark = pytest.mark.django_db(transaction=True)


def seed():
    call_command("absurd_sync_queues")
    add.enqueue(2, 3)
    add.using(queue_name="other").enqueue(7, 8)
    boom.enqueue()
    call_command("absurd_worker", queue="default", burst=True)
    call_command("absurd_worker", queue="other", burst=True)


def test_tasks_changelist_unions_and_filters(client, admin_user):
    from django_absurd.admin import register_absurd_admin
    register_absurd_admin([djadmin.site])
    seed()
    client.force_login(admin_user)
    url = reverse("admin:django_absurd_absurdtask_changelist")
    body = client.get(url).content.decode()
    assert "tests.tasks.add" in body
    filtered = client.get(url, {"queue": "other"}).content.decode()
    assert "tests.tasks.boom" not in filtered  # boom is on default only
```

(Model/url names — `absurdtask` etc. — come from `EntitySpec.model_name`; keep them
consistent with Task 2.)

- [ ] **Step 2: Run, verify fail**

Run:
`PGPORT=5433 uv run pytest tests/test_admin_http.py::test_tasks_changelist_unions_and_filters -v`
Expected: FAIL — `django_absurd.admin` / `register_absurd_admin` not defined.

- [ ] **Step 3: Implement (prose)**

Create `django_absurd/admin.py`. Implement `ReadOnlyAbsurdAdmin`,
`AbsurdQueueListFilter`, `BoundedCountPaginator` per Interfaces. The admin needs its
`EntitySpec` + `using`; attach them as class attributes when the per-entity admin class
is built (in Task 6's `register_absurd_admin`, subclass `ReadOnlyAbsurdAdmin` per
model). For Task 5, implement `register_absurd_admin` minimally enough to register the
five synthesized models on a given site with a `ReadOnlyAbsurdAdmin` subclass carrying
the spec (Task 6 extends it with settings/Queue/sites handling). `get_object` parses
`object_id.split(":", 1)[0]` as the queue. Catch `from psycopg.errors import ...`? Use
Django's `django.db.utils.ProgrammingError`/`OperationalError`. Absolute imports,
`import typing as t`.

- [ ] **Step 4: Run, verify pass**

Run: `PGPORT=5433 uv run pytest tests/test_admin_http.py -v` Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/admin.py tests/test_admin_http.py
git commit -m "feat: read-only ModelAdmin with lazy refresh, queue filter, bounded paginator"
```

---

## Task 6: Registration + autodiscover (settings, sites, Queue, fail-soft)

**Files:**

- Modify: `django_absurd/admin.py`
- Create: `tests/admin.py` (a custom `AdminSite` for the test)
- Test: `tests/test_admin_http.py` (extend)

**Interfaces:**

- Consumes: `get_absurd_backend` (Task 1), `build_admin_models` (Task 2), `Queue`
  (`models.py`), `ReadOnlyAbsurdAdmin` (Task 5).
- Produces:
  - `register_absurd_admin(sites: t.Iterable[AdminSite]) -> None` — for each site,
    register the five synthesized models (each with a `ReadOnlyAbsurdAdmin` subclass
    carrying its `EntitySpec`+`using`) AND the `Queue` model (a read-only admin).
    Idempotent (skip already-registered).
  - `resolve_admin_sites() -> list[AdminSite]` — read `ADMIN_SITE` (default
    `("django.contrib.admin.site",)`) from `get_absurd_backend()`'s OPTIONS,
    `import_string` each; SKIP (fail-soft, no raise) any path that fails to import or
    isn't an `AdminSite`.
  - Module-level autodiscover block: if `get_absurd_backend()` exists and its
    `OPTIONS.get("ENABLE_ADMIN", True)` is truthy →
    `register_absurd_admin(resolve_admin_sites())`. Guarded so import never raises /
    never touches the DB.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_admin_http.py
from django.test import override_settings

BACKEND = "django_absurd.backends.AbsurdBackend"


def test_six_entries_registered_on_default_site():
    from django.contrib import admin as djadmin
    from django_absurd.admin import register_absurd_admin
    register_absurd_admin([djadmin.site])
    registered = {m._meta.model_name for m in djadmin.site._registry}
    assert {"absurdtask", "absurdrun", "absurdcheckpoint", "absurdevent",
            "absurdwait", "queue"} <= registered


def test_staff_user_sees_entries_in_index(client, staff_user):
    from django.contrib import admin as djadmin
    from django_absurd.admin import register_absurd_admin
    register_absurd_admin([djadmin.site])
    client.force_login(staff_user)
    body = client.get("/admin/").content.decode().lower()
    assert "absurdtask" in body  # has_module_permission override works


@override_settings(TASKS={"default": {"BACKEND": BACKEND, "QUEUES": ["default"],
                                       "OPTIONS": {"ADMIN_SITE": ("tests.admin.custom_site",)}}})
def test_custom_site_registration():
    from django_absurd.admin import resolve_admin_sites, register_absurd_admin
    from tests.admin import custom_site
    register_absurd_admin(resolve_admin_sites())
    assert any(m._meta.model_name == "absurdtask" for m in custom_site._registry)


@override_settings(TASKS={"default": {"BACKEND": BACKEND, "QUEUES": ["default"],
                                       "OPTIONS": {"ADMIN_SITE": ("nonexistent.module.site",)}}})
def test_bad_admin_site_fails_soft():
    from django_absurd.admin import resolve_admin_sites
    assert resolve_admin_sites() == []  # skipped, no raise
```

`tests/admin.py`:
`from django.contrib.admin import AdminSite; custom_site = AdminSite(name="custom")`.

- [ ] **Step 2: Run, verify fail**

Run:
`PGPORT=5433 uv run pytest tests/test_admin_http.py -k "registered or staff_user or custom_site or fails_soft" -v`
Expected: FAIL.

- [ ] **Step 3: Implement (prose)**

Extend `admin.py`: implement `resolve_admin_sites` (import_string +
isinstance(AdminSite) filter, fail-soft) and finalize `register_absurd_admin` to also
register `Queue` (read-only admin). Add the guarded module-level autodiscover block at
the BOTTOM of `admin.py` (so `admin.autodiscover()` triggers it): wrap in
`try/except Exception` returning silently if `get_absurd_backend()` is None or anything
import-time fails. Create `tests/admin.py`.

- [ ] **Step 4: Run, verify pass**

Run: `PGPORT=5433 uv run pytest tests/test_admin_http.py -v` Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/admin.py tests/admin.py tests/test_admin_http.py
git commit -m "feat: admin registration, custom sites, Queue entry, fail-soft autodiscover"
```

---

## Task 7: Full HTTP coverage (detail, read-only, task→runs, kill switch, degrade)

**Files:**

- Test: `tests/test_admin_http.py` (extend)

**Interfaces:**

- Consumes: everything from Tasks 5-6.

- [ ] **Step 1: Write the tests**

```python
# add to tests/test_admin_http.py
import uuid
from django.contrib.admin.utils import quote
from django.db import connections


def test_task_detail_renders(client, admin_user):
    from django.contrib import admin as djadmin
    from django_absurd.admin import register_absurd_admin
    register_absurd_admin([djadmin.site])
    seed()
    client.force_login(admin_user)
    from django_absurd.admin_views import build_admin_model, ADMIN_ENTITY_SPECS
    Tasks = build_admin_model(next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks"))
    obj = Tasks.objects.filter(queue="default", task_name="tests.tasks.add").first()
    url = reverse("admin:django_absurd_absurdtask_change", args=[quote(obj.admin_pk)])
    assert client.get(url).status_code == 200


def test_checkpoint_detail_with_nasty_name(client, admin_user):
    from django.contrib import admin as djadmin
    from django_absurd.admin import register_absurd_admin
    register_absurd_admin([djadmin.site])
    call_command("absurd_sync_queues")
    tid = uuid.uuid4()
    with connections["default"].cursor() as cur:
        cur.execute(
            'INSERT INTO absurd."c_default" (task_id, checkpoint_name, state, status)'
            " VALUES (%s, %s, %s, 'committed')",
            [tid, "step/a:b c", '{"x": 1}'],
        )
    client.force_login(admin_user)
    pk = f"default:{tid}:step/a:b c"
    url = reverse("admin:django_absurd_absurdcheckpoint_change", args=[quote(pk)])
    assert client.get(url).status_code == 200


def test_task_detail_has_runs_link(client, admin_user):
    from django.contrib import admin as djadmin
    from django_absurd.admin import register_absurd_admin
    register_absurd_admin([djadmin.site])
    seed()
    client.force_login(admin_user)
    from django_absurd.admin_views import build_admin_model, ADMIN_ENTITY_SPECS
    Tasks = build_admin_model(next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks"))
    obj = Tasks.objects.filter(queue="default", task_name="tests.tasks.add").first()
    url = reverse("admin:django_absurd_absurdtask_change", args=[quote(obj.admin_pk)])
    body = client.get(url).content.decode()
    runs_cl = reverse("admin:django_absurd_absurdrun_changelist")
    assert f"{runs_cl}?q={obj.task_id}" in body


def test_add_view_forbidden(client, admin_user):
    from django.contrib import admin as djadmin
    from django_absurd.admin import register_absurd_admin
    register_absurd_admin([djadmin.site])
    client.force_login(admin_user)
    url = reverse("admin:django_absurd_absurdtask_add")
    assert client.get(url).status_code in (403, 302)


def test_changelist_reflects_queue_added_after_first_load(client, admin_user):
    from django.contrib import admin as djadmin
    from django_absurd.admin import register_absurd_admin
    from django_absurd.queues import get_absurd_client
    register_absurd_admin([djadmin.site])
    call_command("absurd_sync_queues")
    get_absurd_client().drop_queue("other")  # first load sees only 'default'
    client.force_login(admin_user)
    cl = reverse("admin:django_absurd_absurdtask_changelist")
    first = client.get(cl, {"queue": "other"}).content.decode()
    assert "tests.tasks.add" not in first
    # recreate 'other' + a task on it → next load's lazy refresh must include it
    call_command("absurd_sync_queues")
    add.using(queue_name="other").enqueue(7, 8)
    call_command("absurd_worker", queue="other", burst=True)
    second = client.get(cl, {"queue": "other"}).content.decode()
    assert "tests.tasks.add" in second


def test_changelist_degrades_when_schema_absent(client, admin_user):
    from django.contrib import admin as djadmin
    from django_absurd.admin import register_absurd_admin
    register_absurd_admin([djadmin.site])
    call_command("migrate", "django_absurd", "zero", verbosity=0)  # drop absurd schema
    client.force_login(admin_user)
    cl = reverse("admin:django_absurd_absurdtask_changelist")
    assert client.get(cl).status_code == 200  # empty, not 500
    call_command("migrate", "django_absurd", verbosity=0)  # restore
```

- [ ] **Step 2: Run, verify status**

Run: `PGPORT=5433 uv run pytest tests/test_admin_http.py -v` Expected: tests covering
already-built behavior PASS; any FAIL pinpoints a real gap in Tasks 5-6 — fix the
implementation (not the test), re-run.

- [ ] **Step 3: Commit**

```bash
git add tests/test_admin_http.py
git commit -m "test: full admin HTTP coverage (detail, read-only, refresh, degrade)"
```

---

## Task 8: `absurd.E006` system check

**Files:**

- Modify: `django_absurd/checks.py`
- Test: `tests/test_admin_checks.py`

**Interfaces:**

- Consumes: `get_absurd_backend` (Task 1).
- Produces: `check_absurd_admin_config(app_configs, **kwargs) -> list[Error]` registered
  under `@register("absurd")`. Emits one `absurd.E006` per distinct problem:
  `ENABLE_ADMIN` not a bool; `ADMIN_SITE` not a tuple/list of str; an `ADMIN_SITE` path
  that fails `import_string`; a path resolving to a non-`AdminSite`. `msg` = the
  problem; `hint` = the resolution (a dotted path to an `AdminSite` instance / a bool).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_admin_checks.py
import pytest
from django.core.management import call_command
from django.test import override_settings

BACKEND = "django_absurd.backends.AbsurdBackend"


@override_settings(TASKS={"default": {"BACKEND": BACKEND, "QUEUES": ["default"],
                                      "OPTIONS": {"ADMIN_SITE": ("nonexistent.module.site",)}}})
def test_bad_admin_site_path_emits_e006(capsys):
    with pytest.raises(SystemExit):
        call_command("check", "django_absurd")
    assert "absurd.E006" in capsys.readouterr().err


@override_settings(TASKS={"default": {"BACKEND": BACKEND, "QUEUES": ["default"],
                                      "OPTIONS": {"ENABLE_ADMIN": "yes"}}})
def test_non_bool_enable_admin_emits_e006(capsys):
    with pytest.raises(SystemExit):
        call_command("check", "django_absurd")
    assert "absurd.E006" in capsys.readouterr().err


@override_settings(TASKS={"default": {"BACKEND": BACKEND, "QUEUES": ["default"],
                                      "OPTIONS": {"ADMIN_SITE": ("django.contrib.admin.site",)}}})
def test_valid_admin_config_no_e006(capsys):
    call_command("check", "django_absurd")
    assert "absurd.E006" not in capsys.readouterr().err
```

- [ ] **Step 2: Run, verify fail**

Run: `PGPORT=5433 uv run pytest tests/test_admin_checks.py -v` Expected: FAIL (no E006
emitted yet).

- [ ] **Step 3: Implement (prose)**

Add `check_absurd_admin_config` to `checks.py`, `@register("absurd")`. Read
`get_absurd_backend()`; if None return `[]`. Validate `ENABLE_ADMIN` type and each
`ADMIN_SITE` entry (tuple/list of str; `import_string` resolves; result is an
`AdminSite`). Build an `Error(..., id="absurd.E006")` per distinct problem with
non-duplicating msg/hint. Follow the existing E001-E005 construction style; helper below
the public function.

- [ ] **Step 4: Run, verify pass**

Run: `PGPORT=5433 uv run pytest tests/test_admin_checks.py -v` Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/checks.py tests/test_admin_checks.py
git commit -m "feat: absurd.E006 admin config check"
```

---

## Task 9: Docs + final sweep

**Files:**

- Modify: `django_absurd/AGENTS.md`, `README.md`

- [ ] **Step 1: Run the full suite + makemigrations + check**

Run:

```bash
PGPORT=5433 uv run pytest
PGPORT=5433 uv run python -m django makemigrations --check --dry-run --settings=tests.settings
PGPORT=5433 uv run python -m django check django_absurd --settings=tests.settings
```

Expected: all green; no migrations wanted; no unexpected check errors.

- [ ] **Step 2: Update docs (run the sync-docs skill)**

Invoke the `sync-docs` skill. It must: add `ENABLE_ADMIN` + `ADMIN_SITE` to the
AGENTS.md OPTIONS list; add an "Admin introspection" capability section (auto-on, six
entries, read-only, queue filter, the non-default-DB table-location note); add
`absurd.E006` to the checks list; add a short README mention. (`AbsurdBackendOptions`
TypedDict already updated in Task 1.)

- [ ] **Step 3: Commit**

```bash
git add django_absurd/AGENTS.md README.md
git commit -m "docs: admin introspection (OPTIONS, capability, E006)"
```

- [ ] **Step 4: Run lint/format + full matrix (optional but recommended)**

Run: `uvx --with tox-uv tox` (full Python×Django matrix + min-max mypy). Expected:
green.

---

## Notes for the implementer

- **psycopg connection for raw SQL**: use `connections[using].cursor()`. The Absurd
  JSONB loader is NOT needed for the ORM `JSONField` path (spike-confirmed) — do not add
  it.
- **Field instances are not shareable** across models — `build_admin_model` must create
  fresh field instances on every call.
- **Model/url names**: pin `EntitySpec.model_name` in Task 2 (`AbsurdTask`, `AbsurdRun`,
  `AbsurdCheckpoint`, `AbsurdEvent`, `AbsurdWait`); every admin URL reverse
  (`admin:django_absurd_<model_name_lower>_changelist/_change/_add`) depends on it.
- **`drop_queue` CASCADE drops the view itself** — `get_queryset`'s rebuild-and-retry is
  what makes the schema-absent / drop-race tests pass.
- **Do not** register the synthesized models in `models.py` or any module imported at
  app-ready — only in `admin.py` at autodiscover, and only via the private `Apps`
  registry.
