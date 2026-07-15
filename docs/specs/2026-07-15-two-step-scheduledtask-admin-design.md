# Two-step ScheduledTask admin (create → change) — design

## Intent

Admin authoring of pg_cron `ScheduledTask` rows currently = one big form, all fields,
`queue` blank-able, blank-queue resolution baked into `model.clean()`. Simplify: split
into a minimal **create** step + a full **change** step, mirroring
`django.contrib.auth`'s `UserAdmin` (`add_form` + `add_fieldsets`). Create captures only
what can't be derived (identity + task + when); on save, resolve every spawn option from
the task's `@task` / `@absurd_default_params` decorators and store it. Change step edits
the fully-populated row. Lets `queue` be non-blank everywhere + kills the implicit
blank-resolution special-casing.

## Architecture

Two forms, `UserAdmin`-style:

- **`ScheduledTaskCreateForm`** (`add_form`) — fields: `alias` (shown only if >1 pg_cron
  backend), `name`, `task`, `cron`. `source` pinned ADMIN, not shown.
- **`ScheduledTaskForm`** (change `form`) — full field set. Identity (`name`, `alias`,
  `source`) read-only. Rest editable, `queue` required.
- `ScheduledTaskAdmin.get_form` / `get_fieldsets` return add-form + `add_fieldsets` when
  `obj is None`, else change form + full fieldsets (the `UserAdmin` switch).

One shared resolver (unify settings + admin lanes):

- Refactor `resolve_spawn_options(backend, task_path)` — drop the `Schedule` param (only
  `schedule.task` was ever used).
- New `build_scheduled_fields(backend, task_path, *, queue_override="") -> dict` in
  `reconcile.py`. Returns the spawn-derived columns: `queue` (effective-queue = override
  else task `queue_name`), `max_attempts`, `retry_kind` +
  `retry_base_seconds`/`retry_factor`/`retry_max_seconds`,
  `cancellation_max_duration`/`cancellation_max_delay`, `headers`, `idempotency_key`.
  NOT `args`/`kwargs` (call-args, default `[]`/`{}`).
- `sync_crons` (settings lane) calls it instead of its inline defaults dict; admin
  create-save calls it too. Single derivation path — the two lanes can't drift.

## Create flow

Add form validates the 4 fields → save resolves the rest via `build_scheduled_fields`
onto the instance → row saved `enabled=True` → `post_save` schedules the pg_cron job →
admin redirects to the change page (prepopulated). Frozen at create: later decorator
edits don't retroactively change existing rows (same as a settings row between
reconciles, same as `UserAdmin`).

## Model changes

- `queue`: `blank=False`, drop `default=""`. Keep model-level
  `choices=get_declared_queue_choices` (already there). Always concrete — set by
  `build_scheduled_fields` on settings + create; required on change (dropdown has no
  empty option).
- Delete `resolve_blank_queue()` + its call in `validate_against_backend()`. Resolution
  is now explicit in the create path; `clean()` never silently fills a blank queue.
- `validate_against_backend()` keeps validating the (always concrete) `queue` + `cron`.

## Validation (upfront, on create, before save/schedule)

Reuse existing validators: name charset; task (importable + is a `@task` + its
`queue_name` declared on the backend); cron (pg_cron grammar, DB probe); jobname length;
uniqueness `(source, alias, name)`. `queue` isn't a create field — the task-queue-
declared check already guarantees the resolved queue is valid. Invariant: a created row
is always valid + immediately schedulable. Exact resolve-then-persist ordering (form
`save()` vs `save_model`) = plan detail.

## Kept as-is

`clean_args`/`clean_kwargs` blank→`[]`/`{}` coercion (change-form JSON textareas can be
blanked); stale-queue choice-injection in change `__init__` (a declared queue removed
from settings still renders on an existing row so `clean()` can reject it);
`retry_kind`-without-timing check; `validate_unique` override (uniqueness matters on
create); `absurd.E009`; shape validators; `source` read-only.

## Testing (behavioral, real admin HTTP; RED→GREEN)

- **Decorator-derived assignment is the load-bearing test, RED first:** a task with
  `@task(queue_name=X)` +
  `@absurd_default_params(max_attempts=N, retry_strategy=..., cancellation=...)` →
  create via admin → assert the row's `queue == X`, `max_attempts == N`,
  `retry_kind`/`retry_base_seconds`/… and `cancellation_max_*` equal the decorator
  values. Fails before the resolver/create-form exist; passes after.
- add view renders only the 4 fields (+ `alias` iff >1 pg_cron backend).
- valid create → 302, resolved columns stored, `enabled`, pg_cron job scheduled,
  redirect to change page.
- create rejects (form error, no row): undeclared task queue, bad cron, duplicate
  `(source, alias, name)`.
- change form: prepopulated; `name`/`alias`/`source` read-only; blank `queue` → form
  error.
- **parity:** admin-create and `sync_crons` of the same task produce identical resolved
  columns (guards the shared `build_scheduled_fields`).

## Migration

`queue` `blank=False` + drop `default=""` = field metadata; folded into the regenerated
`0001` (unreleased — same collapse approach used through this branch).

## Out of scope

Core `Queue` nested choices relocation (separate PR). Task-picker dropdown (no task
registry to enumerate; `task` stays a validated free-text dotted path). Re-resolving
decorator defaults on change-form load (frozen-at-create chosen).
