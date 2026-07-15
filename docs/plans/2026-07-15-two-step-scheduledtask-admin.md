# Two-step ScheduledTask admin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the pg_cron `ScheduledTask` admin into a minimal create step + a full
change step (UserAdmin-style), resolving all spawn options from the task's decorators on
create; the row is created disabled and enabled by the user in step 2.

**Architecture:** A shared `build_scheduled_fields` derives the spawn columns from a
task path; both the settings reconcile and the new admin create form use it. The admin
gains a `ScheduledTaskCreateForm` (a subclass of `ScheduledTaskForm` with narrowed
fields) + the existing `ScheduledTaskForm` (change), switched by
`get_form`/`get_fieldsets` on `obj is None`; `response_add` redirects to the change
page. `queue` becomes non-blank everywhere; the implicit blank-queue resolution in
`model.clean()` is removed.

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
- Created rows are `enabled=False` (post_save schedules the job inactive); the user
  enables in step 2 — so a schedule never fires with empty `args`.

---

### Task 1: Shared resolver `build_scheduled_fields`

**Files:**

- Modify: `django_absurd/pg_cron/reconcile.py` (`resolve_spawn_options`, `sync_crons`;
  add `build_scheduled_fields`)
- Modify: `tests/tasks.py` (add the combined-decorator fixture)
- Modify: `tests/pg_cron/test_pg_cron_options.py` (its `resolve_spawn_options(be, s)`
  callers — signature change)
- Test: `tests/pg_cron/test_pg_cron_sync_rows.py`

**Interfaces:**

- Produces:
  `resolve_spawn_options(backend: AbsurdBackend, task_path: str) -> dict[str, t.Any]`
  (was `(backend, schedule)`);
  `build_scheduled_fields(backend: AbsurdBackend, task_path: str, *, queue_override: str | None = None) -> dict[str, t.Any]`
  returning keys
  `queue, max_attempts, retry_kind, retry_base_seconds, retry_factor, retry_max_seconds, cancellation_max_duration, cancellation_max_delay, headers, idempotency_key`
  (NOT `args`/`kwargs`).

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

- [ ] **Step 2: Write the characterization test**

`tests/pg_cron/test_pg_cron_sync_rows.py` defines its own local `build_tasks(schedule)`
helper (not `build_pg_cron_tasks`) and imports `sync_crons`, `get_absurd_backends`,
`ScheduledTask`. Match the file's existing style; add:

