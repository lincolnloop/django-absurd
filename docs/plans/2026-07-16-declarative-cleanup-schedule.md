# Declarative CLEANUP schedule — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One declarative `OPTIONS["CLEANUP"] = {"schedule": "<cron>"}` backend knob
that runs per-queue retention on a cadence under both schedulers — beat fires
`cleanup_queues()` in-process; pg_cron schedules a native
`select absurd.cleanup_all_queues()` job — replacing the user-written `@task` wrapper
entirely.

**Architecture:** Reuses `cleanup_queues()` (#65). beat: a non-task firing branch seeded
into `run_beat`. pg_cron: a standalone cron job `django_absurd_cleanup_<alias>` outside
the managed `ScheduledTask` (`absurd:`) namespace, with its own reconcile + teardown.
New `absurd.E010` validates the option.

**Tech Stack:** Django 6.0, psycopg3, croniter, pg_cron, pytest (function-based, real
Postgres).

**Spec:** `docs/specs/2026-07-16-declarative-cleanup-schedule-design.md`.

## Global Constraints

- **Depends on #65 merged** (`django_absurd/cleanup.py:cleanup_queues`,
  `absurd_cleanup`, `absurd_flush`). Branch off up-to-date `origin/main` after #65
  lands.
- Django 6.0 / Python 3.12 floor. `import typing as t` only; absolute imports;
  verb-named functions; no leading-underscore module constants.
- pytest function-based; no `unittest.mock` / monkeypatch; behavioral through real
  entrypoints.
- Assert the COMPLETE emitted/logged message text, never a fragment. Alphabetize
  `@parametrize` values + fixture params.
- Full patch coverage (100% statement + branch) on added lines.
- **No production code in this plan** — steps show the RED test, then describe the
  minimal implementation in prose (project rule). Implementer writes the code.
- `CLEANUP` cleans all the backend's queues (`cleanup_queues(None)`); it's valid under
  either scheduler; assumes ≤1 Absurd backend per DB (see spec Edge cases).
- Two Postgres services must be up: `docker compose up -d db db_pg_cron`. Suites run
  separately: `uv run pytest tests/core`, `uv run pytest tests/pg_cron`.

---

## File Structure

- `django_absurd/backends.py` (modify) — `AbsurdBackendOptions` gains `CLEANUP`.
- `django_absurd/checks.py` (modify) — extract a beat-cron validator; add `E010` +
  `check_absurd_cleanup_config`.
- `django_absurd/scheduler.py` (modify) — `get_cleanup_schedule`, `fire_cleanup`,
  `run_beat` seed + guard.
- `django_absurd/pg_cron/reconcile.py` + `pg_cron/validators.py` (modify) — cleanup-job
  jobname helper, schedule/unschedule reconcile, teardown.
- Docs: `django_absurd/AGENTS.md`, `docs/web/cleanup.md`, `docs/web/configuration.md`,
  `docs/WHY.md`.
- Tests: `tests/core/test_checks.py`, `tests/core/test_cleanup.py` (beat), new
  `tests/pg_cron/test_cleanup_schedule.py`.

---

## Task 1: `CLEANUP` option + `E010` validation

**Files:**

- Modify: `django_absurd/backends.py` (`AbsurdBackendOptions`)
- Modify: `django_absurd/checks.py` (extract beat-cron validator;
  `E010_MSG`/`E010_HINT`; `check_absurd_cleanup_config`)
- Test: `tests/core/test_checks.py`

**Interfaces:**

- Produces: `AbsurdBackendOptions["CLEANUP"]: dict` (shape `{"schedule": str}`); a
  reusable `is_valid_beat_cron(cron) -> bool` (or similar) extracted from
  `validate_schedule`'s inline `croniter.croniter.is_valid`;
  `check_absurd_cleanup_config` registered under the `"absurd"` tag.
- Consumes: existing `checks.py` backend-iteration pattern (mirror
  `check_absurd_schedule_config`, `checks.py:200`).

Notes for the implementer:

- `E010` validates, per Absurd backend with a `CLEANUP` key: it's a dict; has a
  non-empty str `"schedule"`; no unknown keys (only `"schedule"`); and — **beat only** —
  the cron passes `croniter` (pg_cron cron stays DB-authoritative at sync, per the
  schedule stance; do NOT DB-probe at check time). Assert the full `E010` message text
  in tests.
