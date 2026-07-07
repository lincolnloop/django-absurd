# pg_cron schedule admin (read-only) — design

**Goal:** Read-only Django admin for pg_cron `ScheduledTask` rows — view declared
schedules + their spawn options in admin. No editing.

**Issue:** #44. Follows #43 (beat + pg_cron schedulers delivered).

## Scope

- Read-only ONLY. Settings `SCHEDULE` stays source of truth. No add/change/delete.
- pg_cron-only: `ScheduledTask` lives in opt-in `django_absurd.pg_cron` app; admin
  exists only when app installed. Beat = settings-only (no table), not shown.
- **NEXT unit of work (deferred, own sub-project):** fully DB/admin-driven schedule
  authoring — `source="admin"` create/edit in admin + reconcile emitting pg_cron jobs
  for admin rows. Foundation present (`source` split, reconcile never touches
  `source="admin"`, `build_jobname(…, source=…)` parameterized). Out of scope here.

## Architecture

New `django_absurd/pg_cron/admin.py`, mirroring core `django_absurd/admin.py`:

- Reuse `ReadOnlyAbsurdAdmin` base (no add/change/delete, view-only,
  `get_readonly_fields` = all model fields).
- Reuse `resolve_admin_sites()` (reads backend `OPTIONS["ADMIN_SITE"]`, default
  `django.contrib.admin.site`) and the `ENABLE_ADMIN` gate.
- Register `ScheduledTask` on each resolved site at module import under
  `contextlib.suppress(Exception)`. Django admin autodiscover imports the app's
  `admin.py` when the app is installed.
- Routing: `AbsurdRouter` already covers `django_absurd_pg_cron`, so the default
  queryset reads the Absurd DB (non-default-DB safe). Confirm
  `ReadOnlyAbsurdAdmin.get_queryset` doesn't force a wrong `using`; if it needs a
  `using` attr, set via `resolve_absurd_database(backend)` or override to plain
  `super().get_queryset()` (router routes). Resolve in the plan.

## Component: `ScheduledTaskAdmin(ReadOnlyAbsurdAdmin)`

- `list_display` = name, alias, task, queue, cron, enabled, source, updated_at
- `list_filter` = alias, enabled, source, queue
- `search_fields` = name, task
- `verbose_name` / plural = "Scheduled task" / "Scheduled tasks"
- `ordering` = (alias, name)
- detail (read-only) shows option columns too: args, kwargs, max_attempts,
  retry_strategy, headers, cancellation, idempotency_key, created_at, updated_at

## Registration

`autoregister_scheduled_task_admin()` (verb-named): resolve pg_cron backend; if
`ENABLE_ADMIN` false → skip; else register `ScheduledTask` on `resolve_admin_sites()`.
Called at import via `contextlib.suppress(Exception)`. Mirror core `autoregister_admin`.

## Tests (TDD, bs4) — `tests/pg_cron/test_admin/`

Package per [[admin-test-url-pattern]]: module per concern; URLs via `reverse_lazy`
constants (no-arg) + `reverse` helpers (args), never hand-written paths. bs4 DOM
assertions (`parse_html`, `result_rows`). Behavioral via HTTP client + real rows seeded
by `sync_crons` (not raw ORM where a public path exists).

- `support.py` — bs4 helpers; register helper (register on `djadmin.site`, reload
  `ROOT_URLCONF`, `clear_url_caches`); seed rows via `sync_crons`.
- `test_scheduledtask.py`:
  - changelist 200, one row per `ScheduledTask`, expected columns rendered
  - each `list_filter` narrows the rendered rows (alias, enabled, source, queue)
  - search by name / task narrows
  - read-only: no "Add" link; change-view fields disabled; POST to change mutates
    nothing
  - detail view renders the option columns
- `test_registration.py`:
  - `ENABLE_ADMIN` default → `ScheduledTask` registered on site
  - `ENABLE_ADMIN=False` → not registered
  - custom `ADMIN_SITE` → registered there

## Non-goals

- Editable / admin-authored schedules (→ next sub-project).
- Beat schedule visibility (no backing table).
- Custom actions, inlines, next-run computation.
