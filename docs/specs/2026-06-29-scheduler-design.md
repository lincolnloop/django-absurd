# Scheduler (beat) — design

Issue: [#20](https://github.com/lincolnloop/django-absurd/issues/20). Recurring/periodic
tasks for django-absurd. This spec covers **SP1 only** (see Decomposition). Builds the
shared scheduling core + settings-declared schedules + an in-process **beat** loop that
enqueues tasks on a cron cadence. No pg_cron, no new tables — ships scheduling on any
Postgres.

## Goal

Declare recurring tasks in settings. Long-running `absurd_beat` process wakes on
cadence, enqueues task through existing enqueue path. Celery-beat shape, Absurd engine
underneath.

## Architecture: two axes (whole feature)

Recurring tasks split on two independent axes. Naming both here so SP1 fits the larger
picture; SP1 implements only the marked pieces.

- **Declaration** (what schedules exist): **settings provider** (SP1). Model /
  admin-managed schedules are **out of scope** — settings is the only declaration source
  for the beat approach.
- **Execution** (what fires enqueue on cadence): **beat / application-side loop** (SP1)
  · pg_cron / database-side (SP2).

Declaration feeds whichever execution backend active. SP1 = settings provider + beat.

## Scope (SP1)

In: shared core (`Schedule`, settings provider, `spawn_scheduled`), `arun_beat` loop,
`absurd_beat` command, `absurd_worker --beat`, `OPTIONS["SCHEDULE"]` +
`OPTIONS["SCHEDULER"]`, `E007` checks, `croniter` runtime dep.

Out (own sub-project): pg_cron backend (SP2). Out of scope entirely (not pursued for the
beat approach): Django-model / admin-managed schedules. Also out: idempotency keys (see
Decisions), `--once`/burst mode, backfill/catch-up, per-entry timezone, HA/multi-beat.

## Components / file structure

`django_absurd/scheduler.py` — shared core + loop:

- `Schedule` — frozen dataclass. Fields: `name: str`, `task: str` (dotted path),
  `cron: str` (5-field), `queue: str | None`, `args: list`, `kwargs: dict`. No `enabled`
  — delete the settings entry to disable.
- `get_settings_schedules(backend) -> list[Schedule]` — settings provider. Reads
  `OPTIONS["SCHEDULE"]`. Settings is the only declaration source.
- `spawn_scheduled(schedule) -> None` — resolve dotted path via `import_string` to the
  Task, enqueue through the real Django Tasks path, routing to the schedule's queue via
  `.using(queue_name=...)` when set:
  `task.using(queue_name=schedule.queue).enqueue( *schedule.args, **schedule.kwargs)`
  (skip `.using` when queue is None). Reusing enqueue gives param serialization for free
  — no hand-built params.
- `arun_beat(backend, *, now=..., sleep=..., stop=...) -> None` — async loop. `now`,
  `sleep`, `stop` injectable for tests (see Testing). Per iteration: compute each
  schedule's next fire instant (croniter, **Django timezone**), `await sleep` to
  earliest, fire every schedule due at that slot via `spawn_scheduled`, repeat until
  `stop` set.

`django_absurd/management/commands/absurd_beat.py` — wraps `arun_beat` (asyncio.run),
SIGTERM/SIGINT → set `stop` for clean shutdown. Logs declared-schedule count on start.

`absurd_worker --beat` — worker is already async (`arun_worker`); flag schedules
`arun_beat` as a concurrent task on the same loop. One process runs both.

`django_absurd/checks.py` — add `E007` (below).

Module-layout conventions: verbs in function names, helpers below public fns, absolute
imports, `import typing as t`.

## Settings schema

```python
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "OPTIONS": {
            "SCHEDULER": "beat",          # default; "pg_cron" arrives in SP2
            "SCHEDULE": {
                "nightly-report": {
                    "task": "myapp.tasks.send_report",   # dotted path to a @task
                    "cron": "0 2 * * *",                  # 5-field, Django timezone
                    "kwargs": {"full": True},             # optional
                    "queue": "reports",                   # optional; backend default else
                    "args": [],                           # optional
                },
            },
        },
    },
}
```

`SCHEDULE`: name → spec. Required `task`, `cron`. Optional `queue`, `args`, `kwargs`.
`SCHEDULER`: execution backend selector, default `"beat"` — works on any Postgres, so it
stays the default for everyone. `"pg_cron"` (SP2) is an explicit opt-in requiring
deliberate setup (extension + privileges); never auto-selected.

## Behavior

- **Timezone**: cron interpreted in Django `TIME_ZONE` (operator-intuitive — `0 2 * * *`
  = 2am local). DST edge cases = standard cron behavior (document).
- **Fire-forward-only**: on start / oversleep compute next slot ≥ now; never backfill
  missed slots. Matches pg_cron, keeps SP2 consistent.
- **Failure isolation**: one schedule raising (import error, enqueue error) is caught +
  logged; loop and sibling schedules continue.
- **Single instance**: run exactly **one** beat process. Concurrent beats double-fire
  (operator contract, same as Celery beat). No idempotency guard (see Decisions).
  Document prominently in the command help + user guide.

## System checks (`E007`)

Per `SCHEDULE` entry, no DB access (runs pre-migrate, follows existing `absurd.Exxx`
pattern). `msg` states problem, `hint` states fix:

- `task` imports and is a Django Task (not a bare callable).
- `cron` parses under croniter.
- only known keys (`task`, `cron`, `queue`, `args`, `kwargs`); `args`/`kwargs`
  JSON-serializable.
- `queue` (when given) is declared.

## Dependencies

- Runtime: add `croniter` to `[project] dependencies`. Scheduling is first-class — no
  "croniter missing" degradation branch.
- Dev: add `pytest-asyncio`, `freezegun` to the dev group.

## Testing

Function-based pytest, behavior-driven (assert observable enqueues, not internals).
Drive `check`/command by running them, assert full emitted text per project conventions.

Async loop tested deterministically without real waiting. freezegun controls
`timezone.now()`; inject a fake `sleep` that ticks the frozen clock instead of waiting
(freezegun/asyncio cannot fast-forward a real `asyncio.sleep` — the wait is injected). A
`stop` event terminates the loop after N fires.

Test sketch (RED-first; implementation follows in the plan):

```python
async def test_beat_enqueues_each_due_slot():
    # freeze at 01:59, fake sleep advances frozen clock to the slot, stop after 2 fires
    # assert: send_report landed on its queue at 02:00 and the next slot
    ...
```

Cases: due slot enqueues the task on its queue; args/kwargs reach the task; multiple
schedules fire independently; one failing schedule does not block others; fire-forward
skips a missed slot; `absurd_beat` start/stop is clean; `check` emits `E007` text for
each invalid entry (bad cron, unimportable task, non-Task, unknown key, undeclared
queue, non-serializable args). Tests run on host via uv/tox; Postgres from
`docker compose up -d db`.

## Decisions (resolved in brainstorming)

- **No idempotency keys.** Single-instance + fire-forward-only is already at-most-once
  per slot without a key (post-fire restart recomputes the next _future_ slot).
  Idempotency is insurance against concurrent beats, not correctness; operator owns
  single-instance. pg_cron (SP2) is a single DB-side scheduler — also no concurrency.
  Additive later if HA is ever needed (no redesign).
- **Beat is async** (`arun_beat`), matching `arun_worker`; integrates into the worker
  loop for `--beat`. Accepts a `pytest-asyncio` dev dep.
- **croniter** (not cronsim) per preference; **freezegun** (not time-machine) per
  preference.
- **Django timezone** (not UTC) for cron evaluation.
- Naming: module `scheduler.py`; everything else keeps "beat".

## Decomposition (follow-on sub-project)

Same #20; own issue + spec→plan→build cycle.

- **SP2 — pg_cron backend.** `OPTIONS["SCHEDULER"]="pg_cron"`. DB-side `cron.schedule()`
  → `absurd.spawn_task()`. Hard parts: SQL params must match Django Tasks serialization
  byte-for-byte; ownership-prefix reconcile (upsert + **destructive prune** of owned
  orphans, unlike non-destructive queue sync); align pg_cron timezone to Django
  `TIME_ZONE`; privilege/availability checks (extension present, role has `USAGE` on
  schema `cron`); cannot `CREATE EXTENSION` ourselves (superuser +
  `shared_preload_libraries`) — assume installed, degrade + check.

Explicitly **not pursued**: a Django-model / admin-managed schedule store. The beat
approach declares schedules in settings only.