- Reuse: extract the beat-cron validity check `validate_schedule` does inline
  (`checks.py:~282`) into a small helper and call it from both places (DRY; seam for
  #66).

- [ ] **Step 1: Write failing checks — malformed CLEANUP shapes (parametrized) + valid
      passes**

Add to `tests/core/test_checks.py` (mirror the existing check-test style — set
`settings.TASKS`, run `call_command("check", "django_absurd")` or the project's
`run_absurd_check` helper with `capsys`, assert the full message). Cover, alphabetized:
bad-cron (beat), non-dict CLEANUP, missing `schedule`, unknown key. Assert the complete
`absurd.E010` message + hint text inline. Add one test that a VALID `CLEANUP` (beat and
pg_cron) emits no `E010`.

- [ ] **Step 2: Run them — verify they fail**

Run: `uv run pytest tests/core/test_checks.py -k cleanup -v` Expected: FAIL — no `E010`
emitted (check doesn't exist yet).

- [ ] **Step 3: Implement (prose)**

Add `CLEANUP: dict[str, t.Any]` to `AbsurdBackendOptions`. Extract the beat-cron
validity helper in `checks.py`. Define `E010_MSG` ("django-absurd: invalid CLEANUP
option.") + `E010_HINT` (points at `OPTIONS["CLEANUP"] = {"schedule": "<cron>"}`). Add
`check_absurd_cleanup_config` (`@register("absurd")`) iterating Absurd backends, reading
`OPTIONS["CLEANUP"]`, emitting `E010` for each malformed case above; validate the beat
cron via the extracted helper. Keep `msg` = problem, `hint` = resolution (project check
convention).

- [ ] **Step 4: Run — verify pass**

Run: `uv run pytest tests/core/test_checks.py -k cleanup -v` Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/backends.py django_absurd/checks.py tests/core/test_checks.py
git commit -m "feat: CLEANUP backend option + absurd.E010 validation"
```

---

## Task 2: beat in-process cleanup firing

**Files:**

- Modify: `django_absurd/scheduler.py` (`get_cleanup_schedule`, `fire_cleanup`,
  `run_beat`)
- Test: `tests/core/test_cleanup.py`

**Interfaces:**

- Consumes: `django_absurd.cleanup.cleanup_queues()` (#65);
  `run_beat(backend, *, now, stop, wait)` injectable seams (`scheduler.py:84-89`); the
  `run_beat_until` test helper pattern (`tests/core/test_scheduler.py:138`).
- Produces: `get_cleanup_schedule(backend) -> str | None`;
  `fire_cleanup(backend, slot)`; `run_beat` fires cleanup on its cadence even with an
  empty `SCHEDULE`.

Notes for the implementer:

- `run_beat` currently returns early when `SCHEDULE` is empty (`scheduler.py:92-95`) — a
  CLEANUP-only backend must still tick. Change the guard to account for a cleanup
  cadence; seed a reserved cleanup key into `upcoming` (a sentinel that cannot collide
  with a schedule name); in the firing loop, dispatch the reserved key to `fire_cleanup`
  and reschedule it via the cleanup cron.
- `fire_cleanup` MUST mirror `fire_schedule`/`spawn_scheduled`: wrap in try/except (a
  raise must not kill the loop) and bracket with `close_old_connections()`
  (`scheduler.py:66,81`). It calls `cleanup_queues()` and logs the per-queue counts
  (past-tense log line).

- [ ] **Step 1: Write the failing beat-cleanup test**

Add to `tests/core/test_cleanup.py` (reuse `sync_queue` / `drain` / the aged-row seeding
already there; drive beat with the injected `wait`/`now` seam like `run_beat_until`).
Test: CLEANUP-only backend (`SCHEDULER=beat`, no `SCHEDULE`,
`OPTIONS["CLEANUP"]={"schedule": "* * * * *"}`), seed one aged terminal task (short
`cleanup_ttl`), run the beat loop until just past the first minute slot → assert the
aged row is deleted (via `cleanup_queues()` observation or the queue's task count). This
proves the guard change + seed + in-process firing together.

- [ ] **Step 2: Run — verify it fails**

Run: `uv run pytest tests/core/test_cleanup.py -k beat -v` Expected: FAIL — beat returns
early (empty SCHEDULE) so nothing fires.

- [ ] **Step 3: Implement (prose)**

Add `get_cleanup_schedule(backend)` reading `OPTIONS["CLEANUP"]["schedule"]` (or
`None`). Add a `fire_cleanup(backend, slot)` mirroring `fire_schedule` (try/except +
`close_old_connections` bracket) that calls `cleanup_queues()` and logs counts. In
`run_beat`: compute the cleanup cron; change the early-exit guard so a cleanup-only
backend proceeds; seed the reserved cleanup key into `upcoming`/rescheduling; dispatch
it to `fire_cleanup` in the firing loop.

- [ ] **Step 4: Run — verify pass + no regression in the beat suite**

Run: `uv run pytest tests/core/test_cleanup.py -k beat tests/core/test_scheduler.py -v`
Expected: PASS (existing beat tests unaffected).

- [ ] **Step 5: Commit**

```bash
git add django_absurd/scheduler.py tests/core/test_cleanup.py
git commit -m "feat: beat runs CLEANUP in-process on cadence"
```

---

## Task 3: pg_cron cleanup-job reconcile + teardown

**Files:**

- Modify: `django_absurd/pg_cron/validators.py` (cleanup jobname helper)
- Modify: `django_absurd/pg_cron/reconcile.py` (schedule/unschedule in `sync_crons` +
  `reconcile_crons_after_migrate` + `teardown_crons`)
- Test: `tests/pg_cron/test_cleanup_schedule.py` (create)

**Interfaces:**

- Consumes: `open_locked_cursor` (`pg_cron/models.py:311`); `resolve_absurd_database`;
  the reconcile entry points `sync_crons` (`reconcile.py:70`),
  `reconcile_crons_after_migrate` (`apps.py:58`), `teardown_crons` (`reconcile.py:146`);
  the cron.job query pattern (`tests/pg_cron/test_pg_cron_sync_jobs.py:29-40`).
- Produces: a `build_cleanup_jobname(alias) -> str` returning
  `django_absurd_cleanup_<alias>` (NOT in the `absurd:` prefix — never swept by
  `get_managed_jobs()`); reconcile that schedules/unschedules that job.

Notes for the implementer:

- The cleanup job command is the static literal `select absurd.cleanup_all_queues()` —
  no interpolated data, no injection surface. Schedule via
  `cron.schedule(<jobname>, <cleanup schedule>, 'select absurd.cleanup_all_queues()')`
  through `open_locked_cursor`; unschedule via `cron.unschedule(<jobname>)` (guard the
  not-found case like `prune_pg_cron_jobs`, `models.py:320`).
- Reconcile is stateless (no `ScheduledTask` row): if the backend has `CLEANUP` →
  schedule/update; else → unschedule. Wire it into `sync_crons`,
  `reconcile_crons_after_migrate` (per-backend, after the schedule sync), and
  `teardown_crons` (always unschedule the cleanup job — it's outside the managed
  prefixes, so nothing else removes it; this closes the leak the review flagged).
  pg_cron validates the cron itself (`cron.schedule` raises on bad grammar → surfaces as
  `CommandError` in the command, skip-with-log at migrate — match the existing stance).

- [ ] **Step 1: Write the failing pg_cron integration tests**

Create `tests/pg_cron/test_cleanup_schedule.py`
(`pytestmark = pytest.mark.django_db(transaction=True)`; set `settings.TASKS` with
`SCHEDULER=pg_cron` + `OPTIONS["CLEANUP"]={"schedule": "17 * * * *"}` + declared
`QUEUES`). Tests, using the `cron.job` query pattern from `test_pg_cron_sync_jobs.py`:

- after `call_command("absurd_sync_crons")`: `cron.job` has
  `django_absurd_cleanup_default` with schedule `17 * * * *` and command
  `select absurd.cleanup_all_queues()`. Assert the complete row tuple.
- `ScheduledTask.pg_cron.get_managed_jobs() == []` — the cleanup job is NOT in the
  managed `absurd:` namespace.
- drop `CLEANUP`, re-sync → the cleanup job is gone from `cron.job`.
- `call_command("absurd_sync_crons", "--teardown")` (or the teardown flag/path) → the
  cleanup job is gone.

- [ ] **Step 2: Run — verify they fail**

Run: `uv run pytest tests/pg_cron/test_cleanup_schedule.py -v` Expected: FAIL — no
cleanup job scheduled. (If `--create-db` is needed and blocked by the pg_cron launcher,
use the ALLOW_CONNECTIONS-false + terminate dance from `CLAUDE.md` first.)

- [ ] **Step 3: Implement (prose)**

Add `build_cleanup_jobname(alias)` to `pg_cron/validators.py`. Add reconcile logic in
`pg_cron/reconcile.py` that schedules `select absurd.cleanup_all_queues()` under that
jobname when the backend declares `CLEANUP`, else unschedules it; call it from
`sync_crons`, `reconcile_crons_after_migrate`, and `teardown_crons`. Use
`open_locked_cursor` + the not-found-safe unschedule.

- [ ] **Step 4: Run — verify pass + no regression in the pg_cron suite**

Run:
`uv run pytest tests/pg_cron/test_cleanup_schedule.py tests/pg_cron/test_absurd_sync_crons_command.py tests/pg_cron/test_pg_cron_teardown.py -v`
Expected: PASS (existing `get_managed_jobs() == []` teardown assertions still hold).

- [ ] **Step 5: Commit**

```bash
git add django_absurd/pg_cron/validators.py django_absurd/pg_cron/reconcile.py tests/pg_cron/test_cleanup_schedule.py
git commit -m "feat: pg_cron schedules a standalone cleanup job from CLEANUP"
```

---

## Task 4: docs + WHY (declarative-only)

**Files:**

- Modify: `django_absurd/AGENTS.md`, `docs/web/cleanup.md`, `docs/web/configuration.md`,
  `docs/WHY.md`

**Interfaces:** none (docs). Verification is `uvx zensical build` + grep.

- [ ] **Step 1: Rewrite the user docs**

In `django_absurd/AGENTS.md` and `docs/web/cleanup.md`: **delete** the "write a `@task`
wrapper + schedule it" content entirely. Add the declarative story:
`OPTIONS["CLEANUP"] = {"schedule": "<cron>"}` runs cleanup on cadence under beat or
pg_cron; keep the on-demand `absurd_cleanup` command + `cleanup_queues()` as the
programmatic/ad-hoc path. Link the
[Absurd cleanup docs](https://earendil-works.github.io/absurd/cleanup/) (per the
source-docs rule). Add a `CLEANUP` row to `docs/web/configuration.md`'s Backend
`OPTIONS` table and the `absurd.E010` row to its check-ID table.

- [ ] **Step 2: Rewrite WHY.md**

Replace the cleanup/beat-vs-pg_cron reasoning with the declarative `CLEANUP` model, and
add the sanctioned historical note: first shipped a user-written `@task` wrapper (a good
first step), then replaced it with declarative `CLEANUP` because it needs zero user
code, serves beat AND pg_cron uniformly, and preserves the no-shipped-`@task` property
(beat in-process, pg_cron native SQL).

- [ ] **Step 3: Build + grep for stale references**

Run: `uvx zensical build` → expect `No issues found`. Run:
`grep -rn "cleanup_queues" django_absurd/AGENTS.md docs/web/` → no "write a wrapper /
SCHEDULE a cleanup task" guidance remains (only the declarative + on-demand +
programmatic mentions).

- [ ] **Step 4: Commit**

```bash
git add django_absurd/AGENTS.md docs/web/cleanup.md docs/web/configuration.md docs/WHY.md
git commit -m "docs: declarative CLEANUP replaces the cleanup @task wrapper"
```

---

## Self-Review

**Spec coverage:**

- Config `OPTIONS["CLEANUP"]` → Task 1. ✓
- beat in-process firing (seed + guard + fire_cleanup + close_old_connections +
  try/except) → Task 2. ✓
- pg_cron standalone job `django_absurd_cleanup_<alias>`, own reconcile + teardown,
  outside `absurd:` namespace → Task 3. ✓
- Validation `E010` + reuse beat-cron helper → Task 1 (beat cron) + Task 3 (pg_cron cron
  at sync). ✓
- Removed wrapper docs; kept command; WHY historical note → Task 4. ✓
- Edge cases (E008 covers app-absent; ≤1 backend/DB) → no task (existing E008 +
  assumption; #63). ✓
- Testing: core beat + pg_cron integration, both requested → Tasks 2, 3. ✓
- Out of scope (#61 partition/detach, #66 static cron check, #67 admin, #63
  multi-backend) → untouched. ✓

**Placeholder scan:** none — each code step is a real RED test; implementation steps are
prose by project rule (no production code blocks).

**Type consistency:** `cleanup_queues()` / `OPTIONS["CLEANUP"]["schedule"]` /
`get_cleanup_schedule` / `fire_cleanup` / `build_cleanup_jobname` / `E010` used
consistently across tasks; jobname `django_absurd_cleanup_<alias>` is the single
spelling everywhere.