```python
def test_sync_materializes_decorator_derived_columns(settings):
    settings.TASKS = build_tasks(
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

If `build_tasks` there doesn't declare a `reports` queue, extend that helper's QUEUES
(or the test's settings) so `reports` is declared.

- [ ] **Step 3: Run to verify it passes (characterization — locks current behavior)**

Run:
`uv run pytest tests/pg_cron/test_pg_cron_sync_rows.py::test_sync_materializes_decorator_derived_columns -q`
Expected: PASS. The settings lane already derives these columns; this guards the
refactor. If it FAILS, fix the fixture/QUEUES before refactoring.

- [ ] **Step 4: Extract + refactor (implementation — prose)**

Change `resolve_spawn_options` to accept `task_path: str` (its body only used
`schedule.task`); update its one `import_string(schedule.task)` to
`import_string(task_path)`. Add
`build_scheduled_fields(backend, task_path, *, queue_override=None)`: effective queue =
`queue_override` when truthy else the task's `queue_name`; call
`resolve_spawn_options(backend, task_path)`; return the ten spawn columns, splitting
`retry_strategy`/`cancellation` into their typed sub-columns exactly as `sync_crons`
does today (`kind`→`retry_kind` with `or ""`, numeric sub-keys via `.get(...)`,
`idempotency_key` with `or ""`). Rewrite `sync_crons`'s `update_or_create` `defaults` to
take the spawn columns from
`build_scheduled_fields(backend, schedule.task, queue_override=schedule.queue)`, merged
with the schedule-owned keys (`task`, `args`, `kwargs`, `cron`, `enabled`). Update the
`test_pg_cron_options.py` callers from `resolve_spawn_options(be, s)` to
`resolve_spawn_options(be, s.task)`. Decide keep-or-fold `get_effective_queue` (if only
`test_pg_cron_options.py` still uses it, folding its logic into `build_scheduled_fields`
and updating that test is cleaner — but keeping it is acceptable). Verb-named functions;
no leading-underscore helpers.

- [ ] **Step 5: Run to verify still green**

Run:
`uv run pytest tests/pg_cron/test_pg_cron_sync_rows.py tests/pg_cron/test_pg_cron_sync_jobs.py tests/pg_cron/test_run_scheduled_fn.py tests/pg_cron/test_pg_cron_options.py -q`
Expected: PASS (refactor preserves settings-lane behavior; `test_pg_cron_options.py`
callers updated).

- [ ] **Step 6: Commit**

```bash
git add django_absurd/pg_cron/reconcile.py tests/tasks.py tests/pg_cron/test_pg_cron_sync_rows.py tests/pg_cron/test_pg_cron_options.py
git commit -m "refactor(pg_cron): extract build_scheduled_fields, share it with sync_crons"
```

---

### Task 2: Two-step admin — create form, resolution, disabled-on-create

**Files:**

- Modify: `django_absurd/pg_cron/admin.py`
- Modify: `tests/pg_cron/test_admin/test_scheduledtask.py`
- Modify: `tests/pg_cron/validators/utils.py` (rework the admin form subject)

**Interfaces:**

- Consumes: `build_scheduled_fields` (Task 1).
- Produces: `ScheduledTaskCreateForm(ScheduledTaskForm)` with
  `Meta.fields = ("alias", "name", "task", "cron")`;
  `ScheduledTaskAdmin.get_form`/`get_fieldsets`/`add_fieldsets`/`response_add`.

- [ ] **Step 1: Write the load-bearing test (decorator resolution + disabled +
      redirect), RED**

Add to `tests/pg_cron/test_admin/test_scheduledtask.py` (reuse `seed`, `ADD`,
`change_url`, `ScheduledTask`):

```python
def test_create_resolves_all_spawn_options_and_is_disabled(
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
    assert response["Location"] == change_url(row.pk)
    assert row.source == "a"
    assert row.enabled is False
    assert row.queue == "reports"
    assert row.max_attempts == 9
    assert row.retry_kind == "fixed"
    assert row.retry_base_seconds == 5
    assert row.cancellation_max_duration == 45
    assert row.cancellation_max_delay == 3
```

- [ ] **Step 2: Run to verify it fails**

Run:
`uv run pytest tests/pg_cron/test_admin/test_scheduledtask.py::test_create_resolves_all_spawn_options_and_is_disabled -q`
Expected: FAIL — current form saves `max_attempts` NULL (omitted), `retry_kind` `""`,
doesn't resolve decorators, redirects to the changelist (not the change page), and
`enabled` handling differs.

- [ ] **Step 3: Build the create form + admin switch + resolution (implementation —
      prose)**

Add `class ScheduledTaskCreateForm(ScheduledTaskForm)` overriding only
`Meta.fields = ("alias", "name", "task", "cron")` — subclassing inherits the
`validate_unique` override (so a duplicate `(source, alias, name)` is a form error, not
a 500), the `source` pinning, and the harmless `clean_args`/`clean_kwargs`. Override
`_post_clean`: when `cleaned_data` has both `alias` and `task` (i.e. they validated),
set the spawn columns on `self.instance` from
`build_scheduled_fields(get_absurd_backends()[cleaned_data["alias"]], cleaned_data["task"])`
and `self.instance.enabled = False`, THEN call `super()._post_clean()` — so the resolved
columns pass `full_clean`/`model.clean()` (e.g. `max_attempts >= 1`). `args`/`kwargs`
stay at field defaults. On `ScheduledTaskAdmin`:
`add_fieldsets = ((None, {"fields": ("alias", "name", "task", "cron")}),)`;
`get_form(self, request, obj=None, **kwargs)` passes `form=ScheduledTaskCreateForm` to
`super().get_form` when `obj is None`; `get_fieldsets(self, request, obj=None)` returns
`add_fieldsets` when `obj is None`;
`response_add(self, request, obj, post_url_continue=None)` redirects to the change page
(return `super().response_add` with `post_url_continue` defaulted, or
`HttpResponseRedirect(reverse("admin:django_absurd_pg_cron_scheduledtask_change", args=[obj.pk]))`).
Do NOT override `save()` — resolution lives in `_post_clean`, so the admin saves once
and `post_save` fires once (scheduling the job inactive because `enabled=False`).

- [ ] **Step 4: Run to verify it passes**

Run:
`uv run pytest tests/pg_cron/test_admin/test_scheduledtask.py::test_create_resolves_all_spawn_options_and_is_disabled -q`
Expected: PASS.

- [ ] **Step 5: Write the create-shape, rejection, and parity tests**

```python
def test_add_view_renders_only_the_minimal_fields(settings, client, admin_user):
    seed(settings)
    client.force_login(admin_user)
    soup = BeautifulSoup(client.get(ADD).content, "html.parser")
    names = {i.get("name") for i in soup.select("#scheduledtask_form [name]")}
    assert {"alias", "name", "task", "cron"} <= names
    assert "max_attempts" not in names
    assert "retry_kind" not in names
    assert "queue" not in names


def test_create_with_undeclared_task_queue_is_form_error_not_created(
    settings, client, admin_user
):
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
        {"alias": "default", "name": "badq", "task": "tests.tasks.routed",
         "cron": "0 2 * * *"},
    )
    assert response.status_code == 200
    assert "queue 'other' is not declared." in response.content.decode()
    assert not ScheduledTask.objects.filter(name="badq").exists()


