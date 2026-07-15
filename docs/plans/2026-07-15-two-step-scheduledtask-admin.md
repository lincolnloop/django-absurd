# Two-step ScheduledTask admin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the pg_cron `ScheduledTask` admin into a minimal create step + a full
change step (UserAdmin-style), resolving all spawn options from the task's decorators on
create.

**Architecture:** A shared `build_scheduled_fields` derives the spawn columns from a
task path; both the settings reconcile and the new admin create-form use it. The admin
gains a `ScheduledTaskCreateForm` (add) + the existing `ScheduledTaskForm` (change),
switched by `get_form`/`get_fieldsets` on `obj is None`. `queue` becomes non-blank
everywhere; the implicit blank-queue resolution in `model.clean()` is removed.

**Tech Stack:** Django 6.0, Python 3.12, psycopg3, pg_cron, absurd_sdk.

## Global Constraints

- Django 6.0 / Python 3.12 floor; psycopg (v3) backend.
- Function-based pytest only; no monkeypatch / `unittest.mock`; HTTP-test admin through
  the real request cycle; assert the COMPLETE error message; alphabetize `@parametrize`
  values.
- Full patch coverage (100% on added/changed lines) via real entrypoints.
- `import typing as t`; absolute imports only; verb-named functions; no
  leading-underscore module helpers.
- TextChoices live in `pg_cron/choices.py`.
- TDD RED-first. Tests are shown as real code; implementation steps are described in
  PROSE only — never a finished production-code block.
- pg_cron suite needs the `db_pg_cron` service; rebuild after a migration change with
  the eviction dance in CLAUDE.md, then `uv run pytest tests/pg_cron --create-db`.
- pg_cron migrations collapse into a single fresh `0001` (unreleased); re-add
  `CreateExtension("pg_cron")` (op 0) + the wrapper `RunSQL(CREATE_FN, DROP_FN)` + the
  `("django_absurd","0001_initial_0_4_0")` dependency after regenerating.
- Frozen-at-create: decorator edits never retroactively change existing rows.

---

### Task 1: Shared resolver `build_scheduled_fields`

**Files:**

- Modify: `django_absurd/pg_cron/reconcile.py` (`resolve_spawn_options`, `sync_crons`;
  add `build_scheduled_fields`)
- Modify: `tests/tasks.py` (add the combined-decorator fixture)
- Test: `tests/pg_cron/test_pg_cron_sync_rows.py`

**Interfaces:**

- Produces:
  `resolve_spawn_options(backend: AbsurdBackend, task_path: str) -> dict[str, t.Any]`
  (was `(backend, schedule)`);
  `build_scheduled_fields(backend: AbsurdBackend, task_path: str, *, queue_override: str = "") -> dict[str, t.Any]`
  returning keys
  `queue, max_attempts, retry_kind, retry_base_seconds, retry_factor, retry_max_seconds, cancellation_max_duration, cancellation_max_delay, headers, idempotency_key`
  (NOT `args`/`kwargs`).
- Consumes: `resolve_spawn_options`, `get_effective_queue` semantics already in
  `reconcile.py`.

- [ ] **Step 1: Add the combined-decorator fixture task**

In `tests/tasks.py` (imports `RetryStrategy`, `CancellationPolicy`,
`absurd_default_params` already present), append:

```python
@task(queue_name="reports")
@absurd_default_params(
    max_attempts=9,
    retry_strategy=RetryStrategy(kind="fixed", base_seconds=5),
    cancellation=CancellationPolicy(max_duration=45, max_delay=3),
)
def fully_specced():
    return "fully_specced"
```

- [ ] **Step 2: Write the failing test**

Add to `tests/pg_cron/test_pg_cron_sync_rows.py` (reuse its existing imports:
`sync_crons`, `get_absurd_backends`, `ScheduledTask`, and `build_pg_cron_tasks` from
`tests.pg_cron.utils`):

```python
def test_sync_materializes_decorator_derived_columns(settings):
    settings.TASKS = build_pg_cron_tasks(
        {"full": {"task": "tests.tasks.fully_specced", "cron": "0 2 * * *"}}
    )
    sync_crons(get_absurd_backends()["default"])
    row = ScheduledTask.objects.get(source="s", name="full")
    assert row.queue == "reports"
    assert row.max_attempts == 9
    assert row.retry_kind == "fixed"
    assert row.retry_base_seconds == 5
    assert row.cancellation_max_duration == 45
    assert row.cancellation_max_delay == 3
```

- [ ] **Step 3: Run test to verify it passes/fails**

Run:
`uv run pytest tests/pg_cron/test_pg_cron_sync_rows.py::test_sync_materializes_decorator_derived_columns -q`
Expected: PASS (the settings lane already derives these columns — this is the
characterization test that locks the behavior the refactor must preserve). If it FAILS,
the fixture or QUEUES set is wrong — fix before refactoring.

