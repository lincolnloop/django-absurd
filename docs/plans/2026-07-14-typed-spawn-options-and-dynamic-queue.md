# Typed Spawn-Option Fields + Dynamic Queue Choices — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** replace the pg_cron `ScheduledTask`'s raw-JSON `retry_strategy`/`cancellation`
with typed, validated columns, and make `queue` a dynamic dropdown of declared queues.

**Architecture:** the row stores flat typed columns; the pg_cron wrapper
(`django_absurd_run_scheduled`) reassembles the `RetryStrategy`/`CancellationPolicy`
jsonb Absurd expects from those columns at fire time; reconcile splits
`resolve_spawn_options`' dicts into the columns; the admin renders typed widgets. Typed
columns add form/`full_clean` validation the raw JSONFields lacked (bad `kind`/type
previously slipped to runtime). `queue` gets a callable `choices` on the model field.

**Settings-driven values bubble up (REQUIREMENT).** Values set in code — a task's
`@absurd_default_params(retry_strategy=…, cancellation=…)`, resolved by
`resolve_spawn_options` — must materialize into these typed columns at reconcile for
settings schedules, exactly as they do today into the JSONFields. A partial strategy
(e.g. `kind` only, or `kind`+`base_seconds`) round-trips: reconcile splits it into the
columns, the wrapper rebuilds the same partial jsonb. This is not just an admin feature
— the split in Tasks 1/2 covers the settings path too.

**Blank → omit (SDK contract, confirmed).** `absurd_sdk._serialize_retry_strategy`
always emits `kind` but omits absent `base_seconds`/`factor`/`max_seconds` (Absurd
applies its own backoff defaults). So the wrapper must **omit null numeric params**
(`jsonb_strip_nulls`), and emit a `retry_strategy` **only when `retry_kind` is set** —
`kind` anchors the strategy; numeric params without a `kind` are meaningless and must be
rejected at `full_clean` (see Task 1).

**Tech Stack:** Django 6.0, psycopg3, pg_cron, absurd_sdk (`RetryStrategy`,
`CancellationPolicy` TypedDicts, `total=False`).

## Global Constraints

- Django 6.0 / Python 3.12 floor; psycopg3 backend.
- pytest function-based only; autouse `_enable_db`; `transaction=True` only when a test
  commits / does DDL (migrate, create_queue, fire wrapper).
- No monkeypatch / `unittest.mock`. Behavioral tests through real entrypoints. Admin
  HTTP-tested.
- Assert the COMPLETE error/message text, never a fragment. Alphabetize `@parametrize`
  values + fixture params.
- Full patch coverage (100% stmt+branch on added/changed lines) via real entrypoints,
  not test-only seams.
- `import typing as t`; absolute imports; functions contain a verb; no
  leading-underscore module helpers.
