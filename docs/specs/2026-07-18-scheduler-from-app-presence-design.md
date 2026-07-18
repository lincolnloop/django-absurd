# Derive Scheduler from pg_cron App Presence — Design (#68)

**Goal:** drop `OPTIONS["SCHEDULER"]`. `backend.scheduler` becomes derived — `"pg_cron"`
when `django_absurd.pg_cron` is in `INSTALLED_APPS`, else `"beat"` — not a user-set
knob. Makes `beat`-backend-with-pg_cron-app-installed unrepresentable, folds away
`absurd.E008`, pairs with #63's single-backend model (merged, #77).

## Decision + why

`SCHEDULER` was over-granular: pg_cron is a property of the DB (extension +
`cron.database_name`), not a per-backend choice, and #63 already collapsed to one
backend per project. One backend + implicit scheduling = the clean end state. Breaking
change, but library is pre-1.0/alpha — no deprecation shim, straight removal.

## Core mechanism

`AbsurdBackend.__init__` (`backends.py`): replace
`self.scheduler = self.options.get("SCHEDULER", "beat")` with
`self.scheduler = "pg_cron" if apps.is_installed(PG_CRON_APP_NAME) else "beat"`.
`PG_CRON_APP_NAME` moves from `checks.py` to `backends.py` (checks.py already imports
from backends.py; backends.py must not import the pg_cron subpackage — core stays
independent of the optional app). `checks.py` imports the constant from `backends.py`
instead of defining it.

## Removed surface

- `AbsurdBackendOptions.SCHEDULER` TypedDict key — DELETE.
- `VALID_SCHEDULERS`, `E007_HINT_SCHEDULER` — DELETE. The "unknown SCHEDULER value"
  branch in `check_absurd_schedule_config` (checks.py
  `scheduler not in VALID_SCHEDULERS`) — DELETE; no longer user-settable, can't be
  invalid.
- `absurd.E008` (`check_scheduler_app_installed`'s error branch, `E008_MSG`,
  `E008_HINT`) — DELETE entirely. The misconfig it guarded is unrepresentable. `W003`
  (INSTALLED_APPS ordering) in the same function STAYS — unaffected, still checks
  ordering whenever the app is installed regardless of scheduler value. Once the E008
  branch is gone, `check_scheduler_app_installed` no longer checks that the app is
  installed — it only emits W003 on ordering — so RENAME it (e.g.
  `check_pg_cron_app_ordering`) to match what it actually does.
- `get_pg_cron_backends()` (backends.py) — DELETE. Its only caller is the E008 branch
  above; once that's gone it's unused.

## Collapsed dead branches (in same PR)

Everywhere inside `django_absurd/pg_cron/*` that tests `backend.scheduler != "pg_cron"`
is now tautological — pg_cron's own modules (`models.py`, `apps.py`, `checks.py`, its
management command) only ever execute when the app is installed (Django imports an app's
`models`/`apps`/registers its checks only for apps in `INSTALLED_APPS`), so
`backend.scheduler` is definitionally `"pg_cron"` there whenever a backend resolves at
all.

- `pg_cron/checks.py::check_pg_cron_schedules` — drop
  `if backend.scheduler != "pg_cron": continue`; iterate
  `get_absurd_backends().values()` directly (still ≤1 backend, still needs the loop
  shape or `get_absurd_backend()` — pick whichever reads cleaner at implementation
  time).
- `pg_cron/models.py` (3 sites: `get_declared_queue_choices`, `ScheduledTask.clean`,
  `resolve_pg_cron_backend`) — collapse
  `backend is None or backend.scheduler != "pg_cron"` → `backend is None`; collapse
  `backend is not None and backend.scheduler == "pg_cron"` → `backend is not None`.
- `pg_cron/apps.py::reconcile_crons_after_migrate` — the `else: teardown_crons()` branch
  (scheduler-switch-while-app-still-installed) is dead; DELETE it and its stdout
  message. Function becomes: no backend → return; else always the pg_cron sync
  (`sync_crons` + `sync_admin_crons`) path — no `if/else` on scheduler at all.
- `pg_cron/management/commands/absurd_sync_crons.py` — the
  `if backend.scheduler != "pg_cron": raise CommandError(...)` guard is dead (command
  only registered when installed); DELETE it, and the now-unused error message.

