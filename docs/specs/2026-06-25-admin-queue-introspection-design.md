# Admin queue introspection — design

## Problem

Absurd stores tasks/runs/results across **per-queue tables** (`t_<queue>`, `r_<queue>`,
`c_<queue>`, `e_<queue>`, `w_<queue>`) plus one catalog `absurd.queues`. No way to
browse them in Django. Operators debugging a failed task or watching throughput must
drop to `psql` or run Absurd's separate `habitat` binary. Goal: surface this state in
the Django admin — the look-and-feel operators already know — read-only.

Hard obstacle: table names are dynamic (`<prefix>_<queue>`), so there's no static
`models.py`. Worse, one model per `(queue × prefix)` fragments the admin into N×5
entries.

## Goal

Read-only admin introspection of Absurd state, **one admin entry per entity type** —
five synthesized entities (Tasks, Runs, Checkpoints, Events, Waits) spanning ALL queues
with `queue` as a filter, **plus the existing `Queue` catalog model = six admin entries
total**. Mirrors `habitat`'s UX ("one Tasks page, pick the queue"). Auto-on: declaring
queues + `django.contrib.admin` installed = the entries appear, no per-queue code. "The
six entries" below always means the five synthesized + Queue; `ENABLE_ADMIN` and
`ADMIN_SITE` govern all six uniformly.

## Constraints

