# Admin-definable pg_cron schedules — design

**Goal:** author/edit/delete recurring pg_cron schedules in Django admin as
`source="admin"` `ScheduledTask` rows — same validation rigor as the settings path, with
the `pg_cron` job (un)scheduled immediately on save/delete.

**Issue:** follow-up to #44 (read-only admin, shipped). Builds on the pg_cron scheduler
(#43) + read-only admin (#46).

## Ships as two PRs

- **Phase A — validator extraction + model-first enforcement** (prep,
  behavior-preserving except the cron-grammar corrections below). No writable admin yet.
- **Phase B — writable admin + runtime job emission** (the feature). Depends on A.

Each phase is its own plan/PR. Spec covers both so the shared decisions live in one
place.

## De-risked (already true in code)

- Fire path is **source-agnostic**: the wrapper
  `django_absurd_run_scheduled(source, alias, name)` reads the row by all three and
  spawns if `enabled` — an admin row + its `pg_cron` job fires with no fire-path change.
- `sync_crons`/`teardown_crons` are scoped to `source="settings"` — admin rows are never
  clobbered by settings reconcile.
- `build_jobname(alias, name, source=...)` is already parameterized; admin jobs are
  `absurd:admin:<alias>:<name>`, distinct from `absurd:settings:...`.

---

## Cron grammar — validated per scheduler (the load-bearing decision)

Two schedulers, two grammars, two authorities. **croniter is strictly the beat
validator.** pg_cron crons are validated by pg_cron (the DB), never croniter.

- **beat** — croniter grammar (full, incl. 6-field leading-seconds sub-minute).
  Unchanged. Validated at `check` time (DB-free, `croniter.is_valid`) and driven at fire
  time by croniter.
- **pg_cron** — pg_cron's own grammar, **DB-authoritative**. Empirically (pg_cron 1.6):
  accepts a **5-field cron** OR the interval form **`[1-59] seconds`** (seconds only,
  1–59; `1 hour`/minutes/days rejected). No croniter, no hand-rolled matcher.
  - **admin `clean()`** validates via a **savepoint-trial**:
    `SAVEPOINT s; SELECT cron.schedule('<probe>', <cron>, 'select 1'); ROLLBACK TO SAVEPOINT s`
    on `backend.database`. If `cron.schedule` raises, re-raise as a `ValidationError` on
    the `cron` field carrying pg_cron's own message/hint; the rollback guarantees no job
    is created. Auto-adapts to the deployment's pg_cron version.
  - **settings** stay validated by `cron.schedule` at sync (already DB-authoritative);
    check-time gets NO croniter grammar check for pg_cron entries (deferred to the DB at
    sync, per the existing "check stays DB-free" design).

### Corrections this forces (Phase A)

1. `validate_schedule` (core `checks.py`) runs `croniter.is_valid` for **every**
   scheduler today → scope croniter to **beat only**. pg_cron entries are no longer
   croniter-checked at `check` time.
2. Remove `check_pg_cron_cron_fields` (the "reject 6-field / assume max-5-fields" rule)
   — it blocks valid `30 seconds`, and pg_cron itself rejects bad grammar at sync with a
   clear hint.
3. **WHY.md** — reverse the "No sub-minute on pg_cron" note: pg_cron supports
   `1–59 seconds` natively (distinct from the rejected croniter-6-field _shim_); pg_cron
   grammar is DB-authoritative.
4. Existing check-time `E007`-for-pg_cron-6-field tests change (that rejection moves to
   the DB at sync). Settings pg_cron gains `30 seconds` support as a consistent side
   effect.
5. `1 seconds` is legal to pg_cron (~86k `cron.job_run_details`/day). An admin is
   explicitly authoring, so **allow** the full 1–59s range; surface a help-text caveat
   about high-frequency growth rather than capping.

---

## Phase A — validators + model-first enforcement

**Validators = pure functions, single source of rule truth.** Each validates one aspect
and raises `django.core.exceptions.ValidationError`. Split by granularity:

**Field-level** (single field → attach as `validators=[...]` on the model field; errors
surface on that field via `clean_fields()`):

- `name` — charset `[A-Za-z0-9_-]`
- `alias` — charset `[A-Za-z0-9_-]`
- `task` — importable + is a Django `Task`
- `args` — JSON-serializable
- `kwargs` — JSON-serializable

**`Model.clean()`** (needs 2+ fields or external/DB context):

- `cron` — pg_cron savepoint-trial (see cron section). (Not a field validator: needs
  `backend.database`.)
- jobname ≤ 63 bytes (composed `absurd:<source>:<alias>:<name>`)
- declared-queue membership (`queue` or the task's `queue_name` vs the backend's
  declared queues — resolves backend from `alias`)
- `alias` resolves to a configured `SCHEDULER="pg_cron"` backend (settings can't have a
  bad alias; admin can — new rule)
- cross-source `(alias, name)` clash — reject an `admin` row whose `(alias, name)`
  already exists as a `settings` row (and the converse in spirit). One name = one
  schedule. A validator, NOT a DB-constraint change (`unique_together` stays
  `(source, alias, name)`).

**Enforcement:** `ScheduledTask.clean()` calls the contextual validators (resolving the
backend from `self.alias`); field validators run via `full_clean()`. The admin
`ModelForm` and any `full_clean()` caller get validation for free. Reconcile's bulk
`update_or_create` does NOT call `full_clean`, so settings rows keep being validated by
the system check — same validators, no divergence.

**System checks rewired:** `checks.py` / `pg_cron/checks.py` call the shared validators
over the settings-`SCHEDULE` dict entries, wrapping `ValidationError` →
`E007 CheckMessage`. Behavior-preserving except the cron-grammar corrections above.
Existing E007 tests stay green (minus the removed 6-field pg_cron rejection).

### Testing — model-first, parametrized subjects, no duplicated assertions

Validators test package `tests/pg_cron/validators/test_<rule>.py`. **Per rule, one table
of cases** `(input → expected error)`. **Parametrized over SUBJECTS** = the real
entrypoints enforcing the rule:

- subject 1 — the **system check** (build a settings `SCHEDULE`, run checks, assert the
  `E007` text)
- subject 2 — **`ScheduledTask.full_clean()`** (build an instance, assert
  `ValidationError` on the field)
- (Phase B adds) subject 3 — **admin change POST** (submit the form, assert the form
  error)

Integration-style: drive each real entrypoint, write each rule's cases once. A
per-subject adapter maps a case to its entrypoint. Phase B slots subject 3 into the
existing tables with no new case data.

---

## Phase B — writable admin + runtime job emission

**Writable `ScheduledTaskAdmin`** (source="admin" only):

- `has_add`/`has_change`/`has_delete` → true (the read-only admin for `settings` rows
  stays; scope writability to the admin lane).
- A `ModelForm` (validation flows through `Model.clean()` / field validators).
- `source` auto-set to `"admin"` (hidden/non-editable).
- `alias` — a **choice** limited to configured pg_cron backends; label **"Backend"** +
  help text ("Which Absurd pg_cron backend runs this schedule"); prefill the sole
  backend when only one exists. Immutable on edit (part of the job identity + unique
  key).
- `name` — immutable on edit (same reason).
- Editable:
  `task, queue, cron, enabled, args, kwargs, max_attempts, retry_strategy, headers, cancellation, idempotency_key`.

**Runtime job emission** — `post_save` / `post_delete` signals scoped to
`source="admin"`:

- `post_save` → `apply_admin_job(row)`: upsert the `pg_cron` job (`cron.schedule` +
  `cron.alter_job(active := enabled)`) named `absurd:admin:<alias>:<name>`, on
  `backend.database`, under `SYNC_CRONS_ADVISORY_LOCK`, **in the same transaction as the
  row write**. Re-upsert on edit picks up cron/enabled changes.
- `post_delete` → `remove_admin_job(row)`: `cron.unschedule` the job.
- **Atomic:** if the pg_cron op raises, it propagates → the enclosing transaction rolls
  back → the row write is undone. `queryset.delete()` still sends `post_delete` per row,
  so admin bulk-delete is covered; direct ORM writes are covered too (model-first).
- `source="settings"` writes skip emission (reconcile owns them).

**Coexistence:** the cross-source `(alias, name)` clash validator (Phase A) blocks the
double-fire case.

**Scheduler-switch orphan cleanup:** extend `teardown_crons` to also unschedule the
`absurd:admin:<alias>:*` jobs (not just `absurd:settings:`), so nothing orphan-fires
after a backend switches off pg_cron — but **keep** the `source="admin"` rows (user
data). Re-arming on switch-back is a documented manual step (re-save the row); no
auto-reconcile of admin rows.

## Non-goals

- Editing `source="settings"` rows in admin (stay read-only).
- Beat schedules in admin (beat declares nothing in the DB — no table to show).
- Auto re-arm of admin rows on switch back to pg_cron (documented manual re-save).
- A DB-free static matcher for pg_cron cron grammar (the DB is the authority).