def test_create_with_bad_cron_is_form_error_not_created(settings, client, admin_user):
    seed(settings)
    client.force_login(admin_user)
    response = client.post(
        ADD,
        {"alias": "default", "name": "badcron", "task": "tests.tasks.add",
         "cron": "not a cron"},
    )
    assert response.status_code == 200
    assert not ScheduledTask.objects.filter(name="badcron").exists()


def test_create_and_sync_produce_identical_spawn_columns(settings, client, admin_user):
    # parity: admin-create and settings sync_crons of the same task resolve the same
    # columns (guards the shared build_scheduled_fields).
    seed(settings)
    client.force_login(admin_user)
    client.post(
        ADD,
        {"alias": "default", "name": "fully_specced", "task": "tests.tasks.fully_specced",
         "cron": "0 2 * * *"},
    )
    from django_absurd.pg_cron.reconcile import sync_crons  # noqa: PLC0415
    sync_crons(get_absurd_backends()["default"])
    admin_row = ScheduledTask.objects.get(source="a", name="fully_specced")
    # settings row: seed()'s SCHEDULE must include a fully_specced entry; assert equal
    cols = ("queue", "max_attempts", "retry_kind", "retry_base_seconds",
            "cancellation_max_duration", "cancellation_max_delay")
    settings_row = ScheduledTask.objects.get(source="s", name="hourly")  # adjust to a seed entry
    # simplest parity: compare admin_row columns to build_scheduled_fields directly
    from django_absurd.pg_cron.reconcile import build_scheduled_fields  # noqa: PLC0415
    expected = build_scheduled_fields(get_absurd_backends()["default"], "tests.tasks.fully_specced")
    for col in cols:
        assert getattr(admin_row, col) == expected[col]
```

(Implementer: simplify the parity test to whatever cleanly asserts admin-create columns
== `build_scheduled_fields(...)` for `fully_specced`; the seed cross-reference above is
illustrative, not required.)

- [ ] **Step 6: Reconcile the existing tests broken by the create form (implementation —
      prose)**

The add view is now four fields and `response_add` redirects to the change page. Fix
every existing test that assumes otherwise; run the whole file/suite green with intent
intact:

- **GET add-view field tests** (`test_scheduledtask.py`):
  `test_add_view_prefills_default_max_attempts`,
  `test_add_view_queue_is_a_dropdown_of_declared_queues`,
  `test_add_view_retry_kind_is_a_dropdown`,
  `test_add_view_cancellation_fields_are_number_inputs` select fields no longer on the
  add page → retarget each to `change_url` of a pre-created row. (The queue-dropdown
  expected values include the `""` empty option after Task 2; Task 3 removes it —
  sequence accordingly.)
- **POST add tests whose intent is a non-create field**:
  `test_posting_add_with_blank_args_kwargs_falls_back_to_defaults` and
  `test_posting_add_with_blank_queue_inherits_task_queue` → re-express against the
  change form (or delete if moot — create resolves queue, args/kwargs default).
  `test_posting_add_with_tampered_source_is_forced_to_admin` → retarget the tamper as a
  change-form POST. `test_posting_duplicate_admin_name_is_form_error_not_500` → POST
  only the four create fields twice.
- **Validators `form` subject** (`tests/pg_cron/validators/utils.py`):
  `validate_from_admin_post` currently POSTs a full payload to `ADD_URL`; the create
  form ignores non-create fields, so rules about
  `args`/`kwargs`/`queue`/retry/cancellation stop being exercised via the form. Rework
  it to POST the **change** form of a pre-seeded admin row (all those fields are
  editable there). Rules on fields read-only on the change form (`name`) or absent from
  both forms move their form-subject coverage to the check+model subjects
  (`validate_check_and_model`) — mirror how `test_alias_charset` already dropped its
  form subject. Audit each consumer of the `validate` fixture (`test_task`, `test_cron`,
  `test_name_charset`, `test_args_kwargs_shape`, `test_declared_queue`) and re-home as
  needed.

Run:
`uv run pytest tests/pg_cron/test_admin/test_scheduledtask.py tests/pg_cron/validators -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add django_absurd/pg_cron/admin.py tests/pg_cron/
git commit -m "feat(pg_cron): minimal create form; resolve spawn options from the task, disabled on create"
```

---

### Task 3: `queue` non-blank; drop blank-resolution; regenerate migration

**Files:**

- Modify: `django_absurd/pg_cron/models.py`
- Modify: `django_absurd/pg_cron/migrations/0001_initial.py`
- Modify: `tests/pg_cron/validators/utils.py` (the `VALID` baseline),
  `tests/pg_cron/validators/test_declared_queue.py`, and any dropdown test retargeted in
  Task 2 Step 6
- Test: `tests/pg_cron/test_admin/test_scheduledtask.py`

**Interfaces:**

- Consumes: create-form resolution (Task 2) + `sync_crons` (Task 1) both guarantee a
  concrete queue before save.

- [ ] **Step 1: Write the failing test (blank queue rejected on the change form)**

Add to `tests/pg_cron/test_admin/test_scheduledtask.py`:

```python
def test_change_form_rejects_a_blank_queue(settings, client, admin_user):
    seed(settings)
    client.force_login(admin_user)
    client.post(
        ADD,
        {"alias": "default", "name": "needsqueue", "task": "tests.tasks.add",
         "cron": "0 3 * * *"},
    )
    row = ScheduledTask.objects.get(name="needsqueue")
    response = client.post(change_url(row.pk), {**ADD_PAYLOAD, "queue": ""})
    assert response.status_code == 200
    assert "This field is required." in response.content.decode()
