# Why django-absurd is the way it is

> **Why, not how.** For how it works today, read the code, `AGENTS.md`, and `README.md`.
> This file records intent and the load-bearing reasons behind the project's shape —
> nothing that goes stale when code moves.

## Intent

A thin, idiomatic Django integration for Absurd (a Postgres-native workflow engine).
Lean on Django's own primitives — settings, migrations, management commands, system
checks, and the Tasks framework — instead of inventing a parallel task API. Thinness is
the north star: resist features that duplicate what Django already provides.

## Database & connection

Absurd reuses Django's own database connection rather than opening its own, so all
configuration stays in Django's database settings and there is a single connection to
reason about. The hard consequence: the psycopg (v3) backend is mandatory — the SDK
rides Django's connection — so a psycopg2 setup cannot work, and the mismatch is
asserted early rather than failing cryptically later. Only the engine's own bookkeeping
needs Postgres/psycopg3; task bodies may use any other configured database.

## Schema & migrations

Absurd's schema ships as ordinary Django migrations, generated offline from a pinned
Absurd version — no network at migrate time, and the schema travels with the package
version. The schema version is deliberately coupled to the SDK version floor so the two
cannot drift apart.

Maintainer process: migrations are not hand-written. When bumping the pinned Absurd
version, regenerate the SQL with `absurdctl` as the delta between the currently pinned
schema and the new target version.

The schema lives in a fixed, non-relocatable namespace, and applying it needs a role
allowed to create that namespace and the UUID extension it depends on — so locked-down
deployments must grant those rights or pre-create the namespace.

## Tasks, enqueue & the worker

Enqueuing runs on Django's connection inside the caller's transaction, so a task spawned
inside a transaction that later rolls back is discarded with it — enqueue-on-commit, for
free, with no separate outbox.

The worker, by contrast, takes its own dedicated connection: at concurrency greater than
one a shared connection would interleave Absurd's bookkeeping and corrupt it. Delivery
is at-least-once by design — there is no atomicity between a handler's own writes and
Absurd marking the run complete — so handlers must tolerate re-execution (idempotency
keys exist for this).

## Recurring scheduling

Recurring tasks are declared in settings and driven by an in-process beat that wakes on
cadence and enqueues through the normal path. The beat is the default because it needs
nothing beyond Postgres; the database-side alternative (`pg_cron`) requires an extension
and privileges that aren't available everywhere, so it is a deliberate opt-in rather
than the default. Cron is evaluated in Django's configured timezone, not UTC, so an
operator who writes "2am" gets local 2am.

The beat only ever fires forward: a slot missed while it was down is skipped, never
backfilled. This matches the database-side scheduler (so the two stay consistent) and
avoids a thundering re-fire when a stopped beat comes back.

Each firing carries a per-slot idempotency key derived from the schedule name, the cron
expression, and the slot instant. The first design skipped this — a single beat firing
forward is already at-most-once. It was added because the key is cheap insurance that
keeps at-most-once true when a beat restart straddles a slot or a second beat is started
by accident, following Absurd's documented cron dedup pattern. The key is anchored on
the schedule name (not the task or args) so entries that differ only by arguments or
queue don't collide.

The beat is synchronous even though the worker is async. The worker must be async — it
runs async task handlers and drives the async SDK — but the beat only sleeps and
enqueues, so keeping it synchronous removed the extra concurrency machinery a shared
async loop would have demanded. Co-located with a worker, it runs on its own thread.

Database-side scheduling (`pg_cron`) is the opt-in alternative. An admin/model-managed
schedule store was considered and deliberately not pursued for the initial release:
settings is the only declaration source for both the beat and `pg_cron`. The model
reserves a `source` column so an admin lane can slot in later without rework.

### `pg_cron`: projection table + constant command

The `pg_cron` command is a constant call —
`select public.django_absurd_run_scheduled('settings', '<alias>', '<name>')` — not a
dynamically assembled SQL string. Task data (args, kwargs, options) lives in a
`ScheduledTask` projection table row; the wrapper function reads it at fire time and
calls `absurd.spawn_task`. This removes the SQL-string-from-data injection surface
entirely: there is no `format('%L')` over task arguments or kwargs, no escaped-string
gymnastics, no injection path — the cron command is a literal of the schedule name only,
and that name is charset-restricted by a static check. (`format('%L')` is used, but only
over source/alias/name — fixed-charset identifiers, never free-form task data.)

The wrapper is defined `SET search_path = pg_catalog` and fully schema-qualifies every
object it touches. `pg_cron` fires each job as the stored role with that role's default
search path, not the reconcile session's — an unqualified reference would fail silently
into `cron.job_run_details`. The search-path-safe definition is load-bearing.