- [ ] **Step 4: Refactor + extract (implementation — prose)**

Change `resolve_spawn_options` to accept a `task_path: str` instead of a `Schedule` (its
body only ever used `schedule.task`); update its one internal
`import_string(schedule.task)` to `import_string(task_path)`. Add
`build_scheduled_fields(backend, task_path, *, queue_override="")`: resolve the
effective queue (the override when truthy, else the task's `queue_name`), call
`resolve_spawn_options(backend, task_path)`, and return the ten spawn columns by
splitting `retry_strategy`/`cancellation` into their typed sub-columns exactly as
`sync_crons` does today (`kind`→`retry_kind` with `or ""`, the numeric sub-keys via
`.get(...)`, `idempotency_key` with `or ""`). Rewrite the `sync_crons`
`update_or_create` `defaults` so the spawn columns come from
`build_scheduled_fields(backend, schedule.task, queue_override=schedule.queue)`, merged
with the schedule-owned keys (`task`, `args`, `kwargs`, `cron`, `enabled`). Keep
verb-named functions; no leading-underscore helpers.

- [ ] **Step 5: Run tests to verify still green**

Run:
`uv run pytest tests/pg_cron/test_pg_cron_sync_rows.py tests/pg_cron/test_pg_cron_sync_jobs.py tests/pg_cron/test_run_scheduled_fn.py -q`
Expected: PASS (refactor preserves settings-lane behavior).

- [ ] **Step 6: Commit**

```bash
git add django_absurd/pg_cron/reconcile.py tests/tasks.py tests/pg_cron/test_pg_cron_sync_rows.py
git commit -m "refactor(pg_cron): extract build_scheduled_fields, share it with sync_crons"
```

---

### Task 2: Two-step admin — create form + resolution

**Files:**

- Modify: `django_absurd/pg_cron/admin.py` (add `ScheduledTaskCreateForm`,
  `add_fieldsets`, `get_form`, `get_fieldsets`, create-save resolution)
- Test: `tests/pg_cron/test_admin/test_scheduledtask.py`

**Interfaces:**

- Consumes: `build_scheduled_fields` (Task 1).
- Produces: `ScheduledTaskCreateForm(forms.ModelForm)` with
  `Meta.fields = ("alias", "name", "task", "cron")`;
  `ScheduledTaskAdmin.get_form(request, obj=None, **kwargs)` returns the create form
  when `obj is None`; `ScheduledTaskAdmin.add_fieldsets`.

- [ ] **Step 1: Write the failing test (decorator-derived assignment — the load-bearing
      one)**

Add to `tests/pg_cron/test_admin/test_scheduledtask.py` (reuse `seed`, `ADD`,
`change_url`, `ScheduledTask`). Note the create POST carries ONLY the four create
fields:

```python
def test_create_resolves_all_spawn_options_from_task_decorators(
    settings, client, admin_user
):
    seed(settings)
    client.force_login(admin_user)
    response = client.post(
        ADD,
        {
            "alias": "default",
            "name": "fromdecorators",
            "task": "tests.tasks.fully_specced",
            "cron": "0 2 * * *",
        },
    )
    assert response.status_code == 302
    row = ScheduledTask.objects.get(name="fromdecorators")
    assert row.source == "a"
    assert row.queue == "reports"
    assert row.max_attempts == 9
    assert row.retry_kind == "fixed"
    assert row.retry_base_seconds == 5
    assert row.cancellation_max_duration == 45
    assert row.cancellation_max_delay == 3
    assert row.enabled is True
    assert ScheduledTask.pg_cron.get_job("default", "fromdecorators", "a") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run:
`uv run pytest tests/pg_cron/test_admin/test_scheduledtask.py::test_create_resolves_all_spawn_options_from_task_decorators -q`
Expected: FAIL — the current single form neither restricts to four fields nor resolves
the decorator params (e.g. `max_attempts` would be the backend default `5`, `retry_kind`
would be `""`), and/or the POST of only four fields errors.

- [ ] **Step 3: Add the create form + admin switch + resolution (implementation —
      prose)**

Add `ScheduledTaskCreateForm(forms.ModelForm)` with `Meta.model = ScheduledTask` and
`Meta.fields = ("alias", "name", "task", "cron")`. In its `__init__`, pin
`self.instance.source = ScheduledTask.Source.ADMIN` (new instance) and, when >1 pg_cron
backend, keep `alias` a `<select>` of pg_cron aliases (the model field's `choices`
already do this — no rebuild needed); with a single backend the lone alias is
preselected. Resolve on save: override the form's `save(commit=...)` to set the spawn
columns on `self.instance` from
`build_scheduled_fields(get_absurd_backends()[self.instance.alias], self.instance.task)`
(no queue override — creation always inherits the task's queue), leaving `args`/`kwargs`
at their field defaults, then call `super().save()`. On `ScheduledTaskAdmin`: add
`add_fieldsets` = one section `("alias", "name", "task", "cron")`; add
`get_form(self, request, obj=None, **kwargs)` returning `ScheduledTaskCreateForm` (via
`super().get_form` with `form=`) when `obj is None`, else the existing form; add
`get_fieldsets(self, request, obj=None)` returning `add_fieldsets` when `obj is None`.
Ensure the created row's `post_save` still fires (it schedules the pg_cron job) — it
does, since the instance is saved normally. Keep upfront validation: the create form's
`full_clean` runs `model.clean()`, which already validates the task's effective queue is
declared, the cron grammar, jobname length, and uniqueness.

- [ ] **Step 4: Run test to verify it passes**

Run:
`uv run pytest tests/pg_cron/test_admin/test_scheduledtask.py::test_create_resolves_all_spawn_options_from_task_decorators -q`
Expected: PASS.

- [ ] **Step 5: Write the create-form shape + rejection tests**

```python
def test_add_view_renders_only_the_minimal_fields(settings, client, admin_user):
    seed(settings)
    client.force_login(admin_user)
    soup = BeautifulSoup(client.get(ADD).content, "html.parser")
    names = {i.get("name") for i in soup.select("form#scheduledtask_form [name]")}
    assert {"alias", "name", "task", "cron"} <= names
    assert "max_attempts" not in names
    assert "retry_kind" not in names


def test_create_with_undeclared_task_queue_is_form_error_not_created(
    settings, client, admin_user
):
    # tests.tasks.routed has queue_name="other"; a backend that does not declare "other"
    # must reject the schedule at create with the declared-queue message.
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"QUEUES": {"default": {}}, "SCHEDULER": "pg_cron"},
        }
    }
    call_command("absurd_sync_queues")
    client.force_login(admin_user)
    response = client.post(
        ADD,
        {
            "alias": "default",
            "name": "badq",
            "task": "tests.tasks.routed",
            "cron": "0 2 * * *",
        },
    )
    assert response.status_code == 200
    assert "queue 'other' is not declared." in response.content.decode()
    assert not ScheduledTask.objects.filter(name="badq").exists()