```

- [ ] **Step 2: Run test to verify it fails**

Run:
`uv run pytest tests/pg_cron/test_admin/test_scheduledtask.py::test_change_form_rejects_a_blank_queue -q`
Expected: FAIL — `queue` is currently `blank=True`, so a blank submit is accepted.

- [ ] **Step 3: Tighten the model + drop blank-resolution (implementation — prose)**

In `models.py`: drop `blank=True` and `default=""` on `queue` (keep
`choices=get_declared_queue_choices`) → required, dropdown has no empty option. Delete
`resolve_blank_queue` and its call in `validate_against_backend`
(`validate_against_backend` keeps validating the concrete `queue` + `cron`). Remove the
now-unused `import_string` import if nothing else in the module uses it.
(`validate_declared_queue`'s blank→effective branch stays — it's the shared check-path
rule.)

- [ ] **Step 4: Fix the validators baseline broken by `blank=False` (implementation —
      prose)**

`tests/pg_cron/validators/utils.py`'s `VALID` dict has `queue: ""`;
`validate_from_model` runs `ScheduledTask(**VALID).full_clean()` with no exclusions, so
`blank=False` now adds "This field cannot be blank." to every model-subject run. Set
`VALID["queue"] = "default"`. Then
`test_declared_queue.py::test_bad_task_no_queue_reports_task_not_queue` (whose whole
point is the blank-effective-queue branch) must pass an explicit `queue=""` override so
it still exercises that path. Any dropdown test retargeted in Task 2 that expected a
`""` option must drop it.

- [ ] **Step 5: Regenerate the migration**

Delete `0001_initial.py`, run
`DJANGO_SETTINGS_MODULE=tests.pg_cron.settings uv run python -m django makemigrations django_absurd_pg_cron`,
then re-add `CreateExtension("pg_cron")` (op 0), the
`("django_absurd","0001_initial_0_4_0")` dependency, and the wrapper
`RunSQL(CREATE_FN, DROP_FN)` (final wrapper body verbatim from the prior `0001`). Verify
`makemigrations --check --dry-run` prints "No changes detected".

- [ ] **Step 6: Rebuild the test DB + run the affected suites green**

Eviction dance, then:
`uv run pytest tests/pg_cron/test_admin/test_scheduledtask.py tests/pg_cron/validators --create-db -q`
Expected: PASS.

- [ ] **Step 7: Commit**

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
`@absurd_default_params` decorators; the row is created **disabled**, and the user
reviews the resolved values, fills `args`/`kwargs` if needed, and enables it on the
change page to go live; a blank queue is not allowed. Note resolution is frozen at
create (later decorator edits don't change existing rows). Terse; don't narrate old
behavior.

- [ ] **Step 2: Build the site + verify**

Run: `uvx zensical build` Expected: "No issues found".

- [ ] **Step 3: Commit**

```bash
git add django_absurd/AGENTS.md docs/web/cron-jobs.md
git commit -m "docs: two-step pg_cron schedule admin (disabled create, resolves from the task)"
```

---

## Final verification (after all tasks)

- `uv run pytest tests/pg_cron -q` · `uv run pytest tests/core -q` ·
  `uv run pytest tests/multidb -q` — all green.
- `DJANGO_SETTINGS_MODULE=tests.pg_cron.settings uv run python -m django makemigrations --check --dry-run`
  — no changes.
- 100% patch coverage on the changed lines of `reconcile.py`, `admin.py`, `models.py`.