Reword the surviving "no-op" docstrings at these collapse sites
(`resolve_pg_cron_backend`, `schedule_pg_cron_job`, `unschedule_pg_cron_job` in
`pg_cron/models.py`) — they currently document "a no-op when no pg_cron backend is
configured"; after the collapse the only surviving no-op condition is "no
`AbsurdBackend` configured at all," so drop the now-impossible "backend present but not
pg_cron" framing.

## Kept as real conditionals

Core (`django_absurd/`, not `pg_cron/`) doesn't know app-installed status except via
`backend.scheduler` — these stay meaningful, not dead:

- `management/base.py::BEAT_DISABLED_UNDER_PG_CRON` + its use in
  `absurd_beat.py`/`absurd_worker.py` (`backend.scheduler == "pg_cron"` checks). REWORD
  the message — "SCHEDULER" is no longer a user-facing concept:
  `"the pg_cron app is installed: schedules run in the database via pg_cron, so the beat process is disabled. Reconcile with 'manage.py absurd_sync_crons' (migrate does it too)."`
- `checks.py::is_valid_cleanup(cleanup, scheduler)` — unchanged signature/logic, still
  branches on `scheduler == "beat"` for CLEANUP cron-grammar validation.

## Teardown-on-uninstall (edge case — documented, no new mechanism)

Uninstalling `django_absurd.pg_cron` entirely means its `AppConfig.ready()` never runs,
so the automatic post_migrate teardown path has no trigger. Manual escape already
exists: `manage.py absurd_sync_crons --teardown --noinput` while the app is STILL
installed. Docs get one note in `cron-jobs.md`: run that BEFORE removing the app from
`INSTALLED_APPS`, not after. No code changes for this case.

## Touchpoint table

| File                                               | What                                              | Change                                                                                                 |
| -------------------------------------------------- | ------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `backends.py`                                      | `AbsurdBackend.__init__`, `PG_CRON_APP_NAME`      | derive `self.scheduler` from `apps.is_installed`; constant moves here                                  |
| `backends.py`                                      | `AbsurdBackendOptions`                            | drop `SCHEDULER` key                                                                                   |
| `checks.py`                                        | `check_absurd_schedule_config`                    | drop unknown-SCHEDULER branch; import `PG_CRON_APP_NAME` from backends                                 |
| `checks.py`                                        | `check_scheduler_app_installed`                   | drop the E008 error branch + `E008_MSG`/`E008_HINT`; keep W003; RENAME to `check_pg_cron_app_ordering` |
| `checks.py`                                        | `VALID_SCHEDULERS`, `E007_HINT_SCHEDULER`         | DELETE                                                                                                 |
| `backends.py`                                      | `get_pg_cron_backends`                            | DELETE (only caller was the E008 branch)                                                               |
| `pg_cron/checks.py`                                | `check_pg_cron_schedules`                         | drop scheduler guard                                                                                   |
| `pg_cron/models.py`                                | 3 sites listed above                              | collapse to `backend is None` checks                                                                   |
| `pg_cron/apps.py`                                  | `reconcile_crons_after_migrate`                   | delete else/teardown branch                                                                            |
| `pg_cron/management/commands/absurd_sync_crons.py` | scheduler-mismatch guard                          | DELETE                                                                                                 |
| `management/base.py`                               | `BEAT_DISABLED_UNDER_PG_CRON`                     | reword message text                                                                                    |
| `docs/web/cron-jobs.md`                            | SCHEDULER references, E008 section                | rewrite for implicit derivation; add teardown-before-uninstall note                                    |
| `docs/web/configuration.md`                        | E008 row in check table                           | DELETE row                                                                                             |
| `django_absurd/AGENTS.md`                          | scheduler section, E008 entry, SCHEDULER examples | rewrite                                                                                                |
| `examples/beat/app.py`, `examples/pg_cron/app.py`  | `OPTIONS["SCHEDULER"]`                            | DELETE key (scheduling now implicit from which example installs the app)                               |
| `docs/WHY.md`                                      | scheduler decision note                           | add via sync-docs / capture-why after merge                                                            |

## Tests — delete vs edit vs add