def test_create_with_bad_cron_is_form_error_not_created(settings, client, admin_user):
    seed(settings)
    client.force_login(admin_user)
    response = client.post(
        ADD,
        {
            "alias": "default",
            "name": "badcron",
            "task": "tests.tasks.add",
            "cron": "not a cron",
        },
    )
    assert response.status_code == 200
    assert not ScheduledTask.objects.filter(name="badcron").exists()
```

- [ ] **Step 6: Run to verify pass**

Run: `uv run pytest tests/pg_cron/test_admin/test_scheduledtask.py -q` Expected: PASS.
If `test_add_view_renders_only_the_minimal_fields` mismatches the form id/selector,
inspect the rendered add page and adjust the selector to the real admin form (do not
weaken the "max_attempts/retry_kind absent" assertions).

- [ ] **Step 7: Reconcile the existing add-view tests (implementation — prose)**

The add view is now four fields, so audit the existing `test_scheduledtask.py` tests
that POST the full `ADD_PAYLOAD` to `ADD`. Extra POST keys are ignored by the create
form, so most still pass — but the ones whose _intent_ is a field no longer on the
create form must move to the change form or be dropped:
`test_posting_add_with_blank_args_kwargs_falls_back_to_defaults` and
`test_posting_add_with_blank_queue_inherits_task_queue` become change-form (or moot,
since create resolves queue and args/kwargs default) — re-express them against
`change_url` if the behavior still matters, else delete;
`test_posting_add_with_tampered_source_is_forced_to_admin` — source isn't a create
field, so retarget the tamper as a change-form POST. Keep
`test_posting_duplicate_admin_name_is_form_error_not_500` but POST only the four create
fields twice. Run the full `test_scheduledtask.py` after and make every test green with
intent intact.

Run: `uv run pytest tests/pg_cron/test_admin/test_scheduledtask.py -q` Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add django_absurd/pg_cron/admin.py tests/pg_cron/test_admin/test_scheduledtask.py
git commit -m "feat(pg_cron): minimal create form that resolves spawn options from the task"
```

---

### Task 3: `queue` non-blank; drop blank-resolution; regenerate migration

**Files:**

- Modify: `django_absurd/pg_cron/models.py` (`queue` field; delete
  `resolve_blank_queue`; `validate_against_backend`)
- Modify: `django_absurd/pg_cron/migrations/0001_initial.py` (regenerate)
- Test: `tests/pg_cron/test_admin/test_scheduledtask.py`,
  `tests/pg_cron/validators/test_declared_queue.py`

