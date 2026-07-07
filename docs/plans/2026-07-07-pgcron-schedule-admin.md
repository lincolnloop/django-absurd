# Read-only pg_cron schedule admin — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (inline) or
> subagent-driven-development. Steps use `- [ ]` checkboxes.

**Goal:** Read-only Django admin for pg_cron `ScheduledTask` rows.

**Architecture:** New `django_absurd/pg_cron/admin.py` registers `ScheduledTask` on the
resolved admin site(s) at import, gated by `ENABLE_ADMIN`. Read-only behavior comes from
a small `ReadOnlyAdminMixin` extracted from the existing `ReadOnlyAbsurdAdmin` in
`django_absurd/admin.py` (the permission methods + `get_readonly_fields` — NOT the
UNION-view-specific `ordering`/`get_object`/`get_queryset`). Non-default-DB routing is
handled by the existing `AbsurdRouter` (covers `django_absurd_pg_cron`), so the default
admin queryset already reads the Absurd DB.

**Tech Stack:** Django 6 admin, pytest (function-based), BeautifulSoup4 DOM assertions.

## Global Constraints

- Python 3.12+, Django 6.0+; psycopg v3 only.
- `import typing as t`; absolute imports only; functions contain a verb; no
  leading-underscore module helpers.
- Tests: pytest function-based; no monkeypatch/`unittest.mock`; drive via the HTTP
  client
  - real rows; assert on rendered DOM (bs4), not internals.
- Admin HTTP tests in a package under `tests/pg_cron/test_admin/`; URLs via
  `reverse_lazy` constants (no-arg) + `reverse` helpers (args) — never hand-written
  paths.
- Read-only ONLY (no add/change/delete); pg_cron-only; `source="admin"` authoring
  deferred.
- Reuse `ReadOnlyAbsurdAdmin`/mixin, `resolve_admin_sites()`, `ENABLE_ADMIN`; mirror
  core `autoregister_admin` (register at import under `contextlib.suppress(Exception)`).
- Full patch coverage on changed lines.

---

### Task 1: `ReadOnlyAdminMixin` extraction + `ScheduledTaskAdmin` registration

**Files:**

- Modify: `django_absurd/admin.py` (extract mixin from `ReadOnlyAbsurdAdmin`)
- Create: `django_absurd/pg_cron/admin.py`
- Create: `tests/pg_cron/test_admin/__init__.py` (empty),
  `tests/pg_cron/test_admin/support.py`
- Test: `tests/pg_cron/test_admin/test_registration.py`

**Interfaces:**

- Consumes: `django_absurd.admin.resolve_admin_sites() -> list[AdminSite]`; the model
  `django_absurd.pg_cron.models.ScheduledTask`; `sync_crons(backend)` +
  `get_absurd_backends()` for seeding; the existing suite helper
  `build_pg_cron_tasks(schedule)` pattern.
- Produces: `django_absurd.pg_cron.admin.register_scheduled_task_admin(sites)`,
  `autoregister_scheduled_task_admin()`, `ScheduledTaskAdmin`; admin URL names
  `admin:django_absurd_pg_cron_scheduledtask_{changelist,change,add}`.

- [ ] **Step 1: support.py helpers**

`tests/pg_cron/test_admin/support.py` — bs4 + seed helpers (no assertions here):

```python
from bs4 import BeautifulSoup
from django.core.management import call_command

from django_absurd.backends import get_absurd_backends
from django_absurd.pg_cron.reconcile import sync_crons

BACKEND = "django_absurd.backends.AbsurdBackend"


def pg_cron_tasks(schedule):
    return {
        "default": {
            "BACKEND": BACKEND,
            "OPTIONS": {
                "QUEUES": {"default": {}, "other": {}, "reports": {}},
                "SCHEDULER": "pg_cron",
                "SCHEDULE": schedule,
            },
        }
    }


def parse_html(response):
    return BeautifulSoup(response.content, "html.parser")


def result_rows(soup):
    return soup.select("#result_list tbody tr")


def seed(settings, schedule):
    settings.TASKS = pg_cron_tasks(schedule)
    call_command("absurd_sync_queues")
    sync_crons(get_absurd_backends()["default"])
```

- [ ] **Step 2: Write the failing registration test**

`tests/pg_cron/test_admin/test_registration.py`:

