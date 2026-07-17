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
`SCHEDULER` beat OR pg_cron — no scheduler-gating. Absent → no scheduled cleanup.

## beat path

`run_beat` derives a cleanup cadence from `CLEANUP["schedule"]` alongside the task
schedules. On fire — same forward-only croniter cadence as task schedules — beat calls
`cleanup_queues()` **in-process** against the backend DB and logs per-queue counts. No
enqueue, no `@task`, no worker (beat already holds a DB connection). This is a non-task
firing branch in the beat loop, distinct from `spawn_scheduled`.

## pg_cron path

The pg_cron app's reconcile (post_migrate + `absurd_sync_crons`) schedules **one** cron
job running `select absurd.cleanup_all_queues()` on `CLEANUP["schedule"]`. Managed
statelessly by a deterministic job name in our own namespace (e.g.
`absurd:cleanup:<alias>`) — distinct from task-schedule jobs (`absurd:s:…`) and from
Absurd's own maintenance jobs (`absurd_*`); reconcile observes presence via that name,
no `ScheduledTask` projection row. Dropping `CLEANUP` unschedules it. It is our job, not
`enable_cron`'s 3-job bundle: cleanup-only, so partition/detach stay with #61, and it
**survives `absurd_flush`** (`drop_queue`→`disable_cron` only removes `absurd_*`
per-queue maintenance jobs, never ours). Cron grammar is DB-authoritative — validated by
`cron.schedule` at sync, matching the existing schedule stance.

## Kept (on-demand / programmatic)

`absurd_cleanup` command + importable `cleanup_queues()` stay — ad-hoc "clean now" and
the logic beat itself calls. Not scheduling; `CLEANUP` does not replace them.

## Removed

The user-written `@task` wrapper + the "schedule a cleanup task" guidance — deleted from
`AGENTS.md` and `docs/web/cleanup.md`. `CLEANUP` is the sole scheduled-retention path.

## Validation

`AbsurdBackendOptions` gains `CLEANUP` (TypedDict `{"schedule": str}`). New system check
(`absurd.E010` — next free ID) fires when `CLEANUP` is malformed (not
`{"schedule": <non-empty str>}`). Cron validity is checked where each scheduler already
does it — beat via croniter, pg_cron via `cron.schedule` at sync (loud in the command,
skip-with-log at migrate). No scheduler-gating error: `CLEANUP` is valid under either
scheduler.

## Testing (behavioral, real DB, no mocks)

- **core suite (beat):** set `CLEANUP` under `SCHEDULER=beat`; seed aged terminal rows
  (short `cleanup_ttl`); drive the beat cleanup firing path once → assert the aged rows
  are deleted and the per-queue counts are logged.
- **pg_cron suite (integration — requested):** set `CLEANUP` + `SCHEDULER=pg_cron`;
  reconcile (`absurd_sync_crons` / migrate) → assert the cleanup job exists in
  `cron.job` with the declared schedule and the `select absurd.cleanup_all_queues()`
  command; drop `CLEANUP`, reconcile → assert the job is unscheduled. (Job presence +
  command asserted; actual firing is pg_cron-timed, not asserted.)
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
- Removing the on-demand `absurd_cleanup` command.

## Depends on

#65 (`cleanup_queues()`, `absurd_cleanup`, `absurd_flush`) merged first — this reuses
`cleanup_queues()` and rewrites the cleanup docs #65 introduced.
