# Single Absurd Backend + Drop the schedule `alias` — Design (#63)

**Goal:** django-absurd supports exactly one Absurd backend per project, resolved by
capability (`isinstance(..., AbsurdBackend)`), never by the `"default"` name. Remove the
pg_cron-**schedule** `alias` — the `ScheduledTask.alias` field, the jobname segment, the
`--alias` command flags, the per-alias job scans, and the alias-charset rule.

## What drops vs what STAYS (crucial — corrected after adversarial review)

`backend.alias` — the Django TASKS key exposed as `BaseTaskBackend.alias` — is
**load-bearing and STAYS**. The beat scheduler does
`task.using(backend=schedule.backend)` (`scheduler.py:71`, `schedule.backend` =
`backend.alias` set at `:53`), and Django's `Task.using(backend=...)` indexes
`settings.TASKS` by that alias key; `derive_idempotency_key` (`scheduler.py:63`) hashes
it too. Command log/error/prompt strings also read `backend.alias`. None of that changes
— `scheduler.py` is untouched.

What we remove is the pg_cron-**schedule** notion of alias (redundant once there's one
backend):

- `ScheduledTask.alias` field (+ `get_pg_cron_alias_choices`,
  `validate_alias_is_pg_cron_backend`)
- the jobname alias segment: `_dj:{source}:{alias}:{name}` → `_dj:{source}:{name}`
- `--alias` command flags + `resolve_backend`'s alias selection
- per-alias job scans
- the alias-**charset** rule (alias no longer lands in a jobname → nothing to constrain)

## Decision + why

One Absurd system per project — deliberate design, not a temporary limit. `>1`
`AbsurdBackend` is forbidden **regardless of DB**: two aliases pointing Absurd at the
same database is nonsense (one schema, one queue set, split config = confusion), and
distinct DBs are the deferred multi-DB boundary. The codebase already assumes one Absurd
DB (`resolve_absurd_database`, `absurd_cleanup_all` authority, pg_cron single
`cron.database_name`, per-DB migrations, UNION-ALL admin views). Enforcing "at most one"
makes it real. Soft, liftable hint — but genuinely one-per-project. Also supersedes
#63's shipped-`@task` loose spot (moot: no shipped `@task`) and the silent
`resolve_absurd_database` fallback (now validated by the check).

## Topology

At most one `AbsurdBackend`, at ANY TASKS alias. `0` = feature unused (lenient — router
hot-path must never raise). `>1` (same OR distinct DB) = `absurd.E004`. Non-Absurd task
backends coexist freely. **Behavior change:** two Absurd backends on the same DB were
previously allowed (disambiguated by `--alias`) — now an error.

## Enforcement — repurpose `absurd.E004` (checks.py)

Error when `len(get_absurd_backends()) > 1` (was `>1 distinct DB` — now a subset,
unreachable). Message/hint:

```
E004_MSG  = "django-absurd: more than one Absurd backend is configured."
E004_HINT = "django-absurd uses a single Absurd backend per project — configure exactly one AbsurdBackend in TASKS."
```

## Touchpoint table