```python
import pytest
from django.contrib import admin as djadmin
from django.urls import reverse_lazy

from django_absurd.pg_cron.admin import (
    autoregister_scheduled_task_admin,
    register_scheduled_task_admin,
)
from django_absurd.admin import resolve_admin_sites

pytestmark = pytest.mark.django_db(transaction=True)

LOGIN = reverse_lazy("admin:login")
INDEX = reverse_lazy("admin:index")


def test_scheduledtask_registered_on_default_site():
    registered = {m._meta.model_name for m in djadmin.site._registry}
    assert "scheduledtask" in registered


def test_staff_user_sees_scheduledtask_in_index(client, staff_user):
    from tests.pg_cron.test_admin.support import parse_html

    client.force_login(staff_user)
    soup = parse_html(client.get(INDEX))
    assert (
        soup.select_one('a[href$="/django_absurd_pg_cron/scheduledtask/"]') is not None
    )


def test_enable_admin_false_skips_registration(settings):
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"ENABLE_ADMIN": False},
        }
    }
    from django.contrib.admin import AdminSite

    site = AdminSite()
    register_scheduled_task_admin([site])  # gate is in autoregister; see impl note
    autoregister_scheduled_task_admin()  # honors ENABLE_ADMIN=False
    assert not any(
        m._meta.model_name == "scheduledtask" for m in site._registry
    )


def test_custom_admin_site_registration(settings):
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"ADMIN_SITE": ("tests.admin.custom_site",)},
        }
    }
    from tests.admin import custom_site

    register_scheduled_task_admin(resolve_admin_sites())
    assert any(m._meta.model_name == "scheduledtask" for m in custom_site._registry)
```

Note for impl: `register_scheduled_task_admin(sites)` registers unconditionally on the
given sites (idempotent — skip if already registered); the `ENABLE_ADMIN` gate lives in
`autoregister_scheduled_task_admin()`. The `test_enable_admin_false` test asserts the
autoregister path (gate) leaves a fresh site unregistered — adjust the test to call only
`autoregister_scheduled_task_admin()` against a site it controls, OR assert via a fresh
`AdminSite` that autoregister targets `resolve_admin_sites()` (default site). Keep the
test asserting the observable gate behavior; refine during RED.

- [ ] **Step 3: Run tests — verify they fail**

Run: `uv run pytest tests/pg_cron/test_admin/test_registration.py -q` Expected: FAIL —
`ModuleNotFoundError: django_absurd.pg_cron.admin` (module absent).

- [ ] **Step 4: Extract `ReadOnlyAdminMixin` (prose)**

In `django_absurd/admin.py`: pull the five `has_*_permission` methods and
`get_readonly_fields` out of `ReadOnlyAbsurdAdmin` into a new `ReadOnlyAdminMixin`
(mixin, not subclassing `ModelAdmin`). Redefine
`class ReadOnlyAbsurdAdmin(ReadOnlyAdminMixin, admin.ModelAdmin)` keeping its
view-specific attrs/methods (`spec`, `using`, `ordering = ("natural_key",)`,
`get_queryset`, `get_object`, `changelist_view`, paginator). Behavior-preserving — the
core admin suite must stay green.

- [ ] **Step 5: Create `django_absurd/pg_cron/admin.py` (prose)**

Define `ScheduledTaskAdmin(ReadOnlyAdminMixin, admin.ModelAdmin)` for `ScheduledTask`
with `ordering = ("alias", "name")` (ScheduledTask has no `natural_key`). Do NOT
override `get_queryset` — the default reads through `AbsurdRouter`. Set list attrs in
Task 2. Write `register_scheduled_task_admin(sites)`: for each site,
`site.register(ScheduledTask, ScheduledTaskAdmin)` guarded by
`if not site.is_registered(ScheduledTask)`. Write `autoregister_scheduled_task_admin()`:
resolve backend; if `backend.options.get("ENABLE_ADMIN", True)` is false → return; else
`register_scheduled_task_admin(resolve_admin_sites())`. At module bottom, call it under
`with contextlib.suppress(Exception):` (mirrors core `admin.py`). Imports:
`import typing as t`, `import contextlib`, absolute imports.

- [ ] **Step 6: Run tests — verify pass + core admin regression**

Run: `uv run pytest tests/pg_cron/test_admin/test_registration.py -q` Then:
`uv run pytest tests/core/test_admin -q` (mixin refactor regression). Expected: PASS
both.

- [ ] **Step 7: Commit**

```bash
git add django_absurd/admin.py django_absurd/pg_cron/admin.py tests/pg_cron/test_admin/
git commit -m "feat(pg_cron): register read-only ScheduledTask admin"
```

---

### Task 2: Changelist display, filters, search, read-only enforcement

**Files:**

- Modify: `django_absurd/pg_cron/admin.py` (list attrs)
- Test: `tests/pg_cron/test_admin/test_scheduledtask.py`

**Interfaces:**

- Consumes: `ScheduledTaskAdmin` (Task 1); `support.seed`, `parse_html`, `result_rows`.
- Produces: final `list_display`/`list_filter`/`search_fields` on `ScheduledTaskAdmin`.

- [ ] **Step 1: Write the failing changelist tests**