- pg_cron suite needs the `db_pg_cron` service up; `--create-db` after migration changes
  (evict pg_cron's session first per CLAUDE.md).
- TDD RED-first. This plan shows tests as code; implementation described in PROSE only —
  never a finished production code block.
- New migration files for schema changes (do NOT in-place-edit applied migrations here).

**absurd_sdk shapes (mirror exactly):**

- `RetryStrategy`: `kind: Literal["fixed","exponential","none"]`, `base_seconds: float`,
  `factor: float`, `max_seconds: float`.
- `CancellationPolicy`: `max_duration: int`, `max_delay: int`.

---

### Task 1: Typed `retry_strategy` columns

**Files:**

- Modify: `django_absurd/pg_cron/models.py` (ScheduledTask fields)
- Modify: `django_absurd/pg_cron/reconcile.py` (`sync_crons` update_or_create defaults;
  keep `resolve_spawn_options` returning the SDK dict)
- Create: `django_absurd/pg_cron/migrations/0004_*.py` (add columns, drop
  `retry_strategy`, ALTER wrapper fn)
- Modify: `django_absurd/pg_cron/admin.py` (fieldsets order; retry_kind auto-dropdown
  from choices)
- Test: `tests/pg_cron/validators/test_retry_strategy.py` (new),
  `tests/pg_cron/test_pg_cron_sync_jobs.py`,
  `tests/pg_cron/test_scheduledtask_model.py`,
  `tests/pg_cron/test_admin/test_scheduledtask.py`

**Interfaces:**

- Produces columns: `retry_kind` (str|None, choices fixed/exponential/none),
  `retry_base_seconds`/`retry_factor`/`retry_max_seconds` (float|None). Removes column
  `retry_strategy`.
- Wrapper contract unchanged externally: at fire time it still passes a `retry_strategy`
  jsonb to `absurd.spawn_task` when any retry column is set.

- [ ] **Step 1: RED — retry_kind rejects a bad kind (validator harness, model
      subject).**

```python
# tests/pg_cron/validators/test_retry_strategy.py
from tests.pg_cron.validators.utils import validate_from_model


def test_retry_kind_invalid_choice_rejected(settings):
    result = validate_from_model(settings, retry_kind="bogus")
    assert result
    assert (
        "Value 'bogus' is not a valid choice." in result
        or "Select a valid choice. bogus is not one of the available choices." in result
    )
```

(Note: the model-field `choices` message form is
`"Value %(value)r is not a valid choice."` for `full_clean`; assert the exact one Django
emits — confirm during RED and pin the single complete string.)

- [ ] **Step 2: Run RED.**
      `uv run pytest tests/pg_cron/validators/test_retry_strategy.py -v` → FAIL (field
      `retry_kind` doesn't exist / no validation).

- [ ] **Step 3: Implement (prose).** In `models.py`, add a module constant for
      retry-kind choices (a `TextChoices` named e.g. `RetryKind` with
      `FIXED/EXPONENTIAL/NONE`, values `"fixed"/"exponential"/"none"`, verb-free enum
      allowed). Replace the `retry_strategy = JSONField(...)` line with four fields:
      `retry_kind = TextField(choices=RetryKind.choices, null=True, blank=True)`;
      `retry_base_seconds`, `retry_factor`, `retry_max_seconds` =
      `FloatField(null=True, blank=True)`. Keep `VALID` baseline in
      `validators/utils.py` clean (no retry_* keys → all null).

- [ ] **Step 4: Run GREEN.** Same command → PASS once the field + choices exist
      (`full_clean` runs the choice validation).

- [ ] **Step 5: RED — reconcile round-trips a task's `@absurd_default_params` retry
      strategy into the columns.**

```python
# tests/pg_cron/test_pg_cron_sync_rows.py  (add)
def test_reconcile_splits_retry_strategy_into_columns(settings):
    # tests.tasks.<a task decorated with @absurd_default_params(retry_strategy=...)>
    settings.TASKS = build_pg_cron_tasks(
        {"r": {"task": "tests.tasks.retrying", "cron": "0 2 * * *"}}
    )
    reconcile_crons_after_migrate(sender=None)
    row = ScheduledTask.objects.get(source="s", alias="default", name="r")
    assert row.retry_kind == "exponential"
    assert row.retry_base_seconds == 2.0
```

(If no such fixture task exists, add `tests/tasks.py::retrying` decorated with
`@absurd_default_params(retry_strategy=RetryStrategy(kind="exponential", base_seconds=2))`
— a fixture task, not production.)

- [ ] **Step 6: Run RED** → FAIL (`retry_kind` empty; reconcile still tries to write
      `retry_strategy`).

- [ ] **Step 7: Implement (prose).** In `sync_crons`' `update_or_create(defaults=...)`,
      replace `"retry_strategy": opts.get("retry_strategy")` with a split: read
      `opts.get("retry_strategy") or {}` and map `kind→retry_kind`,
      `base_seconds→retry_base_seconds`, `factor→retry_factor`,
      `max_seconds→retry_max_seconds` (each `.get(...)` → None when absent).
      `resolve_spawn_options` itself is unchanged (still returns the SDK dict from
      `_normalize_spawn_options`).

- [ ] **Step 8: Run GREEN** → PASS.

- [ ] **Step 9: RED — the wrapper reassembles `retry_strategy` jsonb at fire time.**

```python
# tests/pg_cron/test_pg_cron_sync_jobs.py  (add / extend)
def test_wrapper_rebuilds_retry_strategy_from_columns(settings):
    settings.TASKS = build_pg_cron_tasks(
        {"r": {"task": "tests.tasks.add", "cron": "0 2 * * *"}}
    )
    sync_crons(get_absurd_backends()["default"])
    row = ScheduledTask.objects.get(source="s", alias="default", name="r")
    ScheduledTask.objects.filter(pk=row.pk).update(
        retry_kind="fixed", retry_base_seconds=1.5
    )
    # fire the committed wrapper; assert the spawned run carries the rebuilt jsonb
    run_scheduled("s", "default", "r")
    # read the spawned task's retry_strategy from the queue table (via the public
    # Task/Run models or absurd.claim) and assert == {"kind": "fixed", "base_seconds": 1.5}
```

(Choose the observable: the simplest is asserting the queued run's `retry_strategy`
column via a direct `cron.job`-independent query on the absurd queue table, or
`absurd.claim_task`. Pin the exact assertion during RED.)

- [ ] **Step 10: Run RED** → FAIL (wrapper still reads the dropped `v.retry_strategy`).

- [ ] **Step 11: Implement (prose).** Author `0004` migration: `AddField` the four
      retry_* columns; `RemoveField` `retry_strategy`; a `RunSQL` that
      `CREATE OR REPLACE`s `public.django_absurd_run_scheduled` so the `retry_strategy`
      branch fires **only when `v.retry_kind IS NOT NULL`** (kind anchors the strategy)
      and appends
      `jsonb_build_object('retry_strategy', jsonb_strip_nulls(jsonb_build_object('kind', v.retry_kind, 'base_seconds', v.retry_base_seconds, 'factor', v.retry_factor, 'max_seconds', v.retry_max_seconds)))`
      — the inner `strip_nulls` drops absent numeric params so Absurd applies its own
      defaults (per `_serialize_retry_strategy`). Reverse SQL restores the prior
      function body. Keep the wrapper's other branches (headers, cancellation — Task 2,
      idempotency_key) intact.

- [ ] **Step 12: Rebuild the pg_cron test DB** (schema change): evict + `--create-db`
      per CLAUDE.md, then run GREEN → PASS.

- [ ] **Step 13: RED — admin renders `retry_kind` as a select.**

```python
# tests/pg_cron/test_admin/test_scheduledtask.py  (add)
def test_add_view_retry_kind_is_a_dropdown(settings, client, admin_user):
    seed(settings)
    client.force_login(admin_user)
    soup = BeautifulSoup(client.get(ADD).content, "html.parser")
    values = [o.get("value") for o in soup.select('select[name="retry_kind"] option')]
    assert values == ["", "exponential", "fixed", "none"]
```

(Order = the blank choice then choices as Django renders them; pin exact order during
RED — `choices` order, not sorted, unless the enum is declared sorted.)

- [ ] **Step 14: Run RED** → FAIL (retry_kind renders as text / field missing from
      fieldset).

- [ ] **Step 15: Implement (prose).** In `admin.py`, update the "Spawn options"
      fieldset: replace `retry_strategy` with `retry_kind`, `retry_base_seconds`,
      `retry_factor`, `retry_max_seconds`; replace `cancellation` slot later in Task 2.
      `retry_kind` auto-renders as a `Select` (model choices) — no form override. Drop
      `retry_strategy`/`cancellation` from any `Meta.widgets` TextInput map (they're not
      text). Keep `headers` as-is (JSONField textarea).

- [ ] **Step 16: Run GREEN** → PASS.

- [ ] **Step 16b: RED — a retry timing param without `retry_kind` is rejected** (kind
      anchors the strategy; the wrapper emits nothing without it, so silently ignoring
      the param would surprise).

```python
# tests/pg_cron/validators/test_retry_strategy.py  (add)
def test_retry_timing_without_kind_rejected(settings):
    result = validate_from_model(settings, retry_base_seconds=1.5)
    assert result
    assert "Set a retry kind to configure retry timing."  # pin the exact message
```

- [ ] **Step 16c: Run RED** → FAIL (no such rule).

- [ ] **Step 16d: Implement (prose).** In `ScheduledTask.clean()`, add: if any of
      `retry_base_seconds`/`retry_factor`/`retry_max_seconds` is set while `retry_kind`
      is None, raise `ValidationError` keyed to `retry_kind` with the message above.
      (Reconcile's settings rows always come from `resolve_spawn_options`, which
      includes `kind` whenever it emits a strategy, so this only bites hand-authored
      admin/ORM rows — and reconcile doesn't `full_clean`, so it's unaffected.)

- [ ] **Step 16e: Run GREEN** → PASS.

- [ ] **Step 17: Update existing tests referencing `retry_strategy`.** The model test
      `test_scheduledtask_has_explicit_option_columns` sets/asserts
      `retry_strategy={"kind":"fixed"}` — split it to set `retry_kind="fixed"` and
      assert the column. Grep `retry_strategy` across `tests/pg_cron/` and convert every
      occurrence.

- [ ] **Step 18: Run the pg_cron suite** `uv run pytest tests/pg_cron -q` → all PASS,
      100% patch coverage.

- [ ] **Step 19: Commit.**

```bash
git add -A
git commit  # feat(pg_cron): typed retry_strategy columns (retry_kind/base_seconds/factor/max_seconds)
```

---

### Task 2: Typed `cancellation` columns

**Files:** same set as Task 1 (models, admin, wrapper via a NEW migration `0005_*`,
tests). Mirrors Task 1 for `CancellationPolicy`.

**Interfaces:**

- Produces columns: `cancellation_max_duration` (int|None), `cancellation_max_delay`
  (int|None). Removes column `cancellation`.

- [ ] **Step 1: RED — cancellation columns validate as integers (model subject).**

```python
# tests/pg_cron/validators/test_cancellation.py (new)
from tests.pg_cron.validators.utils import validate_from_model


def test_cancellation_max_duration_rejects_non_integer(settings):
    result = validate_from_model(settings, cancellation_max_duration="soon")
    assert result
    assert "Enter a whole number." in result  # pin the exact Django message during RED
```

- [ ] **Step 2: Run RED** → FAIL.
- [ ] **Step 3: Implement (prose).** In `models.py` replace
      `cancellation = JSONField(...)` with
      `cancellation_max_duration`/`cancellation_max_delay` =
      `IntegerField(null=True, blank=True)`.
- [ ] **Step 4: Run GREEN** → PASS.
- [ ] **Step 5: RED — reconcile splits cancellation dict into columns** (mirror Task 1
      Step 5, a fixture task with
      `@absurd_default_params(cancellation=CancellationPolicy(max_duration=30))`).
- [ ] **Step 6: Run RED** → FAIL.
- [ ] **Step 7: Implement (prose).** `sync_crons` defaults: replace
      `"cancellation": opts.get("cancellation")` with
      `max_duration→cancellation_max_duration`, `max_delay→cancellation_max_delay`
      split.
- [ ] **Step 8: Run GREEN** → PASS.
- [ ] **Step 9: RED — wrapper rebuilds `cancellation` jsonb** (mirror Task 1 Step 9).
- [ ] **Step 10: Run RED** → FAIL.
- [ ] **Step 11: Implement (prose).** `0005` migration: `AddField` the two columns,
      `RemoveField` `cancellation`, `RunSQL` `CREATE OR REPLACE` wrapper so the
      cancellation branch builds
      `jsonb_strip_nulls(jsonb_build_object('max_duration', v.cancellation_max_duration, 'max_delay', v.cancellation_max_delay))`
      appended only when `<> '{}'`. Reverse restores prior body.
- [ ] **Step 12: Rebuild pg_cron DB (`--create-db`), run GREEN** → PASS.
- [ ] **Step 13: Implement (prose).** Admin "Spawn options" fieldset: swap
      `cancellation` for the two columns.
- [ ] **Step 14: Update existing `cancellation` test references** (grep
      `tests/pg_cron/`), run suite → PASS.
- [ ] **Step 15: Commit.**
      `feat(pg_cron): typed cancellation columns (max_duration/max_delay)`

---

### Task 3: Dynamic `queue` choices

> **DESIGN NOTE — read before starting; confirm with the maintainer.** Putting `choices`
> on the _model_ field does NOT let us delete the custom queue validator, because the
> **system check** (`absurd.E007`) validates a settings `SCHEDULE` entry's queue with
> `validate_declared_queue` and has **no model instance** — model-field `choices` don't
> apply there. So `validate_declared_queue` MUST stay for the check. Model `choices`
> also make `full_clean` emit Django's generic `"…is not a valid choice."` for an
> undeclared explicit queue, which (a) differs from the check's
> `"queue '…' is not declared."` message and (b) would double-error if `clean()` also
> called the override branch. Resolution baked into this task: **keep
> `validate_declared_queue` intact (both branches) for the check; `clean()` calls it
> ONLY for the blank/task-intrinsic case; model `choices` cover the explicit-queue case
> on the model/admin path.** A simpler alternative (a form-only `ChoiceField`, no
> migration, single validator/message) exists — confirm which approach before executing.

**Files:**

- Modify: `django_absurd/pg_cron/models.py` (queue field `choices=<callable>`; `clean()`
  narrows the queue call)
- Modify: `django_absurd/pg_cron/validators.py` (docstring only if behavior unchanged;
  keep both branches)
- Create: `django_absurd/pg_cron/migrations/0006_*.py` (AlterField queue choices)
- Modify: `tests/pg_cron/validators/test_declared_queue.py`,
  `tests/pg_cron/validators/utils.py`, `tests/pg_cron/test_admin/test_scheduledtask.py`

**Interfaces:**

- Produces module callable `get_declared_queue_choices()` (verb `get`) →
  `list[tuple[str,str]]` = declared queues of configured pg_cron backends, or
  `[("default","default")]` when none declared. Referenced by the migration (must stay
  importable permanently).

- [ ] **Step 1: RED — admin queue field is a dropdown of declared queues.**

```python
# tests/pg_cron/test_admin/test_scheduledtask.py  (add)
def test_add_view_queue_is_a_dropdown_of_declared_queues(settings, client, admin_user):
    seed(settings)  # QUEUES {default, other, reports}
    client.force_login(admin_user)
    soup = BeautifulSoup(client.get(ADD).content, "html.parser")
    values = [o.get("value") for o in soup.select('select[name="queue"] option')]
    assert values == ["", "default", "other", "reports"]
```

- [ ] **Step 2: Run RED** → FAIL (queue renders as text input).

- [ ] **Step 3: Implement (prose).** In `models.py` add module-level
      `get_declared_queue_choices()` returning
      `[(q, q) for q in sorted(<declared queues across pg_cron backends>)]` or
      `[("default", "default")]` when the set is empty (import
      `get_absurd_backends`/`get_declared_queues` lazily inside the function if needed
      to avoid app-registry issues). Set
      `queue = models.TextField(choices=get_declared_queue_choices, blank=True, default="")`.
      Admin auto-renders a `Select`.

- [ ] **Step 4: Run GREEN** → PASS. Confirm
      `test_add_view_backend_field_offers_only_pg_cron_backends` and other admin tests
      still pass (queue now a select — update any that asserted a text input).

- [ ] **Step 5: RED — undeclared explicit queue is rejected per entrypoint.** Split the
      old unified `test_undeclared_queue_override_rejected`. Check subject keeps the
      custom message; model subject gets Django's choice message; the form subject can't
      submit an undeclared value (dropdown), so it's dropped from this case.

```python
# tests/pg_cron/validators/test_declared_queue.py  (rewrite the override case)
def test_undeclared_queue_override_rejected_by_check(settings, capsys):
    result = validate_from_system_check(settings, capsys, queue="ghost")
    assert result
    assert "queue 'ghost' is not declared." in result


def test_undeclared_queue_override_rejected_by_model(settings):
    result = validate_from_model(settings, queue="ghost")
    assert result
    assert "…is not a valid choice."  # pin Django's exact full message during RED
```

- [ ] **Step 6: Run RED** → the model case FAILs if `clean()` still emits the custom
      message (double) or the field lacks choices; the check case should already pass
      (validate_declared_queue unchanged).

- [ ] **Step 7: Implement (prose).** In `ScheduledTask.clean()`, wrap the
      `validate_declared_queue(...)` call so it runs only when `self.queue` is blank
      (the explicit-queue case is now enforced by the field's `choices` on `full_clean`;
      running the override branch too would double-error). Leave
      `validate_declared_queue` and the check path unchanged. Author `0006` migration:
      `AlterField` `queue` to add `choices=get_declared_queue_choices` (Django
      serializes the callable by import path).

- [ ] **Step 8: Run GREEN** → PASS.

- [ ] **Step 9: RED — blank queue still validates the task's intrinsic queue is declared
      (check + model).** Keep the existing
      `test_bad_task_no_queue_reports_task_not_queue` and the blank/task-queue coverage;
      ensure both check + model subjects still emit `"queue '…' is not declared."` for a
      task whose own `queue_name` is undeclared and no override is set.

- [ ] **Step 10: Run** the blank-case tests → PASS (behavior preserved).

- [ ] **Step 11: Rebuild pg_cron DB (`--create-db`) if the AlterField altered anything;
      run the pg_cron suite** → all PASS, patch coverage 100%.

- [ ] **Step 12: Commit.** `feat(pg_cron): dynamic queue choices from declared queues`

---

### Task 4: Docs

**Files:** `django_absurd/AGENTS.md`, `docs/web/cron-jobs.md`

- [ ] **Step 1:** Document the typed spawn-option fields (retry_kind + numeric retry
      params; cancellation max_duration/max_delay) and that `queue` is a dropdown of
      declared queues (blank → the task's own queue). Note `headers` stays free-form
      JSON.
- [ ] **Step 2:** `uvx zensical build` → "No issues found".
- [ ] **Step 3:** Commit. `docs: typed spawn-option fields + queue dropdown`

---

## Self-Review

- **Spec coverage:** retry_strategy typed (T1), cancellation typed (T2), headers stays
  JSON (T1 impl note), wrapper rebuild (T1/T2 RunSQL), reconcile split (T1/T2), dynamic
  queue choices + none→default (T3), migrations as new files (T1/T2/T3), admin widgets
  (T1/T2/T3), validator harness updates (T3). Covered.
- **Placeholder scan:** RED assertions on exact Django messages are marked "pin during
  RED" — the implementer must substitute the verbatim string (Django's
  `invalid_choice`/`invalid` message) before GREEN; this is inherent to complete-message
  assertions, not a TBD in logic.
- **Type consistency:** column names `retry_kind`, `retry_base_seconds`, `retry_factor`,
  `retry_max_seconds`, `cancellation_max_duration`, `cancellation_max_delay`, callable
  `get_declared_queue_choices` — used consistently across tasks.

## Open decision (surface before executing Task 3)

Task 3's DESIGN NOTE: model callable `choices` (maintainer's stated preference) vs a
form-only `ChoiceField`. Model choices need `validate_declared_queue` retained for the
check anyway + cause a per-entrypoint message split; the form field is simpler and keeps
one validator/message. Confirm the approach before Task 3.
