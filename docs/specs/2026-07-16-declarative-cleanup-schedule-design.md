# Declarative scheduled cleanup — design

## Intent

Replace the user-written `@task` wrapper (shipped in #65) with one declarative knob —
`OPTIONS["CLEANUP"] = {"schedule": "<cron>"}` — that runs per-queue retention on a
cadence under **both** schedulers (beat and pg_cron), zero user code. Reuses
`cleanup_queues()` (#65). Retires the wrapper-scheduling story entirely. Delivers the
scheduled-cleanup half of #64 (the broader `enable_cron` maintenance surface stays out —
see Out of scope). Ref:
[Absurd cleanup](https://earendil-works.github.io/absurd/cleanup/).

## Why declarative (supersedes the wrapper)

#65 shipped the cleanup logic + on-demand command + a documented "write your own `@task`
wrapper, drop it in `SCHEDULE`" pattern (beat only; pg_cron unserved). Works, but pushes
boilerplate onto users and leaves pg_cron with no cleanup path. One `CLEANUP` setting
serves both schedulers with no user code. Still **no shipped `@task`** — beat runs
cleanup in-process, pg_cron uses native SQL — so the import-time backend/queue binding
that ruled out a shipped task never arises.

## Config

Backend `OPTIONS` gains `CLEANUP: {"schedule": "<cron>"}`. Backend-global: cleans every
queue on that backend (`cleanup_queues(None)` / `absurd.cleanup_all_queues()`).
Retention _amounts_ stay per-queue (`cleanup_ttl` / `cleanup_limit`). Honoured under
`SCHEDULER` beat OR pg_cron — no scheduler-gating. Absent → no scheduled cleanup. The
`schedule` cron reuses the schedulers' existing cron validation (see Validation).

## beat path

`run_beat` gains a cleanup cadence from `CLEANUP["schedule"]` **as a first-class seed**,
not an afterthought — the current loop is built purely from task schedules and **returns
early when `SCHEDULE` is empty** (`scheduler.py:92-95`), so a CLEANUP-only backend (the
headline case) would never tick. The plan must: (1) change the early-exit guard to
`not schedules and not cleanup`; (2) seed the cleanup cadence into the loop's
`upcoming`/`by_name` maps beside the task schedules; (3) on the cleanup slot, fire a
**non-task branch** that calls `cleanup_queues()` in-process and logs per-queue counts.
The cleanup firing MUST reuse the existing `fire_schedule` try/except (a raised cleanup
otherwise kills the whole beat loop) and the same `close_old_connections()` bracketing
`spawn_scheduled` uses (`scheduler.py:66,81`) — beat manages its connection per-fire, it
does not hold one. Long cleanup blocks the single-threaded loop until it returns
(acceptable; note it). No enqueue, no `@task`, no worker.

## pg_cron path

The pg_cron app's reconcile (post_migrate + `absurd_sync_crons`) schedules **Absurd's
own global cleanup job** on `CLEANUP["schedule"]` — jobname **`absurd_cleanup_all`**,
command **`select * from absurd.cleanup_all_queues(null::text);`**, the exact identity
`absurd.enable_cron` / `absurdctl cron --enable` use. This is a deliberate design call
(C2, revised): rather than forking a parallel per-alias job that would be INCOMPATIBLE
with Absurd's own, we reference the **one shared job** (a `cron.schedule` upsert;
`absurdctl cron --disable` also removes it). `absurd_cleanup_all` **survives
`absurd_flush`**: flush drops queues via per-queue `client.drop_queue()` →
`absurd.disable_cron(queue_name)`, which computes
`v_job_suffix = substr(md5(p_queue_name), 1, 12)` and targets only
`absurd_cleanup_<suffix>` jobs — never the `_all` suffix — so the global cleanup job is
untouched. It lives outside the managed `ScheduledTask` (`absurd:` colon) machinery, so
it is NOT swept by `get_managed_jobs()`'s `starts_with(jobname, 'absurd:')` scan
(`models.py:88-95`) — it never pollutes the `== []` teardown/prune assertions. It has
**no `ScheduledTask` projection row**; a dedicated reconcile enables it (when `CLEANUP`
is set) or unschedules it (when absent), managed statelessly by that fixed name.
**django-absurd is authoritative** over `absurd_cleanup_all` when `CLEANUP` is set (it
schedules and unschedules it), so cleanup must be driven by `OPTIONS["CLEANUP"]` OR
`absurdctl cron` — not both (multi-manager arbitration deferred to #63). Because it
lives outside the managed prefixes, it must be **explicitly torn down** by its own hook
wired into `teardown_crons` (`reconcile.py`) and the scheduler-flip path (`apps.py`) —
those only handle `absurd:s:`/`absurd:a:` today and would otherwise leak it. It is
cleanup-only, not `enable_cron`'s 3-job bundle, so partition/detach stay with #61. The
`select * from absurd.cleanup_all_queues(null::text);` command is a static literal — no
injection surface. Cron grammar is DB-authoritative (`cron.schedule` at sync). Admin
visibility for this job is deferred to #67 (a read-only maintenance panel) —
deliberately NOT via a `ScheduledTask` row, which would drag it back into the managed
namespace.

## Kept (on-demand / programmatic)

`absurd_cleanup` command + importable `cleanup_queues()` stay — ad-hoc "clean now" and
the logic beat itself calls. Not scheduling; `CLEANUP` does not replace them.

## Removed

The user-written `@task` wrapper + the "schedule a cleanup task" guidance — deleted from
`AGENTS.md` and `docs/web/cleanup.md`. `CLEANUP` is the sole scheduled-retention path.

## Validation

`AbsurdBackendOptions` gains `CLEANUP` (TypedDict `{"schedule": str}`). Nothing reads
`OPTIONS["CLEANUP"]` today (`check_absurd_schedule_config`, `checks.py:200`, only sees
`SCHEDULE`), and beat's cron check is an inline `croniter…is_valid` welded to the `E007`
"Schedule …" message — so no reusable validator exists yet and CLEANUP has no validation
home. Therefore **mint `absurd.E010`**: a dedicated check reading `OPTIONS["CLEANUP"]`
that validates the shape (`{"schedule": <non-empty str>}`, reject unknown keys) and the
cron. Do NOT overload `E007` (its meaning is "invalid `SCHEDULE` entry" — misleading for
a CLEANUP problem). Extract a shared **`validate_cron(cron, scheduler)`** helper (beat →
croniter; pg_cron → the existing `validate_pg_cron_cron`, `validators.py:94`) so
beat/pg_cron/CLEANUP validate through one path — the same seam #66 (static pg_cron
grammar check) builds on. No scheduler-gating: `CLEANUP` is valid under either
scheduler. pg_cron cron stays DB-authoritative at sync; static grammar validation is
#66.

## Edge cases (accepted for now)

- **`CLEANUP` + `SCHEDULER=pg_cron` but `django_absurd.pg_cron` not installed** — the
  cleanup reconcile lives in that app's post_migrate and would silently no-op, but
  `absurd.E008` already errors on any `SCHEDULER=pg_cron` + app-absent config (keyed on
  the scheduler value, not on SCHEDULE content), so it's caught upstream. No new signal.
- **Two Absurd backends with `CLEANUP` on the same DB** — both target the same shared
  `absurd_cleanup_all` job (last reconcile wins its schedule) / beat tick against the
  same DB. Redundant, not incorrect (cleanup is DB-global + idempotent — a second run
  just finds fewer eligible rows). This design assumes **≤1 Absurd backend per DB**;
  hardening `E004` to forbid same-DB duplicates is tracked in #63. No new check here.

## Testing (behavioral, real DB, no mocks)

- **core suite (beat):** set `CLEANUP` under `SCHEDULER=beat`; seed aged terminal rows
  (short `cleanup_ttl`); drive the beat cleanup firing path once → assert the aged rows
  are deleted and the per-queue counts are logged.
- **pg_cron suite (integration — requested):** set `CLEANUP` + `SCHEDULER=pg_cron`;
  reconcile (`absurd_sync_crons` / migrate) → assert the `absurd_cleanup_all` job exists
  in `cron.job` with the declared schedule and the
  `select * from absurd.cleanup_all_queues(null::text);` command; drop `CLEANUP`,
  reconcile → assert the job is unscheduled. (Job presence + command asserted; actual
  firing is pg_cron-timed, not asserted.)
- Assert the COMPLETE emitted/logged text; alphabetize any parametrize values.

## WHY.md

Rewrite the cleanup/retention + beat-vs-pg_cron reasoning to the declarative `CLEANUP`
model, and add a historical note in the sanctioned "tried X, chose Y because Z" form:
first shipped a user-written `@task` wrapper (a good first step), then replaced it with
declarative `CLEANUP` because it needs zero user code, serves beat AND pg_cron
uniformly, and preserves the no-shipped-`@task` property (beat in-process, pg_cron
native SQL).

## Out of scope

- Partition + detach maintenance (`enable_cron`'s other two jobs) — the broader native
  pg_cron maintenance surface stays in #64/#61. `CLEANUP` is cleanup-only.
- Per-queue cleanup scheduling — `CLEANUP` is backend-global; retention amounts are
  already per-queue policy.
- Static (check-time) pg_cron cron-grammar validation — #66; reuse one cron validator
  across beat / pg_cron / cleanup when that lands.
- Admin visibility for the pg_cron cleanup job — #67 (read-only maintenance panel);
  deliberately not via a `ScheduledTask` row.
- Hardening `E004` to forbid two Absurd backends on one DB — #63.

## Depends on

#65 (`cleanup_queues()`, `absurd_cleanup`, `absurd_flush`) merged first — this reuses
`cleanup_queues()` and rewrites the cleanup docs #65 introduced.
