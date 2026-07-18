# Single Absurd Backend + Drop Schedule `alias` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement task-by-task. Steps use checkbox (`- [ ]`) tracking. Follow project TDD:
> **tests RED first**, then MINIMAL implementation described in prose — never paste a
> finished production-code block.

**Goal:** enforce exactly one Absurd backend per project (repurpose `absurd.E004`) and
remove the pg_cron-schedule `alias` (field, jobname segment, `--alias` flags, per-alias
scans, alias-charset rule, run-wrapper param).

**Architecture:** the framework `backend.alias` (TASKS key on `BaseTaskBackend`) STAYS —
the beat path (`task.using(backend=...)`) and `derive_idempotency_key` need it;
`scheduler.py` is untouched. We drop only the _schedule_ notion of alias. pg_cron app is
unreleased → its `0001_initial` is regenerated (no add-then-drop migration).

**Tech Stack:** Django 6 Tasks, psycopg3, pg_cron, pytest (3 suites), mypy strict, ruff.

**Source of truth:** `docs/specs/2026-07-17-single-absurd-backend-design.md` — its
**touchpoint table** and **tests delete-vs-edit** list are authoritative; consult them
per task.

## Global Constraints

- Python 3.12 floor / Django 6.0. mypy `strict`, ruff `ALL` + `ANN`/`E501` in tests.
- `import typing as t`, `import datetime as dt`, absolute imports, verb-named functions.
- Behavioral tests through real entrypoints (check via `call_command("check")`, commands
  via `call_command`, admin via HTTP); assert COMPLETE message text inline (don't import
  msg constants); alphabetize `@parametrize`.
- 100% patch coverage. Full matrix is CI's job; run **`tox -e py312-django60`** locally
  before pushing (3.14 dev masks version-compat bugs).
- pg_cron tests need `db_pg_cron`; a jobname/schema change → `--create-db` (see
  CLAUDE.md pg_cron eviction dance).
- Reference GitHub issues by full URL in code comments, never bare `#N`.

---

### Task 1: `E004` — forbid more than one Absurd backend

**Files:**

- Modify: `django_absurd/checks.py` (`E004_MSG`/`E004_HINT` ~49-58;
  `check_absurd_config` the `if len(databases) > 1` guard ~447-448)
- Test: `tests/core/test_checks.py`

**Interfaces:**

- Produces: `absurd.E004` now fires on `len(get_absurd_backends()) > 1` (any DB), msg
  `"django-absurd: more than one Absurd backend is configured."`, hint
  `"django-absurd uses a single Absurd backend per project — configure exactly one AbsurdBackend in TASKS."`

- [ ] **Step 1 — RED tests.** In `test_checks.py`, add two tests + repurpose the
      existing distinct-DB one. `build_tasks_setting` helper builds a one-backend TASKS;
      add a helper for two Absurd backends (two aliases) sharing DB `"default"`, and one
      for two on distinct DBs.

```python
def test_two_absurd_backends_same_db_error(settings, capsys):
    settings.TASKS = {
        "a": {"BACKEND": ABSURD, "OPTIONS": {"QUEUES": {}}},
        "b": {"BACKEND": ABSURD, "OPTIONS": {"QUEUES": {}}},
    }
    out = run_absurd_check(capsys, databases=["default"])
    assert "django-absurd: more than one Absurd backend is configured." in out

def test_two_absurd_backends_distinct_db_error(settings, capsys):
    # distinct DBs also error (was the old E004 case)
    ...
    assert "django-absurd: more than one Absurd backend is configured." in out

def test_single_absurd_backend_no_e004(settings, capsys):
    settings.TASKS = build_tasks_setting({"q": {}})
    assert "more than one Absurd backend" not in run_absurd_check(capsys, databases=["default"])
```

Delete/replace the old `test_multiple_backends_distinct_db_errors` (its distinct-DB
framing is now one case of the general rule).