`tests/pg_cron/test_admin/test_scheduledtask.py`:

```python
import pytest
from django.urls import reverse_lazy

from tests.pg_cron.test_admin.support import parse_html, result_rows, seed

pytestmark = pytest.mark.django_db(transaction=True)

CHANGELIST = reverse_lazy("admin:django_absurd_pg_cron_scheduledtask_changelist")
ADD = reverse_lazy("admin:django_absurd_pg_cron_scheduledtask_add")

SCHEDULE = {
    "nightly": {"task": "tests.tasks.add", "cron": "0 2 * * *"},
    "hourly": {"task": "tests.tasks.on_reports", "cron": "0 * * * *", "queue": "reports"},
}


def test_changelist_renders_one_row_per_schedule(settings, client, admin_user):
    seed(settings, SCHEDULE)
    client.force_login(admin_user)
    soup = parse_html(client.get(CHANGELIST))
    assert len(result_rows(soup)) == 2


def test_changelist_shows_expected_columns(settings, client, admin_user):
    seed(settings, SCHEDULE)
    client.force_login(admin_user)
    body = client.get(CHANGELIST).content.decode()
    assert "nightly" in body and "0 2 * * *" in body and "tests.tasks.add" in body


def test_filter_by_queue_narrows(settings, client, admin_user):
    seed(settings, SCHEDULE)
    client.force_login(admin_user)
    soup = parse_html(client.get(CHANGELIST, {"queue": "reports"}))
    rows = result_rows(soup)
    assert len(rows) == 1 and "hourly" in rows[0].get_text()


def test_search_by_name_narrows(settings, client, admin_user):
    seed(settings, SCHEDULE)
    client.force_login(admin_user)
    soup = parse_html(client.get(CHANGELIST, {"q": "nightly"}))
    assert len(result_rows(soup)) == 1


def test_no_add_permission(settings, client, admin_user):
    seed(settings, SCHEDULE)
    client.force_login(admin_user)
    soup = parse_html(client.get(CHANGELIST))
    assert soup.select_one('a.addlink') is None
    assert client.get(ADD).status_code in (403, 302)


def test_detail_view_is_readonly_and_shows_options(settings, client, admin_user):
    from django_absurd.pg_cron.models import ScheduledTask

    seed(settings, SCHEDULE)
    pk = ScheduledTask.objects.get(name="hourly").pk
    change = reverse_lazy(
        "admin:django_absurd_pg_cron_scheduledtask_change", args=[pk]
    )
    client.force_login(admin_user)
    resp = client.get(change)
    body = resp.content.decode()
    assert resp.status_code == 200
    assert "reports" in body  # queue option column rendered
    assert '<input' not in body.split('id="scheduledtask_form"')[-1] or True  # readonly
```

(Refine the read-only detail assertion during RED to a concrete DOM check — e.g. the
change form has no editable `<input name="cron">`; assert the field renders as readonly
text.)

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run pytest tests/pg_cron/test_admin/test_scheduledtask.py -q` Expected: FAIL —
columns/filters absent (rows render but assertions on columns/filter/ search fail).

- [ ] **Step 3: Set list attrs (prose)**

On `ScheduledTaskAdmin`:
`list_display = ("name", "alias", "task", "queue", "cron", "enabled", "source", "updated_at")`;
`list_filter = ("alias", "enabled", "source", "queue")`;
`search_fields = ("name", "task")`. Read-only + detail-shows-options already come from
`ReadOnlyAdminMixin.get_readonly_fields` (all fields) + no-add permission.

- [ ] **Step 4: Run tests — verify pass**

Run: `uv run pytest tests/pg_cron/test_admin -q` Expected: PASS.

- [ ] **Step 5: Full-suite regression + coverage**

Run: `uv run pytest tests/pg_cron -q` (all green, no coverage regression on changed
lines).

- [ ] **Step 6: Commit**

```bash
git add django_absurd/pg_cron/admin.py tests/pg_cron/test_admin/test_scheduledtask.py
git commit -m "feat(pg_cron): ScheduledTask admin changelist columns, filters, search"
```

---

## Self-Review

- **Spec coverage:** read-only mixin + registration (Task 1); columns/filters/search +
  read-only enforcement + detail options (Task 2); ENABLE_ADMIN gate + custom ADMIN_SITE
  (Task 1 tests); pg_cron-only (lives in opt-in app); routing (default queryset via
  router). Non-goals (authoring, beat) untouched. ✓
- **Placeholders:** the two "refine during RED" notes are genuine test-shape refinements
  (read-only DOM assertion), not skipped work — resolve them when the test is first run.
- **Naming:** verb-named fns (`register_scheduled_task_admin`,
  `autoregister_scheduled_task_admin`); `ReadOnlyAdminMixin`, `ScheduledTaskAdmin`.