| File                                       | What                                                                                                                                                                                                                                                                    | Change                                                                                                                                                                                   |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `checks.py`                                | `E004`                                                                                                                                                                                                                                                                  | broaden to `count > 1`; new msg/hint above                                                                                                                                               |
| `queues.py`                                | `resolve_absurd_database` / `get_absurd_backend`                                                                                                                                                                                                                        | already capability-based; KEEP the lenient `0 → "default"` fallback (router never-raise); the `>1` branches become unreachable but STAY as router-safe defense                           |
| `management/base.py`                       | `resolve_backend`                                                                                                                                                                                                                                                       | return the one backend (drop the alias tuple element); `0` → `CommandError` "no Absurd backend configured"; callers read `backend.alias` for messages                                    |
| `management/commands/absurd_beat.py`       | `--alias`                                                                                                                                                                                                                                                               | remove the flag                                                                                                                                                                          |
| `management/commands/absurd_worker.py`     | `--alias`                                                                                                                                                                                                                                                               | remove the flag; the "not declared for backend '{alias}'" error reads `backend.alias`                                                                                                    |
| `management/commands/absurd_sync_crons.py` | `--alias` + alias in confirm/output                                                                                                                                                                                                                                     | remove the flag; teardown prompt/output read `backend.alias`                                                                                                                             |
| `pg_cron/validators.py`                    | `build_jobname` / `build_jobname_prefix`                                                                                                                                                                                                                                | drop the `alias` arg + segment → `_dj:{source}:{name}` / `_dj:{source}:`                                                                                                                 |
| `pg_cron/validators.py`                    | `validate_alias_charset`                                                                                                                                                                                                                                                | DELETE (alias no longer in jobname)                                                                                                                                                      |
| `pg_cron/checks.py`                        | `check_pg_cron_alias` + `E007_HINT_PG_CRON_ALIAS` + alias threading in `validate_pg_cron_schedule`/`check_pg_cron_name`                                                                                                                                                 | DELETE / stop threading alias                                                                                                                                                            |
| `pg_cron/checks.py`                        | `E007_HINT_PG_CRON_JOBNAME`                                                                                                                                                                                                                                             | update the `_dj:s:<alias>:<name>` example → `_dj:s:<name>`                                                                                                                               |
| `pg_cron/models.py`                        | `ScheduledTask.alias`, `get_pg_cron_alias_choices`, `validate_alias_is_pg_cron_backend`                                                                                                                                                                                 | DELETE                                                                                                                                                                                   |
| `pg_cron/models.py`                        | `unique_together`                                                                                                                                                                                                                                                       | `(("source", "alias", "name"),)` → `(("source", "name"),)`                                                                                                                               |
| `pg_cron/models.py`                        | `get_managed_jobs` / `unschedule_matching` / `prune_jobs_without_rows`                                                                                                                                                                                                  | drop the `alias` param; scan `_dj:{source}:` / `_dj:`                                                                                                                                    |
| `pg_cron/models.py`                        | `schedule_pg_cron_job` (`:283`)                                                                                                                                                                                                                                         | this builds the pg_cron job COMMAND — drop the positional `alias` arg to the run-wrapper                                                                                                 |
| `pg_cron/migrations/0001_initial.py`       | regenerate (unreleased)                                                                                                                                                                                                                                                 | no `alias` column; `unique_together (source, name)`; run-wrapper `django_absurd_run_scheduled(p_source, p_name)` (no `p_alias`, no `AND alias = p_alias`); `DROP FUNCTION …(text, text)` |
| `pg_cron/admin.py`                         | ~8 refs: form field (`:41`), create-form `fields` (`:88`), `clean()` (`:104-111`, derives backend from chosen alias → resolve the single backend), fieldsets (`:134`,`:154`), `ordering` (`:137`), `list_display` (`:140`), `list_filter` (`:148`), `readonly` (`:233`) | scrub all; `clean()` resolves the one backend                                                                                                                                            |
| `scheduler.py`                             | `Schedule.backend` / `spawn_scheduled` / `derive_idempotency_key`                                                                                                                                                                                                       | UNCHANGED — `backend.alias` stays                                                                                                                                                        |
| `pg_cron/reconcile.py`                     | `:30`,`:154` "#63" deferral comments                                                                                                                                                                                                                                    | leave (different concern: cleanup-job arbitration) — call out in review                                                                                                                  |

## Tests — delete vs edit

**Delete** (test removed behavior):
`test_scheduler.py::test_absurd_beat_multiple_backends_requires_alias`;
`test_worker.py::test_ambiguous_alias_requires_flag`;
`test_run_scheduled_fn.py::test_disambiguation_by_alias`;
`test_admin/test_scheduledtask.py::test_add_view_alias_field_labeled_alias`; the
bad-alias-charset cases in `test_pg_cron_checks.py` (~238-281).

**Edit:** `test_checks.py::test_multiple_backends_distinct_db_errors` → "more than one
backend errors" (any DB), assert new E004 message; jobname assertions
(`test_pg_cron_naming.py`, `test_pg_cron_sync_jobs.py`, `test_pg_cron_post_migrate.py`,
`test_absurd_sync_crons_command.py`) → `_dj:s:{name}`; `test_jobname_length.py` +
`test_pg_cron_checks.py:132` budget (shorter prefix); `test_scheduledtask_model.py`
uniqueness → `(source, name)`; `test_admin/test_scheduledtask.py` remove alias refs,
uniqueness-error string → "Source and Name"; run-wrapper positional callers
(`test_run_scheduled_fn.py`, `test_pg_cron_post_migrate.py`) → 2 args.

**Add:** resolution with the single backend at a NON-`"default"` alias; E004 with two
backends on the SAME DB.

## Docs

Concrete alias spots to edit: `AGENTS.md:214` (`--alias`), `:386` (run-wrapper sig),
`:415` ("Backend (alias)"), `:428` ("alias … immutable"), `:460-462`
(`_dj:s:<alias>:<name>`); `docs/web/cron-jobs.md` (jobname + alias); `README` if any.
`examples/` need no textual change (settings SCHEDULE, backend at `"default"`, no
`--alias`) — re-run `pg_cron`/`beat` demos to confirm exit 0.

## Out of scope

- Multiple Absurd backends / multi-Absurd-DB (deferred boundary; soft E004 hint keeps it
  liftable).
- Broader pg_cron migration consolidation beyond the `0001_initial` regen this requires.
- `SCHEDULER`-option removal (#68) — separate.