`tests/pg_cron/utils.py::build_beat_tasks` is itself dead post-change, not a "strip the
key" edit: the pg_cron suite always has the app installed, so ANY TASKS it returns
derives `scheduler="pg_cron"` — it can no longer produce a beat backend (stripping its
`SCHEDULER` key would leave it byte-identical to `build_pg_cron_tasks`, mis-named).
DELETE the helper; every caller tests a now-unrepresentable "beat scheduler with pg_cron
app installed" state and must be deleted/reworked, not merely edited:

**Delete** (test now-impossible states):

- `tests/core/test_scheduler_app_checks.py` — whole file, E008 only.
- `tests/pg_cron/test_pg_cron_checks.py::test_unknown_scheduler_value_rejected` (~line
  317, "unknown SCHEDULER" case).
- `tests/pg_cron/test_pg_cron_post_migrate.py::test_reconcile_tears_down_when_scheduler_switches_to_beat`
  and `test_reconcile_emits_teardown_notice_when_backend_switches` (~line 168, ~line 338
  — scheduler-switch-while-installed, no longer reachable; both use `build_beat_tasks`).
- `tests/pg_cron/test_absurd_sync_crons_command.py::test_sync_crons_command_refuses_when_scheduler_is_beat`
  (~line 61 — asserts the exact `CommandError` guard this PR deletes from
  `absurd_sync_crons.py`) and `::test_teardown_allowed_when_scheduler_is_beat` (~line
  110 — "teardown works even under beat" is moot once beat-while-installed can't exist;
  teardown's scheduler-independence is covered by the surviving teardown tests that
  don't vary scheduler).
- `tests/pg_cron/test_schedule_emission.py::test_saving_non_pg_cron_backend_schedule_is_a_noop`
  (~line 81, uses `build_beat_tasks({})` at ~line 86 — unrepresentable post-change: with
  the app installed, `resolve_pg_cron_backend()` never returns `None` for a configured
  backend).
- `tests/pg_cron/test_scheduler_app_checks.py::test_pg_cron_app_before_core_warns_under_beat`
  (~line 76 — exists only to prove W003 fires regardless of scheduler value; once
  scheduler can't vary independently of app-installed, it's a duplicate of
  `test_pg_cron_app_before_core_warns`).
- `tests/pg_cron/test_scheduler_selector.py::test_scheduler_defaults_to_beat` (false in
  this suite post-change — see Add).

**Edit:** strip the `"SCHEDULER": ...` key from every remaining `TASKS` fixture/helper
across both suites (`tests/pg_cron/utils.py::build_pg_cron_tasks`,
`tests/pg_cron/validators/utils.py`,
`tests/pg_cron/test_scheduler_selector.py::build_pg_cron_tasks`,
`tests/pg_cron/test_pg_cron_checks.py::run_pg_cron_check` — drops the `scheduler`
options key too, and any case in that file exercising `scheduler="beat"` inside the
pg_cron suite (dead once the guard it probed is gone) — and the inline dicts in
`test_pg_cron_sync_jobs.py`, `test_pg_cron_sync_rows.py`, `test_pg_cron_teardown.py`,
`test_pg_cron_e2e.py`, `test_cleanup_schedule.py`, `test_scheduledtask_model.py`,
`test_admin/test_scheduledtask.py`, `test_checks.py`); each suite's INSTALLED_APPS now
fully determines the scheduler, so no test should set the key at all.
`tests/pg_cron/test_scheduler_app_checks.py::run_check` — drop its `scheduler` param/key
alongside dropping the E008-not-in-out assertions; keep the (scheduler- independent)
W003 ordering cases. `tests/pg_cron/test_absurd_sync_crons_command.py` and
`test_schedule_emission.py` — after deleting the beat-scheduler tests above, confirm no
remaining test in either file still calls `build_beat_tasks`.
`test_cross_source_coexistence.py` needs no direct edit — it builds TASKS via
`build_pg_cron_tasks` from `utils.py`, no inline `SCHEDULER`/beat references of its own;
covered by the `utils.py` edit.

**Add:** `test_scheduler_defaults_to_beat` moves to `tests/core/` (pg_cron app genuinely
absent there) — asserts `"beat"`; a new pg_cron-suite counterpart asserts
`get_absurd_backends()["default"].scheduler == "pg_cron"` with no `SCHEDULER` key set
anywhere (app-presence alone drives it).

## Migration note

Pre-1.0/alpha, no released users depend on `OPTIONS["SCHEDULER"]` per current
constraints — straight removal, no `RemovedInAbsurdXWarning`-style deprecation path.
