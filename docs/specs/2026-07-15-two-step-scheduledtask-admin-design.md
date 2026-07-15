# Two-step ScheduledTask admin (create → change) — design

## Intent

Admin authoring of pg_cron `ScheduledTask` rows currently = one big form, all fields,
`queue` blank-able, blank-queue resolution baked into `model.clean()`. Simplify: split
into a minimal **create** step + a full **change** step, mirroring
`django.contrib.auth`'s `UserAdmin` (`add_form` + `add_fieldsets`). Create captures only
what can't be derived (identity + task + when); on save, resolve every spawn option from
the task's `@task` / `@absurd_default_params` decorators and store it. Change step edits
the fully-populated row + enables it. Lets `queue` be non-blank everywhere + kills the
implicit blank-resolution special-casing.

## Architecture

Two forms, `UserAdmin`-style:

- **`ScheduledTaskCreateForm`** (`add_form`) — a subclass of `ScheduledTaskForm` with
  `Meta.fields = ("alias", "name", "task", "cron")` (inherits the `validate_unique`
  override, source-pinning, and `clean_args`/`clean_kwargs`). `alias` is always shown; a
  single pg_cron backend renders a one-option dropdown. `source` pinned ADMIN, not
  shown.
- **`ScheduledTaskForm`** (change `form`) — full field set. Identity (`name`, `alias`,
  `source`) read-only. Rest editable, `queue` required.
- `ScheduledTaskAdmin.get_form` / `get_fieldsets` return add-form + `add_fieldsets` when
  `obj is None`, else change form + full fieldsets (the `UserAdmin` switch);
  `response_add` redirects to the change page.

One shared resolver (unify settings + admin lanes):

- Refactor `resolve_spawn_options(backend, task_path)` — drop the `Schedule` param (only
  `schedule.task` was ever used).
- New `build_scheduled_fields(backend, task_path, *, queue_override=None) -> dict` in
  `reconcile.py`. Returns the spawn-derived columns: `queue` (effective-queue = override
  else task `queue_name`), `max_attempts`, `retry_kind` +
  `retry_base_seconds`/`retry_factor`/`retry_max_seconds`,
  `cancellation_max_duration`/`cancellation_max_delay`, `headers`, `idempotency_key`.
  NOT `args`/`kwargs` (call-args, default `[]`/`{}`).
- `sync_crons` (settings lane) calls it instead of its inline defaults dict; the admin
  create form calls it too. Single derivation path — the two lanes can't drift.

## Create flow

The create form validates the four fields, then its `_post_clean` (before
`super()._post_clean()`, so the resolved values flow through `model.clean()` and field
validation — e.g. `max_attempts >= 1`) fills the spawn columns on the instance via
`build_scheduled_fields`, leaving `args`/`kwargs` at their `[]`/`{}` defaults. The row
is saved **`enabled=False`** (its `post_save` schedules the pg_cron job but arms it
inactive, so nothing fires yet). The admin redirects to the change page. There the user
reviews the resolved values, supplies `args`/`kwargs` if the task needs them, and sets
`enabled=True` to go live — so a schedule never fires with empty args. Frozen at create:
later decorator edits don't retroactively change existing rows (same as a settings row
between reconciles).

## Model changes

- `queue`: `blank=False`, drop `default=""`. Keep model-level
  `choices=get_declared_queue_choices` (already there). Always concrete — set by
  `build_scheduled_fields` on settings + create; required on change (dropdown has no
  empty option).
- Delete `resolve_blank_queue()` + its call in `validate_against_backend()`. Resolution
  is now explicit (create `_post_clean` + settings reconcile); `clean()` never silently
  fills a blank queue. Note: `validate_declared_queue`'s blank→effective-queue branch
  stays (it's the shared rule the check path also uses) — the create form no longer
  depends on it, because `_post_clean` sets a concrete queue before `model.clean()`.
- `validate_against_backend()` keeps validating the (always concrete) `queue` + `cron`.

## Validation (upfront, on create, before save/schedule)

Reuse existing validators: name charset; task (importable + is a `@task` + its
`queue_name` declared on the backend); cron (pg_cron grammar, DB probe); jobname length;
uniqueness `(source, alias, name)` (via the inherited `validate_unique`). Because
resolution runs in `_post_clean` before `full_clean`, the decorator-derived columns are
validated too (e.g. a task with `@absurd_default_params(max_attempts=0)` is rejected by
the field's `MinValueValidator(1)`, not a DB `IntegrityError`). Invariant: a created row
is always valid.

## Kept as-is

`clean_args`/`clean_kwargs` blank→`[]`/`{}` coercion (change-form JSON textareas);
stale-queue choice-injection in change `__init__`; `retry_kind`-without-timing check;
`validate_unique` override; `absurd.E009`; shape validators; `source` read-only.

## Testing (behavioral, real admin HTTP; RED→GREEN)

- **Decorator-derived assignment is the load-bearing test, RED first:** a task with
  `@task(queue_name=X)` +
  `@absurd_default_params(max_attempts=N, retry_strategy=..., cancellation=...)` →
  create via admin (POST the 4 fields only) → assert the row's `queue == X`,
  `max_attempts == N`, `retry_kind`/`retry_base_seconds`/… and `cancellation_max_*`
  equal the decorator values, `enabled is False`, and the response redirects to the
  change page. RED (current form saves NULL/`""`/`enabled=False`-by-omission and doesn't
  resolve); GREEN after.
- add view renders only the 4 fields.
- create rejects (form error, no row): undeclared task queue, bad cron, duplicate
  `(source, alias, name)`.
- change form: prepopulated; `name`/`alias`/`source` read-only; blank `queue` → "This
  field is required."; enabling + running works.
- **parity:** admin-create and `sync_crons` of the same task produce identical resolved
  columns (guards the shared `build_scheduled_fields`).

## Migration

`queue` `blank=False` + drop `default=""` = field metadata; folded into the regenerated
`0001` (unreleased — same collapse approach used through this branch).

## Out of scope

Core `Queue` nested choices relocation (separate PR). Task-picker dropdown (no task
registry to enumerate; `task` stays a validated free-text dotted path). Re-resolving
decorator defaults on change-form load (frozen-at-create chosen).