**Interfaces:**

- Consumes: create-form resolution (Task 2) guarantees a concrete queue at create;
  `sync_crons` (Task 1) guarantees it for settings.

- [ ] **Step 1: Write the failing test (blank queue rejected on the change form)**

Add to `tests/pg_cron/test_admin/test_scheduledtask.py`:

```python
def test_change_form_rejects_a_blank_queue(settings, client, admin_user):
    seed(settings)
    client.force_login(admin_user)
    client.post(
        ADD,
        {
            "alias": "default",
            "name": "needsqueue",
            "task": "tests.tasks.add",
            "cron": "0 3 * * *",
        },
    )
    row = ScheduledTask.objects.get(name="needsqueue")
    response = client.post(change_url(row.pk), {**ADD_PAYLOAD, "queue": ""})
    assert response.status_code == 200
    assert "This field is required." in response.content.decode()
```

- [ ] **Step 2: Run test to verify it fails**

Run:
`uv run pytest tests/pg_cron/test_admin/test_scheduledtask.py::test_change_form_rejects_a_blank_queue -q`
Expected: FAIL — `queue` is currently `blank=True`, so a blank submit is accepted (and
silently coerced), not a required-field error.

- [ ] **Step 3: Tighten the model + drop blank-resolution (implementation — prose)**

In `models.py`: change `queue` to drop `blank=True` and `default=""` (keep
`choices=get_declared_queue_choices`), so it is required and its form dropdown has no
empty option. Delete the `resolve_blank_queue` method and its call in
`validate_against_backend` (creation now resolves the queue explicitly via
`build_scheduled_fields`; settings likewise; the change form requires it) —
`validate_against_backend` keeps validating the concrete `queue` + `cron` against the
backend. Remove the now-unused `import_string` import if nothing else uses it.

- [ ] **Step 4: Regenerate the migration**

Delete `0001_initial.py`, run
`DJANGO_SETTINGS_MODULE=tests.pg_cron.settings uv run python -m django makemigrations django_absurd_pg_cron`,
then re-add `CreateExtension("pg_cron")` as op 0, the
`("django_absurd","0001_initial_0_4_0")` dependency, and the wrapper
`migrations.RunSQL(sql=CREATE_FN, reverse_sql=DROP_FN)` (final wrapper body —
retry_kind/cancellation_max_* branches — verbatim from the prior `0001`). Verify:
`DJANGO_SETTINGS_MODULE=tests.pg_cron.settings uv run python -m django makemigrations --check --dry-run django_absurd_pg_cron`
prints "No changes detected".

- [ ] **Step 5: Rebuild the test DB + run to verify pass**

Run the eviction dance, then:
`uv run pytest tests/pg_cron/test_admin/test_scheduledtask.py::test_change_form_rejects_a_blank_queue tests/pg_cron/validators/test_declared_queue.py --create-db -q`
Expected: PASS. Fix any declared-queue test that assumed a blank queue path.

- [ ] **Step 6: Commit**

```bash
git add django_absurd/pg_cron/models.py django_absurd/pg_cron/migrations/0001_initial.py tests/pg_cron/
git commit -m "feat(pg_cron): require a concrete queue; drop implicit blank-queue resolution"
```

---

### Task 4: Docs — two-step create flow

**Files:**

- Modify: `django_absurd/AGENTS.md`, `docs/web/cron-jobs.md`

- [ ] **Step 1: Update the docs (prose)**

In AGENTS.md's admin/scheduling section and `docs/web/cron-jobs.md`'s "Authoring
schedules in the admin" section, describe the two-step flow: the add form asks only for
backend/name/task/cron; on save the remaining spawn options (queue, max_attempts, retry,
cancellation, headers, idempotency) are resolved from the task's `@task` /
`@absurd_default_params` decorators and shown, editable, on the change form; a blank
queue is not allowed. Note the resolution is frozen at create (later decorator edits
don't change existing rows). Keep copy terse; don't narrate the old behavior.

- [ ] **Step 2: Build the site + verify**

Run: `uvx zensical build` Expected: "No issues found".

- [ ] **Step 3: Commit**

```bash
git add django_absurd/AGENTS.md docs/web/cron-jobs.md
git commit -m "docs: two-step pg_cron schedule admin (create resolves from the task)"
```

---

## Final verification (after all tasks)

- `uv run pytest tests/pg_cron -q` · `uv run pytest tests/core -q` ·
  `uv run pytest tests/multidb -q` — all green.
- `DJANGO_SETTINGS_MODULE=tests.pg_cron.settings uv run python -m django makemigrations --check --dry-run`
  — no changes.
- Confirm 100% patch coverage on the changed lines of `reconcile.py`, `admin.py`,
  `models.py`.
