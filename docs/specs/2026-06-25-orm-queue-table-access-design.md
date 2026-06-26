# ORM access to queue tables — design

> Revised after an adversarial review (opus). The earlier draft put view DDL on the ORM
> read path behind a COMMENT-hash marker + an `AUTO_MANAGE_VIEWS` flag; the review
> showed that's a latent outage on a public read API. This revision moves all
> view-building to **queue-creation events** (eager) so reads do zero DDL, and drops the
> marker and the flag.

## Problem

The admin-introspection feature already builds per-entity `UNION ALL` views over
Absurd's per-queue tables and maps them with unmanaged models — but those models are
locked inside the admin request path. There's no supported way to query Absurd state
from Python: no `Task.objects.filter(...)`, no cross-queue counts, nothing for
dashboards / reports / shell debugging outside the admin UI. Operators drop to `psql`.

## Goal

Expose the union-view models as a **public, read-only ORM surface** under
`django_absurd.models` — `Task, Run, Checkpoint, Event, Wait` (joining the existing
`Queue`) — queryable like any Django model: chainable `filter` / `order_by` /
`annotate`, spanning ALL queues with `queue` as a column. Views are provisioned
**eagerly when a queue is created** (and by `absurd_sync_queues`), so ORM reads are pure
`SELECT` — no DDL on the read path.

```python
from django_absurd.models import Task
Task.objects.filter(state="failed")                      # across all queues
Task.objects.filter(queue="default", state="failed")     # one queue
Task.objects.values("queue").annotate(n=Count("*"))      # counts per queue
Task.objects.order_by("-enqueue_at")[:20]                # recent, any queue
```

## Constraints

- Floor Django 6.0 / Python 3.12; psycopg3. **Read-only** — no mutation of Absurd state.
- `import typing as t`; absolute imports; verb-named functions; no leading-underscore
  module constants/helpers; helpers BELOW their public function. System-check
  `msg`=problem / `hint`=fix.
- Tests: function-based pytest; no mocks; real DB; assert full emitted text for
  commands/checks.
- `makemigrations` MUST stay clean — the view models stay in the private `Apps` registry
  even though they're exposed via `models.py`.
- **No DB access at import / app-ready.** View DDL happens only inside a queue-creation
  op or `absurd_sync_queues`. NOT in `AppConfig.ready()` (runs for
  migrate/makemigrations/check, before migrations, and when the DB may be down).
- **No DDL on the ORM read path.** A `Task.objects.…` query never issues
  `CREATE`/`DROP VIEW`.
- Builds on / revises the admin feature (shared models, view rename). Touches the
  auto-create-queue seams (enqueue create-branch, worker-start, `sync_queues`) to hook
  view-rebuild into creation.

## Decisions (locked)

- **Surface = view-backed read-only models in `django_absurd.models`** —
  `Task, Run, Checkpoint, Event, Wait` (beside the existing `Queue`). One model per
  entity type; each maps a plain Postgres view unioning that type's per-queue tables;
  `queue` is a synthesized column; `admin_pk` the synthesized surrogate pk. Read-only
  (`save`/`delete` raise). These are the SAME classes the admin registers — one set,
  used by both (no duplication).