- [ ] **Step 2 — run, expect FAIL** (current E004 keys on distinct DBs, so same-DB-two
      passes today): `uv run pytest tests/core/test_checks.py -k backends -v` → the
      same-DB test FAILs.
- [ ] **Step 3 — implement (prose).** In `check_absurd_config`, change the guard from
      counting distinct databases to counting backends: error when
      `len(get_absurd_backends())
  > 1`. Update `E004_MSG`/`E004_HINT`to the new strings. Remove the now-dead`databases`-set
  > computation if nothing else uses it.
- [ ] **Step 4 — run, expect PASS.** `uv run pytest tests/core/test_checks.py -q`.
- [ ] **Step 5 — commit.** `Forbid more than one Absurd backend (absurd.E004) (#63)`.

---

### Task 2: Commands — drop `--alias`

**Files:**

- Modify: `django_absurd/management/base.py` (`resolve_backend`),
  `management/commands/absurd_beat.py`, `absurd_worker.py`, `absurd_sync_crons.py`
- Test: `tests/core/test_worker.py`, `tests/core/test_scheduler.py`,
  `tests/pg_cron/test_absurd_sync_crons_command.py`

**Interfaces:**

- Consumes: E004 (Task 1) forbids >1 backend.
- Produces: `resolve_backend()` returns the single `AbsurdBackend` (no alias tuple
  element); `0` backends → `CommandError("No Absurd backend configured.")`. Commands
  expose no `--alias`. Message/prompt strings read `backend.alias`.

- [ ] **Step 1 — RED tests.** Delete
      `test_worker.py::test_ambiguous_alias_requires_flag` and
      `test_scheduler.py::test_absurd_beat_multiple_backends_requires_alias` (they test
      the removed multi-backend disambiguation). Add:

```python
def test_worker_rejects_alias_flag(settings):
    with pytest.raises(CommandError):
        call_command("absurd_worker", "--alias", "default", burst=True)  # unrecognized arg

def test_worker_uses_single_backend_at_nondefault_alias(settings):
    settings.TASKS = {"myabsurd": {"BACKEND": ABSURD, "QUEUES": ["default"]}}
    make_group.enqueue("x"); call_command("absurd_worker", burst=True)
    assert Group.objects.filter(name="x").exists()

def test_worker_no_backend_errors(settings):
    settings.TASKS = {"default": {"BACKEND": "django.tasks.backends.dummy.DummyBackend"}}
    with pytest.raises(CommandError, match="No Absurd backend configured"):
        call_command("absurd_worker", burst=True)
```

(`--alias` no longer parsed → argparse errors → `CommandError`.)

- [ ] **Step 2 — run, expect FAIL.** `uv run pytest tests/core/test_worker.py -q`.
- [ ] **Step 3 — implement (prose).** In `base.py`, rewrite `resolve_backend(options)` →
      `resolve_backend()` returning just the backend: `get_absurd_backends()` → if
      exactly one, return it; if zero,
      `raise CommandError("No Absurd backend configured.")`; the `>1` case can't occur
      (E004) — keep a defensive same error. Remove the `alias`/`options["alias"]` logic.
      In each command: delete the `add_argument("--alias", ...)`; update `handle` to
      call `resolve_backend()`; where a message/prompt used `alias`, read
      `backend.alias`.
- [ ] **Step 4 — run, expect PASS** (each suite separately):
      `tests/core/test_worker.py`, `tests/core/test_scheduler.py`,
      `tests/pg_cron/test_absurd_sync_crons_command.py`.
- [ ] **Step 5 — commit.** `Drop --alias; commands use the single Absurd backend (#63)`.

---

### Task 3: Remove the pg_cron schedule `alias` (cohesive)

This is one atomic change — jobname, model field, scans, run-wrapper, migration and
admin must land together for green (the regenerated migration ties them). Consult the
spec touchpoint table for the exact ~8 admin references.

**Files:**