- Floor Django 6.0 / Python 3.12; psycopg3 backend. Targets `DATABASES['default']` (or
  the backend's `DATABASE`); the existing `AbsurdRouter` already routes `django_absurd`.
- READ-ONLY. No mutations (no retry/cancel/create/delete). Matches `habitat`. Mutation
  actions explicitly out of scope (see below).
- Absurd shape (pinned 0.4.0, confirmed by source study):
  - catalog `absurd.queues` (PK `queue_name`) enumerates queues. `absurd.list_queues()`.
  - per-queue tables, pure name concat `'<prefix>_' || queue_name`, schema `absurd`:
    `t_` tasks, `r_` runs, `c_` checkpoints, `e_` events, `w_` waits, `i_` idempotency
    (partitioned only — SKIPPED here). Reads hit the parent table; partitioning
    transparent.
  - NO FKs between per-queue tables (relations are `task_id`/`run_id`/`last_attempt_run`
    columns). NO dead-letter table (`state='failed'` on `t_`). Status + success payload
    on `t_`; per-attempt error (`failure_reason`) + timing on `r_`.
  - PKs: `t.task_id`, `r.run_id`, `e.event_name`, **composite**
    `c.(task_id,checkpoint_name)`, `w.(run_id,step_name)`.
- `makemigrations` MUST stay clean — the dynamic models must be invisible to the
  migration autodetector (spiked; see Validated mechanisms).
- No DB access at IMPORT / app-ready (migrations may not have run; DB may be down) —
  model building + registration are settings-only. View building + queries are lazy, at
  admin- request time. NB at request time the admin DOES write:
  `ContentType.get_for_model` creates a contenttype row on first load, and
  view/session/log writes happen — all normal admin behavior; "no DB" applies to import
  only.
- Multi-DB note: `AbsurdRouter` routes only `django_absurd` app reads/writes to the
  Absurd DB. With a NON-default `DATABASE`, the synthesized models (app_label
  `django_absurd`) read the views on the Absurd DB, while `admin.LogEntry` / `sessions`
  / `contenttypes` / `auth` live on `default` — so `default` must have the standard
  admin/auth/sessions/contenttypes tables migrated. In the test suite the Absurd DB IS
  `default`, so this coincides; a non- default-DB deployment is the documented
  expectation, not a tested path here.

## Decisions (locked)

- **Approach D — union view per prefix.** One Postgres `UNION ALL` view per entity type
  stitches every queue's table together with a synthesized `queue` column; one unmanaged
  model maps each view; one read-only `ModelAdmin` each. Five synthesized entries +
  Queue = six, not N×5.
- **Queue source = the live catalog** (`absurd.queues`), i.e. ALL queues present in the
  DB (declared, auto-created, undeclared) — supersedes an earlier declared-only scope,
  which was a constraint of the rejected per-queue-model approach. A view can only union
  tables that exist, and the catalog is exactly the set of existing queues.
- **Synthesized single surrogate pk** per view — a dedicated text column (`queue` +
  natural key), marked `primary_key=True` on the model (use a non-reserved name like
  `admin_pk`, NOT literally `pk`) → admin detail pages work for EVERY prefix, including
  the composite-PK `c_`/`w_` (which a per-queue model could not render natively). This
  is the load-bearing reason D beats per-queue models.
- **Lazy auto-refresh** of views on admin load: each admin queryset compares the live
  catalog queue-set to what the view covers and rebuilds (`DROP VIEW IF EXISTS` +
  `CREATE VIEW` — NOT `CREATE OR REPLACE`, which cannot drop/reorder columns;
  spike-confirmed "cannot drop columns from view") only on drift. Always fresh, zero
  manual steps, no DDL on the enqueue hot path. Self-heals dropped queues — note
  `drop_queue` CASCADE drops the view ITSELF (spike-confirmed), so `get_queryset`
  rebuilds-and-retries once on the resulting error (see Lazy refresh).
- **Auto-on, kill-switch** `OPTIONS["ENABLE_ADMIN"]` (bool, default `True`).
  `django_absurd/admin.py` registers via admin autodiscover.
- **Target sites configurable** `OPTIONS["ADMIN_SITE"]` — `tuple[str, ...]` of dotted
  paths to `AdminSite` INSTANCES. Default `("django.contrib.admin.site",)`. Resolved
  with `django.utils.module_loading.import_string`; all six entries register on EACH
  resolved site (a custom value replaces the default — we do not also register on
  `admin.site`). Public `register_absurd_admin(sites)` seam takes the tuple; the
  settings path calls it. Bad path → fail SOFT (skip registration, no autodiscover
  crash); E006 is the reporting channel (see Components).
- Existing read-only `Queue` model is the sixth entry, registered by this feature on the
  same site(s), gated by the same `ENABLE_ADMIN`.
- `i_` idempotency tables skipped (low value, partitioned-only).
- Mutation actions (retry/cancel/purge) OUT — read-only only.

## Validated mechanisms (spiked against real Postgres, pg18 + Absurd 0.4.0)

Two rounds of throwaway spikes. Round 1 proved the ORM/migration core; round 2 (added
after adversarial review) proved the full templated admin HTTP surface, permissions,
composite-key detail URLs, predicate pushdown on the detail path, and the drop-race.
Findings:

1. **Dynamic unmanaged model, schema-quoted `db_table`** reads real rows. Reuses the
   established `Queue` trick: `db_table = 'absurd"."<name>'` renders
   `"absurd"."<name>"`.
2. **JSONB columns → `JSONField` decode natively** through the ORM (dict/list) — NO
   manual psycopg loader needed (the loader in `connection.py` is only for the SDK's raw
   path).
3. **`makemigrations` pollution defeated by a private `Apps` registry.** Naive
   registration makes the autodetector emit `CreateModel` per model. Defining each model
   with `Meta.apps = <private Apps()>` keeps it out of the global registry →
   autodetector emits nothing → `makemigrations` clean. Admin works off the class
   reference; `ContentType.get_for_model` resolves (NB: that resolution creates a
   contenttype ROW on first admin load — a write; see Constraints).
4. **Union view + synthesized `admin_pk` + queue filter, end-to-end.** Filter by
   `queue`, search, pagination, and `get(pk='<queue>:<natural-key>')` detail all work.
5. **Full templated admin render with a private-registry model.** Real
   `client.get("/admin/")` index (200) LISTS the models in `app_list` (grouping does NOT
   need the model in the global registry), changelist (200) renders rows, and a
   **Checkpoint detail with a nasty `checkpoint_name` (`step/a:b c` — slash, colon,
   space) renders (200)** — admin `quote()` escapes it into the URL (`_2F`, `_3A`,
   `%20`) and round-trips. The composite-PK detail-page concern is RESOLVED for real,
   not extrapolated.
6. **Pushdown is queue-only.** `WHERE queue='x'` prunes UNION ALL arms (`EXPLAIN` scans
   one arm); `WHERE admin_pk='x:...'` does NOT prune (concat expression — scans every
   arm). So the detail path needs help (see Admin behavior → `get_object`).
7. **Permissions gotcha.** With only `has_view_permission`→True, a non-superuser STAFF
   user does NOT see the entries in the index (the index gates on
   `has_module_permission`, which consults `auth_permission` rows that no migration
   creates). Adding `has_module_permission`→True fixes it (spike-confirmed). Superusers
   short-circuit, which is why round 1 missed this.
8. **`DROP+CREATE`, not `REPLACE`.** `CREATE OR REPLACE VIEW` with a changed column set
   errors `cannot drop columns from view`; `DROP VIEW IF EXISTS` + `CREATE VIEW` works.
9. **Drop-race.** `drop_queue('other')` CASCADE-drops the view itself; a subsequent
   query raises `ProgrammingError: relation "absurd.admin_tasks" does not exist`.
   Rebuilding from the (now smaller) catalog restores it → query succeeds. Hence
   rebuild-and-retry-once.

Model factory shape (proven):

```python
from django.apps.registry import Apps

private = Apps()  # one private registry, shared by all the synthesized models

class Meta:
    managed = False
    app_label = "django_absurd"
    db_table = 'absurd"."admin_tasks'   # the view
    apps = private                       # <-- keeps it out of makemigrations
```

View shape (proven), one per prefix, e.g. tasks. Build is always `DROP VIEW IF EXISTS`
then `CREATE VIEW` (never `CREATE OR REPLACE`):

```sql
DROP VIEW IF EXISTS absurd.admin_tasks;
CREATE VIEW absurd.admin_tasks AS
  SELECT 'default'::text AS queue, ('default:' || task_id::text) AS admin_pk, <cols>
    FROM absurd."t_default"
  UNION ALL
  SELECT 'other'::text   AS queue, ('other:'   || task_id::text) AS admin_pk, <cols>
    FROM absurd."t_other";
-- zero queues: SELECT NULL::text AS queue, NULL::text AS admin_pk, <typed-NULL cols> WHERE false
--   (empty, correctly-shaped; column TYPES must match the populated form exactly)
```

## Architecture

### Components

- **`get_absurd_backend()`** (new, in `queues.py` or `backends.py`) — the single-backend
  resolver this feature needs. There is NO existing one: `get_absurd_client` /
  `resolve_absurd_database` resolve a DB-ALIAS string (and silently fall back to
  `"default"` on multi-backend disagreement), not a backend object.
  `get_absurd_backend()` returns the one `AbsurdBackend` to read admin OPTIONS from: of
  the backends in `get_absurd_backends()`, the one whose `DATABASE` equals
  `resolve_absurd_database()`; if several share that DB, the first by `TASKS` insertion
  order wins (documented). E004 already warns on multi-DB at check time. Verb-named per
  house style.
- `django_absurd/admin.py` (new) — autodiscovered. Reads `ENABLE_ADMIN` + `ADMIN_SITE`
  from `get_absurd_backend()`'s OPTIONS. If enabled, `build_admin_models()` builds the
  five view models + collects `Queue` from STATIC specs (no DB), then
  `register_absurd_admin(sites)` registers read-only `ModelAdmin`s on each site.
  `import_string` resolves each `ADMIN_SITE` path; an unresolvable/non-`AdminSite` path
  is skipped (fail soft) so autodiscover never crashes — E006 reports it.
  `register_absurd_admin(sites)` is the public seam.
- **`build_union_view_sql()` / `rebuild_admin_view()`** (new,
  `django_absurd/admin_views.py`) — given a prefix spec + the catalog queue list, emit
  the `DROP VIEW IF EXISTS` + `CREATE VIEW` SQL (and the zero-queue empty form) and
  execute it. Pure SQL-string assembly + two `cursor.execute`. View name
  `admin_<entity>` in schema `absurd`. Verb-named.
- **System check** (new, `absurd.E006`, runs per resolved backend) — if `ENABLE_ADMIN`
  is non-bool, `ADMIN_SITE` is not a tuple/list of str, or any `ADMIN_SITE` path fails
  `import_string` / doesn't resolve to an `AdminSite` instance → config error. Emit one
  E006 per distinct problem (mirrors `validate_queue_policy`'s per-key E003s). `msg`
  states the problem; `hint` states the fix (no duplication, per CLAUDE.md). Settings
  only, no DB.
- **`ADMIN_ENTITY_SPECS`** (new, plain module constant — no leading underscore) — the
  per- prefix source of truth: prefix, view name, `admin_pk` expression, ordered columns
  (name + Django field), which columns are jsonb, whether the entity has
  `state`/`status`, `list_display`, `list_filter`, `search_fields`. Five entries.
  `build_admin_models` and the view-builder both read it, so adding/adjusting a column
  is one edit. Column order/types are fixed here; changing them requires a view rebuild
  (DROP+CREATE handles it).

### Synthesized surrogate pk per prefix (`admin_pk`, natural key after the `queue:` prefix)

- tasks → `queue || ':' || task_id`
- runs → `queue || ':' || run_id`
- events → `queue || ':' || event_name`
- checkpoints → `queue || ':' || task_id || ':' || checkpoint_name`
- waits → `queue || ':' || run_id || ':' || step_name`

(`task_id`/`run_id` are UUIDv7, effectively globally unique; the `queue:` prefix
guarantees uniqueness across queues regardless and makes the detail URL
self-describing.)

### Column specs (from the pinned DDL — full fidelity for detail; `list_display` is a subset)

- **tasks** `t_`: task_id, task_name, params(jsonb), headers(jsonb),
  retry_strategy(jsonb), max_attempts, cancellation(jsonb), enqueue_at,
  first_started_at, state, attempts, last_attempt_run, completed_payload(jsonb),
  cancelled_at, idempotency_key.
- **runs** `r_`: run_id, task_id, attempt, state, claimed_by, claim_expires_at,
  available_at, wake_event, event_payload(jsonb), started_at, completed_at, failed_at,
  result(jsonb), failure_reason(jsonb), created_at.
- **checkpoints** `c_`: task_id, checkpoint_name, state(jsonb), status, owner_run_id,
  updated_at.
- **events** `e_`: event_name, payload(jsonb), emitted_at.
- **waits** `w_`: task_id, run_id, step_name, event_name, timeout_at, created_at.

`state` exists ONLY on tasks + runs; checkpoints have `status` (not `state`); events +
waits have neither (verified vs DDL) — `ADMIN_ENTITY_SPECS` encodes this so
`list_filter` only adds a status/state filter where the column exists. Assumption
(asserted in a test): these five table shapes are identical across
`unpartitioned`/`partitioned` storage modes, so one view-per-prefix is column-safe
across mixed-mode queues; a future Absurd schema that diverges partitioned columns would
break the UNION and must be caught.

### Lazy refresh

`ModelAdmin.get_queryset` ensures the view is current before querying:

- **Cache**: a module-level `dict[str, frozenset[str]]` keyed by view name → the
  queue-set that view was last built with, in THIS process. Plain module global (no
  leading underscore per house style), guarded so concurrent requests in one process
  don't double- build (a simple lock is enough; cross-process races are harmless — see
  below).
- **Compare**: fetch the live catalog set `SELECT queue_name FROM absurd.queues`. If it
  equals the cached set for this view → no DDL, just query (cost = one cheap catalog
  query).
- **Rebuild on drift**: `rebuild_admin_view()` = `DROP VIEW IF EXISTS` + `CREATE VIEW`
  (NOT `REPLACE` — column-set changes; §Validated #8), wrapped in `transaction.atomic`
  on the Absurd DB; then update the cache.
- **Rebuild-and-retry-once**: the query itself is wrapped so that a `ProgrammingError`
  (the view was CASCADE-dropped by a concurrent `drop_queue` between compare and query,
  or built over a table another process just dropped — §Validated #9) triggers ONE
  rebuild from the freshly-read catalog + retry. A second failure → degrade to empty.
- **Degrade quietly**: `(ProgrammingError, OperationalError)` from schema-absent /
  DB-unreachable / persistent race → return an empty queryset, never 500 the admin
  (mirrors `query_queue_state`'s `except (OperationalError, ProgrammingError)` in
  `checks.py`).
- **Concurrency**: under multiprocess gunicorn each worker keeps its own cache;
  redundant `DROP+CREATE` across workers is harmless and serializes on the view's lock.
  The compare is always against the LIVE catalog, so a process with a stale cache still
  rebuilds when the catalog differs.

### Admin behavior (shared read-only base `ModelAdmin`)

- Read-only:
  `has_add_permission = has_change_permission = has_delete_permission = False`,
  `has_view_permission = True`, AND **`has_module_permission = True`** — the last is
  REQUIRED (§Validated #7): without it a non-superuser staff user with view access sees
  nothing in the index (the index gates on module permission, and no `auth_permission`
  rows exist for these migration-less models). `get_readonly_fields` returns ALL fields
  (the view is non-writable; no field may render an editable input). Defense-in-depth:
  models' `save`/`delete` raise (mirror `Queue`).
- **`ordering = ("admin_pk",)`** — admin pagination needs a deterministic unique sort
  key or it can drop/duplicate rows across pages; `admin_pk` is the unique surrogate.
  (Sort is over the union and not arm-prunable — acceptable cost.)
- **`get_object` parses the queue out of `admin_pk`** and adds an explicit `queue=<q>`
  filter alongside the pk lookup, so the detail-page query prunes UNION ALL arms instead
  of scanning every table (§Validated #6). Without this, every detail open scans all
  per-queue tables.
- `list_filter` includes `queue` (a custom `SimpleListFilter` sourcing names from
  `absurd.queues`, NOT a `DISTINCT` over the union) plus `state`/`status` ONLY where the
  entity has that column (§Column specs).
- `search_fields` per prefix (e.g. tasks: `task_id`, `task_name`; runs: `run_id`,
  `task_id`, `claimed_by`).
- Task↔runs: from a Task detail, a read-only link to the Runs changelist filtered to
  that task (`?q=<task_id>`). No native inline (no FK).
- **Count-free paginator** + `show_full_result_count = False` so the changelist never
  runs `COUNT(*)` over a union of large tables.
- JSONB columns render via `JSONField`; large payloads acceptable read-only.

### Lifecycle / teardown

Views are runtime objects in the `absurd` schema, created lazily — NOT migration-managed
(the set is dynamic). `migrate django_absurd zero` drops the schema CASCADE → views go
with it. No separate cleanup needed.

## Testing (real DB, no mocks, at entrypoints)

Test through the admin HTTP surface, not internals. Requires test scaffolding (Task 0),
all confirmed needed by the spike: add `django.contrib.admin`/`sessions`/`messages`
apps, the matching MIDDLEWARE (`SessionMiddleware`, `CommonMiddleware`,
`AuthenticationMiddleware`, `MessageMiddleware`), a `TEMPLATES` config (with the
`request`/`auth`/`messages` context processors), `ROOT_URLCONF` (`tests/urls.py` with
`admin.site.urls`), and a non-empty `SECRET_KEY` (session signing needs it — spike hit
`SECRET_KEY must not be empty` without). Fixtures: a logged-in superuser AND a
staff-only non-superuser. `transaction=True` (DDL: queues, views). Use the Django test
client against admin URLs; assert on rendered content.

- **Empty**: no queues → each changelist renders, shows zero rows (zero-queue view
  path).
- **Union spans queues**: seed `default` + `other` with tasks (run a worker so some
  complete, one `boom` fails) → Tasks changelist lists rows from both; `queue` column
  shows both values.
- **Queue filter**: `?queue=other` narrows to that queue's rows only.
- **Detail per prefix**: open a Task detail by its synthesized pk → renders; `params`
  jsonb visible; `state='completed'`, `completed_payload` shown. A Run detail shows
  `failure_reason` for the failed task. A **Checkpoint and a Wait detail render**
  (proves the synthesized-pk solution for composite-PK tables) — seed via a
  durable/multi-step task or direct fixture rows.
- **Read-only**: GET the add view → 403/redirect; no delete action; change view fields
  read-only.
- **Permissions** (§Validated #7): a staff-only non-superuser sees all six entries in
  the index AND can open a changelist + detail (proves
  `has_module_permission`/`has_view_permission` overrides — would FAIL without them).
  Index render as superuser too.
- **Detail prunes arms** (§Validated #6): opening a Task detail applies the parsed
  `queue=` filter — assert via a Task detail that resolves correctly across two queues
  (and, if cheap, an `EXPLAIN`-style check that `get_object`'s queryset carries the
  `queue=` predicate).
- **Lazy refresh**: create a new queue after first admin load → next changelist load
  includes it (view rebuilt); drop a queue → next load excludes it without erroring.
- **Task→runs link**: Task detail link lands on Runs changelist filtered to that task.
- **makemigrations clean**: with `django_absurd.admin` imported (models registered),
  assert the migration autodetector reports NO changes for `django_absurd` (run the
  autodetector directly against the loaded app registry — avoids the cross-DB
  consistent-history check that trips pytest's sqlite-alias blocker).
- **Lazy refresh — drop race** (§Validated #9): build the view, `drop_queue` one queue,
  then load the changelist → it rebuilds-and-retries and renders (no 500), excluding the
  dropped queue.
- **Kill switch**: `OPTIONS["ENABLE_ADMIN"]=False` → none of the six entries registered
  on any site.
- **Custom site(s)**: `OPTIONS["ADMIN_SITE"]=("tests.admin.custom_site",)` → all six
  register on that site, NOT on the default `admin.site`. A two-entry tuple → registered
  on both.
- **Bad config (E006)**: `ADMIN_SITE` with an unimportable path, a path resolving to a
  non-`AdminSite`, a non-tuple `ADMIN_SITE`, or non-bool `ENABLE_ADMIN` →
  `call_command("check","django_absurd")` emits `absurd.E006` (assert the full message
  text). Separately: a bad `ADMIN_SITE` does NOT crash autodiscover (fail-soft skip).
- **Schema-absent / DB-unreachable**: admin changelist degrades to empty, no 500.

Drive every state with real DB conditions (sync/worker/drop, `override_settings` for the
kill switch) — no monkeypatching.

## Docs / maintenance ripple

This touches user-facing config (two new OPTIONS keys), a new system check, and a new
capability — so it trips the `sync-docs` trigger. The plan's final task runs `sync-docs`
and updates:

- `backends.py` — add `ENABLE_ADMIN` + `ADMIN_SITE` to the `AbsurdBackendOptions`
  TypedDict (currently absent).
- `django_absurd/AGENTS.md` — OPTIONS list + a new "Admin introspection" capability +
  the non-default-DB table-location note + E006 in the checks list.
- `README.md` — mention the admin entries (likely a short section/screenshot-worthy
  bullet).
- `examples/` — wire the admin into the runnable example if it has a project with admin.
- CLAUDE.md (maintenance) — only if a convention changes; likely no edit.

## Out of scope

- Mutation actions (retry, cancel, requeue, purge, queue create/delete/policy edit).
- `i_` idempotency tables; partition-child drill-down (parent reads suffice).
- Multi-DB / multiple Absurd databases (single `DATABASE`, as today).
- Per-queue custom admin classes / overriding a single entity's admin (the factory is
  the public seam; bespoke registration is a later concern).
- Fixing the `compose.yaml` pg18 volume-mount issue surfaced while spiking (separate).