- **Honest about being read-only introspection** (the review's B3): synthetic
  `queue:task_id` pk (so `get(pk=…)` takes the surrogate, NOT a bare uuid), no FKs /
  `select_related` (task→runs is by `task_id` column only). Public docs filter by
  columns and do not advertise `get(pk=<uuid>)`.
- **Drop the `Absurd` prefix** on the synthesized models (`app_label` `django_absurd`
  namespaces them; consistent with `Queue`). Ripples: admin `model_name` → admin URL
  names (`admin:django_absurd_task_…`) and the admin tests.
- **Plain (non-materialized) views** — live data, no refresh, ~zero storage. Renamed
  `admin_<entity>` → **`<entity>_view`** (`tasks_view`, `runs_view`, `checkpoints_view`,
  `events_view`, `waits_view`).
- **View provisioning = rebuild-on-create + sync.** A `rebuild_views(using)` rebuilds
  all five views over the current catalog. It runs once at the end of each op that
  CREATES a queue: `absurd_sync_queues` (always, build-all incl. zero-queue),
  worker-start (only if it created its served queue), and the enqueue create-branch
  (only on first-create — **under review, see D1**). Normal enqueues / worker starts
  that don't create a queue never touch views. These are the only runtime drift events
  and are rare; rebuild is metadata-only/cheap → no drift detection needed at these
  seams.
- **No DDL on any read path; no self-heal anywhere; no `AUTO_MANAGE_VIEWS` flag; no
  COMMENT-hash marker.** (All removed per the review + the eager-on-create model.) Admin
  and ORM read the views identically — no `pg_depend` check, no in-process memo, no
  per-request rebuild. This also simplifies the EXISTING admin code: its `get_queryset`
  rebuild-retry + `ensure_view_current` go away; the admin becomes a plain read-only
  `ModelAdmin` over the view.
- **Empty views are created in the schema migration** — so all five exist immediately
  after `migrate` (the mandatory setup step), and a fresh / zero-queue DB works with no
  special-casing. The create-hook + `absurd_sync_queues` rebuild them as the queue set
  changes. (The empty-view form references no tables, so it installs cleanly right after
  the Absurd schema; reverse / schema CASCADE drops them.)
- **`drop_queue`** CASCADE-drops the dependent view → reads break until restored. How
  reads behave in the meantime (tolerant view / typed error / degrade) is **under
  review, see D2** — `absurd_sync_queues` always restores.

## Architecture

### Models (`django_absurd/models.py`)

Expose the five view models as importable symbols via the existing pure-Python,
idempotent factory (`build_admin_model` in `admin_views.py`):
`Task = build_admin_model(spec("tasks"))`, … Built into `PRIVATE_ADMIN_APPS` (private
registry) → absent from the global app registry → `makemigrations` stays clean even
though named in `models.py` (registration target is `Meta.apps`, independent of which
module names the class). Default manager — **no custom self-heal manager** (reads are
plain `SELECT`); the `AbsurdRouter` already routes the `django_absurd` app to the Absurd
DB. Read-only (`save`/`delete` raise — already in the factory). `admin.py` imports these
from `models.py` instead of building its own.

### View build — `admin_views.py`

- `build_union_view_sql(spec, queues)` / `rebuild_view(spec, queues, using)` (exist
  today, renamed views) — `DROP VIEW IF EXISTS` + `CREATE VIEW <union>` in one txn. No
  COMMENT, no marker.
- `rebuild_views(using)` (new) — rebuild all five from the current catalog; called by
  the create hooks and sync. Unconditional at these seams (they're the drift events;
  rare; cheap).
- The five **empty views** are emitted by the schema migration (RunSQL, right after the
  Absurd schema) so they exist post-`migrate`. The existing `ensure_view_current` /
  in-process cache and the admin `get_queryset` rebuild-retry are DELETED (no read-path
  drift logic remains).

### Queue-creation hooks

`rebuild_views(using)` is invoked once after a provisioning op actually creates a queue:

- **`absurd_sync_queues`** — after reconciling all declared queues, `rebuild_views`
  (build-all; the zero-queue empty-view form ensures all five exist on a fresh DB).
- **Worker start** (`absurd_worker` command) — after `reconcile_queue`; only when it
  created the served queue. (A worker whose queue already exists does no view DDL —
  preserves the "worker doesn't churn views" property.)
- **Enqueue create-branch** (`backends.py`) — after the failure-driven `create_queue` +
  retry, `rebuild_views`. Only the first enqueue to a not-yet-created queue pays this (5
  metadata `CREATE VIEW`s in that one rare, already-DDL-heavy, caller-transaction
  enqueue); normal enqueues are untouched.

These run under roles already privileged to create queues (they're creating tables), so
view DDL there is consistent.

### Admin

Becomes a **plain read-only `ModelAdmin` over the view** — its `get_queryset`
rebuild-retry and `ensure_view_current` call are removed (views are provisioned eagerly;
nothing to self-heal). On a missing view (post-`drop_queue`) it degrades that changelist
to empty rather than 500. `model_name`s lose the `Absurd` prefix → admin URL names
change; admin tests repoint `reverse()` targets + the registered-names assertion + drop
the self-heal/concurrent-drop-rebuild tests. The admin registers the `models.py`
classes.

## Drift scenarios (what triggers a rebuild)

Data never drifts a view (it's a query, not a snapshot — task rows/state/retries/deletes
are live). Only these:

- **Queue added** (via sync, worker-start-create, or enqueue-create) → covered eagerly
  by the create hook.
- **Queue dropped** (`drop_queue`) → CASCADE drops the view → re-sync.
- **Column-spec change** (a release edits `ADMIN_ENTITY_SPECS`) or **Absurd
  schema-version bump** → deploy runs `absurd_sync_queues`.
- **Absence** — fresh install: the **schema migration** builds the empty views, so they
  exist post-`migrate` (no sync needed). `migrate … zero` / manual `DROP` / `drop_queue`
  CASCADE remove a view → next `absurd_sync_queues` (or queue-create) restores it; reads
  error/degrade meanwhile.
- **NOT** drift: partitioned vs unpartitioned storage (reads hit the same parent table
  name; uniform column shapes — verified).

## Public API

`from django_absurd.models import Task, Run, Checkpoint, Event, Wait` (and `Queue`).
Read-only, chainable querysets over the views; `queue` + `admin_pk` synthesized; columns
per the admin spec. Provisioned by queue-creation + `absurd_sync_queues`; reads are pure
`SELECT`.

## Internal consumers (one source of truth)

The ORM models are not just a public surface — the rest of django-absurd consumes them,
collapsing duplicated schema knowledge into the `EntitySpec`/models:

- **Admin consumes the ORM.** The admin's `ModelAdmin`s are built ON these models (the
  same `Task`/`Run`/… classes), not a separate admin-only model build. One definition,
  used by both. (Already the direction; made explicit.)
- **`get_result` consumes the ORM.** `backends.py`'s result-retrieval path currently
  runs bespoke raw SQL over `t_<queue>`/`r_<queue>` (joining task + last-attempt run to
  build a `TaskResult`). Refactor it to query the `Task`/`Run` models instead — so
  task/run column knowledge lives in ONE place (the `EntitySpec`), and an Absurd schema
  change that breaks a column surfaces through the models/tests, not a stray SQL string.
  De-risks schema drift. (The Django Tasks `get_result` _API_ is unchanged — only its
  implementation.) Constraint: `get_result` runs on a single known queue+task_id → query
  the per-queue data via the model with `queue=`+`task_id` filters (pruning to one arm),
  preserving its current behavior/return shape; keep its existing error semantics
  (`TaskResultDoesNotExist`). Depends on the view for that queue existing — same
  provisioning contract as the rest.

## Testing (real DB, no mocks)

- **ORM query, provisioned by creation (not the admin path):** create queues via the
  public seams (`absurd_sync_queues` / a worker start / an enqueue), then
  `Task.objects.filter(queue=…, state=…)`, cross-queue
  `.values("queue").annotate(Count("*"))`, `.order_by(...)[:N]` return correct
  rows/counts. Read-only: `Task().save()` raises.
- **Eager-on-create at each seam:** (a) `absurd_sync_queues` builds all five views; (b)
  a worker start that creates its served queue rebuilds views (queue then visible via
  `Task.objects`); (c) the first enqueue to a not-yet-created declared queue rebuilds
  views (queue visible).
- **No DDL on the ORM read path:** querying `Task.objects` does NOT change the view
  (e.g. OID stable across queries; no `CREATE` issued).
- **No churn:** a worker start / enqueue to an already-existing queue does NOT rebuild
  views (view OID unchanged).
- **Migration builds empty views:** after `migrate` only (no sync, zero queues), all
  five `<entity>_view` exist; the admin changelist and `Task.objects.all()` render
  empty, no error.
- **`drop_queue` (per D2):** after dropping a queue, reads behave per the chosen D2
  option (tolerant view → that queue's rows vanish, others unaffected; OR a typed "run
  sync" error — no raw psycopg leak); `absurd_sync_queues` restores full rows. (Finalize
  once D2 is decided.)
- **`get_result` via ORM (behavior unchanged):** for completed / failed / pending tasks,
  the refactored `get_result` returns the same `TaskResult` (status, return_value,
  failure, timing, worker_ids) as the raw-SQL version did — assert against the existing
  `test_results.py` expectations; unknown id → `TaskResultDoesNotExist`.
- **No self-heal on reads:** repeated `Task.objects` / admin changelist loads issue NO
  `CREATE`/ `DROP VIEW` (view OID stable); the only DDL comes from a queue-creation op
  or `absurd_sync_queues`.
- **makemigrations clean** with models exposed in `models.py` (autodetector reports no
  `django_absurd` changes; models absent from the global registry).
- **Rename doesn't break the admin:** changelist/detail render under
  `admin:django_absurd_task_…`; existing admin tests repointed.

## De-risk — spikes

**Done (✓):**

- **Tooling:** a private-`Apps` view model exposed in `models.py` →
  `manage.py makemigrations --check` (real CLI) clean, **mypy/django-stubs** clean,
  `dumpdata` ignores it (not in global registry), and `call_command("check")` model
  checks clean. So models-in-`models.py` is migration/typing-clean. ✓
- **Perf (modest at moderate scale):** 20 queues × 500 rows — cross-queue
  `filter(state='failed')` = Append over 20 seq-scans, ~1.1 ms; `filter(queue='q5', …)`
  prunes to one arm, 0.05 ms; `order_by(enqueue_at)[:20]` = Append-all + top-N heapsort,
  2.5 ms. Confirms: `queue=` prunes; `state`/sort scan all arms (no index); cost ∝ total
  rows. ✓
- **Circular import (confirmed real):** `admin_views.py` imports `QueueReadOnlyError`
  from `models.py`, so `models.py` importing the factory from `admin_views` cycles. Fix
  is locked below.

**Still to spike (before/early in planning):**

1. **Large-arm perf:** 1M+ rows in one queue — `order_by(-enqueue_at)[:N]` with no
   `enqueue_at` index; quantify the real degradation curve and finalize the docs caveat
   / "filter by queue first" guidance.
2. **`drop_queue` tolerance (Open decision D2):** a `to_regclass`-guarded union arm —
   does Postgres still prune on `queue=`, and does a dropped-queue arm degrade to zero
   rows instead of erroring the whole view? Determines D2's resolution.
3. **`CREATE OR REPLACE VIEW` for same-shape rebuilds:** confirm it avoids the
   `DROP`-then-`CREATE` `AccessExclusiveLock` gap when only the queue set (not columns)
   changes.

## Docs / maintenance ripple

`sync-docs` trigger (new public API; view rename; create-hook behavior). Update
`AGENTS.md` (an "ORM access / querying queue state" section + the provisioning note +
`drop_queue` re-sync note), README, and note the admin spec's `admin_<entity>` view
names are superseded by `<entity>_view`. No new OPTIONS key (the `AUTO_MANAGE_VIEWS`
flag was dropped).

## Adversarial review #2 — locked fixes + open decisions

**Locked fixes (fold into the plan):**

- **Circular import:** create **`django_absurd/exceptions.py`** and move
  `QueueReadOnlyError` (plus D2's typed view-not-provisioned error, and any read-only
  error the factory raises) there. Both `models.py` and `admin_views.py` import the
  error from `exceptions` → `admin_views` no longer imports `models`, so the cycle is
  broken and the factory can STAY in `admin_views.py`; `models.py` imports it from there
  (now cycle-free). Prove `python -c "import django_absurd.models"` is clean.
- **Migration:** create the empty views via a **separate `RunSQL` migration operation**,
  NOT by editing the pinned `0001_initial_0_4_0.sql` (CLAUDE.md: "Absurd SQL comes only
  from the pinned wheel"). Reverse is handled by the existing schema-CASCADE drop —
  don't add redundant reverse `DROP VIEW`s.
- **Sync builds views once:** `absurd_sync_queues` accumulates the whole declared set,
  then rebuilds views **once at the end** — never once per `create_queue` in the loop
  (avoids O(N²) churn + repeated `AccessExclusiveLock` on a deploy that adds many
  queues).
- **`get(task_id=…)` doc:** tell users to filter `.get(task_id=…, queue=…)` (both) so
  they never rely on global `task_id` uniqueness (the unmanaged model has no unique
  constraint → `MultipleObjectsReturned` is theoretically possible). Keep the synthetic
  `admin_pk` (Django needs a single-col pk; the admin's `get_object` parses `queue:`
  from it).

**OPEN DECISIONS (resolve in review):**

- **D1 — enqueue create-branch + view DDL (reverses an earlier call).** You chose
  "rebuild on create at every seam incl. enqueue." Review M1: `build_absurd_client`
  wraps **Django's own connection**, so on the enqueue create-branch the `CREATE VIEW`s
  run **inside the caller's `transaction.atomic()`** — holding locks until the caller
  commits, and a rebuild failure poisons an enqueue that already created the queue +
  spawned the task; a rollback reverts the views but not the queue (drift). Options:
  **(a)** drop the enqueue view-hook (enqueue-created queues become visible at the next
  worker-start / `absurd_sync_queues` — simplest, safest); **(b)** keep it but run
  `rebuild_views` on a **separate autocommit connection** (like the worker's) and
  swallow+log failures so they never reach the enqueue result. Recommend **(a)**.
- **D2 — `drop_queue` breaking a public read API.** A routine `drop_queue` CASCADE-drops
  the view → `Task.objects` (and, now, `get_result`) raise raw `UndefinedTable` for
  everyone until a human runs sync. Options: **(a)** **tolerant views** — guard each arm
  with `to_regclass('absurd.t_<q>')` so a dropped queue degrades to zero rows, not an
  error (spike pruning — see spikes); **(b)** wrap reads to raise a typed django-absurd
  error with a "run `absurd_sync_queues`" message (no raw psycopg leak); **(c)** degrade
  reads to empty + log. Higher stakes now that `get_result` rides the same views.
  Recommend **(a)** if the pruning spike passes, else **(b)**.

## Out of scope

- Materialized views / caching (plain live views only).
- Write access / mutation through the ORM.
- The `get_result` _API_ / result-retrieval semantics (unchanged — only its
  _implementation_ moves to the ORM; see Internal consumers).
- Multi-DB / multiple Absurd databases (single `DATABASE`).
- **Per-queue (non-union) models with a natural `task_id` pk** — deferred; addable later
  as a thin extra if natural-pk single-queue access proves needed.
- A `QuerySet.union()`-based cross-queue helper (the view supersedes it).
- A `drop_queue` _hook_ (no SDK seam exists) — handled instead by D2's tolerant-views /
  typed-error, not by hooking the drop.