- Modify: `django_absurd/pg_cron/validators.py` (`build_jobname`,
  `build_jobname_prefix`; DELETE `validate_alias_charset`), `pg_cron/checks.py` (DELETE
  `check_pg_cron_alias` + `E007_HINT_PG_CRON_ALIAS`; stop threading `alias`; fix
  `E007_HINT_PG_CRON_JOBNAME` example), `pg_cron/models.py` (DELETE
  `ScheduledTask.alias`, `get_pg_cron_alias_choices`,
  `validate_alias_is_pg_cron_backend`; `unique_together (source, name)`; drop `alias`
  from `get_managed_jobs`/`unschedule_matching`/`prune_jobs_without_rows`;
  `schedule_pg_cron_job` run-wrapper call drops positional `alias`), `pg_cron/admin.py`
  (scrub all `alias` refs; `clean()` resolves the single backend),
  `pg_cron/validators.py`/`models.py` imports.
- Regenerate: `django_absurd/pg_cron/migrations/0001_initial.py` (no `alias` column;
  `unique_together (source, name)`; run-wrapper
  `django_absurd_run_scheduled(p_source, p_name)` with no
  `p_alias`/`AND alias = p_alias`; `DROP FUNCTION …(text, text)`).
- Test: `tests/pg_cron/test_pg_cron_naming.py`, `test_pg_cron_sync_jobs.py`,
  `test_pg_cron_post_migrate.py`, `test_run_scheduled_fn.py`,
  `test_scheduledtask_model.py`, `test_pg_cron_checks.py`,
  `validators/test_jobname_length.py`, `test_admin/test_scheduledtask.py`.

**Interfaces:**

- Consumes: single-backend guarantee (Tasks 1-2).
- Produces: `build_jobname(name, source=Source.SETTINGS) -> "_dj:{source}:{name}"`;
  `build_jobname_prefix(source=Source.SETTINGS) -> "_dj:{source}:"`; `ScheduledTask` has
  no `alias`, `unique_together = (("source", "name"),)`; SQL
  `django_absurd_run_scheduled(p_source text, p_name text)`.

- [ ] **Step 1 — RED tests (jobname + model + run-wrapper).**

```python
# test_pg_cron_naming.py
def test_build_jobname_settings():
    assert build_jobname("nightly") == "_dj:s:nightly"
def test_build_jobname_admin():
    assert build_jobname("nightly", source="a") == "_dj:a:nightly"
def test_build_jobname_prefix():
    assert build_jobname_prefix() == "_dj:s:"

# test_scheduledtask_model.py — uniqueness now (source, name)
def test_unique_per_source_name(settings):
    ...  # same (source, name) twice → IntegrityError/ValidationError; different source OK
```

DELETE: `test_run_scheduled_fn.py::test_disambiguation_by_alias`,
`test_admin/test_scheduledtask.py::test_add_view_alias_field_labeled_alias`, the
bad-alias-charset cases in `test_pg_cron_checks.py`. EDIT jobname assertions across
`test_pg_cron_sync_jobs.py`/`test_pg_cron_post_migrate.py`/`test_absurd_sync_crons_command.py`
to `_dj:s:{name}`; run-wrapper positional callers in `test_run_scheduled_fn.py`/
`test_pg_cron_post_migrate.py` to 2 args; `test_jobname_length.py` +
`test_pg_cron_checks.py` budget (shorter prefix — hardcoded byte counts, update
comments + boundary values); `test_admin/test_scheduledtask.py` remove alias refs,
uniqueness-error string → "Source and Name".

- [ ] **Step 2 — run, expect FAIL** (rebuild pg_cron DB, jobname/schema changed): use
      the CLAUDE.md pg_cron `--create-db` eviction dance, then
      `uv run pytest tests/pg_cron --create-db -q` → FAILs on old `_dj:s:default:name`
      names + `alias` field.