### Settings as source of truth; admin lane reserved

Settings is the single declaration source for both schedulers. The schedule admin is
**read-only and `pg_cron`-only**: it surfaces the projection-table rows, and only
`pg_cron` keeps a row per schedule — the beat declares nothing in the database, so there
is nothing for an admin to show. It is read-only because settings, not the admin, is the
source of truth; a writable lane is a separate, deliberately deferred step.

A future writable lane would author `source="admin"` rows, and the shape already
anticipates it: `sync_crons` is scoped to `source="settings"` and never touches admin
rows, the `absurd:settings:<alias>:<name>` / `absurd:admin:<alias>:<name>` job-name
split gives `pg_cron`'s prune the same scoping, and the fire wrapper takes `source` as a
parameter — so admin-authored schedules fire through the same path with no fire-path
change. It was deferred, not free: authoring is validation-heavy (the schedule rules
that run at `check` time against settings must also run at row-save time), and a saved
row must (un)schedule its `pg_cron` job immediately rather than at the next migrate/sync
— runtime job emission the settings lane never needed.

### Static checks, validate at sync

`manage.py check` stays DB-free. Grammar, privilege, and extension facts are validated
by the real `cron.schedule` at sync time — loud in the command, skip-with-log at
migrate. A DB-probe variant of `absurd.E008` (verifying the extension is present at
check time) was considered and dropped: a connectivity error at `check` time is not a
scheduling problem and should not block deployments. The shipped `absurd.E008` is a
static configuration check — it fires when `SCHEDULER="pg_cron"` but
`django_absurd.pg_cron` is absent from `INSTALLED_APPS`, which is knowable without any
DB connection.

### pg_cron cron grammar is DB-authoritative (croniter is beat-only)

The two schedulers have different cron grammars, each with its own authority. Beat uses
croniter (which supports a 6-field leading-seconds form). `pg_cron` has its own grammar
— a 5-field cron OR the interval form `"[1-59] seconds"` — and `pg_cron` itself is the
authority. croniter can't parse `"30 seconds"` and would false-reject it, so croniter is
scoped to beat only: the DB-free `manage.py check` no longer croniter-validates
`pg_cron` crons. Their grammar is verified by the real `cron.schedule` at sync (and, for
admin-authored rows, a save-time savepoint trial). Consequently `pg_cron` sub-minute
(down to `1 seconds`) is allowed — an admin authoring one accepts the high
`cron.job_run_details` growth. (An earlier design rejected a _croniter 6-field_ "N
seconds" shim; that is different — the shim faked seconds on top of croniter's grammar,
whereas this is `pg_cron`'s own native interval syntax.)

### Extension in the app migration (fail-fast)

The `django_absurd.pg_cron` app migration runs `CREATE EXTENSION IF NOT EXISTS pg_cron`
as its first operation. Empirically: when the extension is already present (managed
Postgres, or pre-created as superuser), the statement is a no-op — no superuser needed.
When it is absent and the migrate role is not a superuser, it fails loudly with
`permission denied / must be superuser`. That fail-fast is exactly right for an opt-in
app: adding it to `INSTALLED_APPS` on a DB that isn't pg_cron-ready breaks visibly at
`migrate` time, not silently at reconcile time.

`shared_preload_libraries = pg_cron` (a server restart GUC) and `cron.database_name` are
still operator-side prerequisites that a migration can't deliver. Those stay documented
as manual setup steps.

## Admin & ORM introspection

Queue state is exposed read-only, in two forms — a Django admin and plain ORM models —
because Absurd owns every write to its tables; an editable view would invite writes that
corrupt its bookkeeping. So the models refuse `save`/`delete` and the admin grants no
add/change/delete. Each entity (tasks, runs, checkpoints, events, waits) spans every
queue through a single `UNION ALL` view carrying a synthesized queue column, so one
changelist or queryset covers all queues instead of one per queue; the cost is no
cross-queue index, so filtering by queue is the fast path. The views are (re)built at
migrate / worker-start / sync, so a queue reached only by a bare enqueue before the next
sync is briefly absent from them — the admin surfaces that gap rather than pretending
the list is complete.

## Routing & multiple databases

The router claims only this app's models; it never dictates routing for the rest of a
host project, and it is a no-op when the default database is used. Spreading the engine
across more than one database is intentionally unsupported for now — the added surface
and the cross-database atomicity questions aren't worth it yet.

## Deliberately not doing (yet)

Native async enqueue, one-shot deferred (scheduled-for-later) enqueue, and task priority
are unsupported on purpose: Absurd has no notion of priority, and async/one-shot
deferral aren't wired — we won't fake them behind a flag that implies otherwise.
(Recurring scheduling is supported — see above.)
