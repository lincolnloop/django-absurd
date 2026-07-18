# Single Absurd Backend + Drop `alias` — Design (#63)

**Goal:** django-absurd supports exactly one Absurd backend per project, resolved by
capability (`isinstance(..., AbsurdBackend)`), never by the `"default"` alias name. The
now-redundant `alias` is removed everywhere.

## Decision + why

One Absurd system per project — deliberate design, not a temporary limit. Multiple would
be a management nightmare (separate schemas, queues, pg_cron `cron.database_name` per
cluster). The codebase already assumes one Absurd DB: `resolve_absurd_database`, the
`absurd_cleanup_all` authority, pg_cron's single `cron.database_name`, per-DB
migrations/provisioning, and the read-only UNION-ALL admin view models. Enforcing "at
most one AbsurdBackend" makes that assumption real instead of hand-waved. Soft, liftable
hint (revisit on community demand) — but genuinely one-per-project.

Supersedes the loose spots in #63: shipped-`@task` `"default"`-binding is already moot
(no shipped `@task`; cleanup is a plain function), and silent `resolve_absurd_database`
fallback is now validated by the check.

## Topology

At most one `AbsurdBackend`, at ANY TASKS alias (e.g. `"default"` or `"myabsurd"`). `0`
= feature unused (lenient — must not crash; router hot-path). `>1` = `absurd.E004`
error. Other (non-Absurd) task backends coexist freely.

## Components

**1. Enforcement — repurpose `absurd.E004` (checks.py).** Error when
`len(get_absurd_backends()) > 1` (was: >1 distinct DB — a strict subset, now
unreachable). Message/hint:

```
E004_MSG  = "django-absurd: more than one Absurd backend is configured."
E004_HINT = "django-absurd uses a single Absurd backend per project — configure exactly one AbsurdBackend in TASKS."
```

**2. Resolution (queues.py) — already capability-based.** `resolve_absurd_database()` /
`get_absurd_backend()` already iterate `get_absurd_backends()` (isinstance-filtered), so
they work at any alias today. Keep the lenient `0 → "default"` fallback: the router
(`db_for_read`/`db_for_write`/`allow_migrate`) calls `resolve_absurd_database()` on
every Absurd-model query, so it MUST always return a DB and never raise. Minimal change.

**3. Commands (management/base.py, absurd_beat, absurd_worker).** Remove the `--alias`
flag. `resolve_backend()` collapses to "return the one Absurd backend": `1` → it; `0` →
`CommandError` "no Absurd backend configured"; `>1` cannot occur (E004 catches it, and
defensively the same error). Return type drops the alias tuple element.

**4. Jobname (pg_cron/validators.py).** `_dj:{source}:{alias}:{name}` →
`_dj:{source}:{name}`. `build_jobname_prefix(alias, source)` →
`build_jobname_prefix(source)` → `_dj:{source}:`. `(source, name)` is unique within the
one backend, so the alias segment disambiguated nothing. Jobname-length budget gains 3+
bytes (shorter prefix). `__probe__` name unaffected in shape.

**5. Scans (pg_cron/models.py).** `get_managed_jobs` / `unschedule_matching` /
`prune_jobs_without_rows` drop the `alias` param; per-alias scoping collapses to a
single lane (`_dj:{source}:` / `_dj:`).

**6. Model — the nuke (pg_cron/models.py).** Drop `ScheduledTask.alias`,
`get_pg_cron_alias_choices`, and the `validate_alias_is_pg_cron_backend` validator + its
admin/model references. Model uniqueness becomes `(source, name)`. **Regenerate
`0001_initial`** without the `alias` column — safe because the pg_cron app is entirely
unreleased (absent from `v0.1.0a4`, the latest PyPI release). No drop-column migration;
clean initial.

**7. Run-wrapper SQL (in `0001_initial`).**
`django_absurd_run_scheduled(p_source, p_name)` — drop `p_alias` and the
`AND alias = p_alias` row-lookup filter. The pg_cron job command that `reconcile` builds
drops the alias argument to match.

**8. `queue` is orthogonal — stays.** `ScheduledTask.queue` (+ the `SCHEDULE` OPTIONS
entry's `queue`) picks which Absurd QUEUE the task runs on; independent of `alias`
(which picked the backend). Untouched.

## Error handling

- `absurd.E004` (system check): `>1` AbsurdBackend.
- `CommandError`: a worker/beat command run with `0` AbsurdBackend ("no Absurd backend
  configured").
- Runtime resolution never raises on `0` (router-safe `"default"` fallback).

## Testing

- E004: two `AbsurdBackend`s (any DBs) → error, full message asserted; one/zero → clean.
- Resolution: single backend at a NON-`"default"` alias resolves correctly.
- Jobname: `build_jobname("nightly")` (settings) → `_dj:s:nightly`; admin → `_dj:a:…`;
  prefix helpers.
- Jobname-length: budget shifts (shorter prefix) — hardcoded boundary values updated.
- Commands: `--alias` no longer accepted; single backend used automatically.
- Model: `ScheduledTask` has no `alias` field; `(source, name)` uniqueness; full_clean.
- Run-wrapper: `django_absurd_run_scheduled(p_source, p_name)` fires the row's task on
  the right queue; per-source teardown still works.

## Examples

The `examples/` demos (`pg_cron`, `beat`, `web`) need no textual change: they configure
a settings `SCHEDULE` (task + cron only) with the single backend at `"default"`, and no
compose command passes `--alias`. But #63 changes resolution/commands/jobname
underneath, so re-run the affected demos after the change to confirm they still exit 0
(`docker compose up --build --abort-on-container-exit`). Also sync docs (AGENTS.md,
docs/web, README) per the doc-audience map.

## Out of scope

- Multiple Absurd backends / multi-Absurd-DB (deliberate deferred boundary; the soft
  E004 hint keeps it liftable).
- Broader pg_cron migration consolidation beyond the `0001_initial` regen this requires.