- [ ] **Step 3 — implement (prose).**
  - `validators.py`: `build_jobname`/`build_jobname_prefix` drop the `alias` param and
    the `{alias}:` segment. Delete `validate_alias_charset`.
  - `checks.py` (pg_cron): delete `check_pg_cron_alias` + `E007_HINT_PG_CRON_ALIAS`;
    remove `alias` from the signatures/calls that thread it
    (`validate_pg_cron_schedule`, `check_pg_cron_name`); update
    `E007_HINT_PG_CRON_JOBNAME`'s `_dj:s:<alias>:<name>` example to `_dj:s:<name>`.
  - `models.py`: delete the `alias` field, `get_pg_cron_alias_choices`,
    `validate_alias_is_pg_cron_backend`; set `unique_together = (("source", "name"),)`;
    drop the `alias` param from
    `get_managed_jobs`/`unschedule_matching`/`prune_jobs_without_rows` (scan
    `_dj:{source}:`/`_dj:`); in `schedule_pg_cron_job` (`:283`) drop the positional
    `alias` from the `django_absurd_run_scheduled(...)` command it builds.
  - `admin.py`: remove `alias` from the form field, create-form `fields`, fieldsets,
    `ordering`, `list_display`, `list_filter`, `readonly`; rewrite `clean()` to resolve
    the single backend (via `get_absurd_backend()`) instead of
    `get_absurd_backends()[alias]`.
  - Regenerate `0001_initial`: drop the pg_cron test DB (eviction dance), delete the
    migration file, `uv run python -m manage makemigrations django_absurd_pg_cron` under
    a pg_cron settings module, then hand-port the raw-SQL `RunSQL` (run-wrapper without
    `p_alias`, `DROP FUNCTION …(text, text)`). Verify the generated model state has no
    `alias` + `unique_together (source, name)`.
- [ ] **Step 4 — run, expect PASS.** `uv run pytest tests/pg_cron --create-db -q`; then
      a normal `uv run pytest tests/pg_cron -q`.
- [ ] **Step 5 — commit.**
      `Remove pg_cron schedule alias: jobname, field, run-wrapper, migration (#63)`.

---

### Task 4: Docs + examples

**Files:**

- Modify: `django_absurd/AGENTS.md` (`:214` `--alias`; `:386` run-wrapper sig; `:415`
  "Backend (alias)"; `:428` "alias … immutable"; `:460-462` jobname),
  `docs/web/cron-jobs.md` (jobname + alias), `README.md` (if any alias ref).
- Verify: `examples/pg_cron`, `examples/beat` still run.

- [ ] **Step 1 — docs.** Update the named spots: remove `--alias`; run-wrapper →
      `django_absurd_run_scheduled(source, name)`; drop "Backend (alias)" / "alias
      immutable"; jobname → `_dj:s:<name>`. Cross-check command/flag/message text vs
      code. Build the site: `uvx zensical build` (expect "No issues found").
- [ ] **Step 2 — examples smoke.** No textual change expected (settings SCHEDULE,
      backend at `"default"`, no `--alias`). Re-run:
      `cd examples/pg_cron && docker compose up --build --abort-on-container-exit` (exit
      0), same for `examples/beat`.
- [ ] **Step 3 — commit.**
      `Docs + examples for single-backend / no schedule alias (#63)`.

---

## Self-Review

- **Spec coverage:** E004 (T1); `--alias`/`resolve_backend`/commands (T2); jobname,
  charset rule, model field + uniqueness, scans, run-wrapper, migration, admin (T3);
  docs + examples (T4). `backend.alias`/`scheduler.py` intentionally untouched (stated).
  ✓
- **Ordering:** T1 (check) → T2 (commands rely on single-backend) → T3 (pg_cron) → T4
  (docs) — each green independently; T3 is atomic by necessity (migration ties it).
- **Tests delete-vs-edit:** matches the spec's list; removed-behavior tests are deleted,
  not edited.
- **Coverage:** each task ends green + patch-covered; T3's deletions remove now-dead
  code so coverage holds.
