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

## Durable execution: steps & sleep

Absurd's durable primitives — checkpointed steps and durable sleep (a task suspends and
resumes later, replaying from its last checkpoint) — are reached through an
**accessor**, not by enriching the context object the task framework hands the task. The
tempting shape (subclass the framework's task-context and add the durable methods) was
tried and rejected: the framework's typed contract promises the handler a _base_
context, and substituting a narrower subtype is unsound — it forces a permanent
type-suppression on every typed task and quietly steps outside the framework's contract.
Instead the durable context is fetched on demand from the SDK's own current-context
handle, leaving the framework's context exactly as the framework defines it. Nothing to
reconcile, nothing to suppress.

There are two accessors — one sync, one async — and that split is a direct consequence
of running **one async worker that also serves synchronous tasks**. A sync task runs off
the event loop (in a thread), so it can't await the async engine context; its durable
calls bridge back to the loop. Native, bridge-free durable calls for both task kinds
would require two workers (one sync, one async) with the tasks routed between them —
deliberate operational cost we declined. One async worker plus a contained bridge keeps
deployment to a single process; the bridge is an internal detail users never see.

Steps are **effectively-once, not exactly-once**: a step's checkpoint is persisted
_after_ its function returns, on a connection that can never be atomic with the task's
own writes, so a crash between the side effect and the checkpoint re-runs that step.
This is the load-bearing thing a task author must know — side effects inside steps still
need to tolerate re-execution. Teaching an absolute "runs once" guarantee would breed
exactly the wrong code.

## Recurring scheduling

Recurring tasks are declared in settings and driven by an in-process beat that wakes on
cadence and enqueues through the normal path. The beat is the default because it needs
nothing beyond Postgres; the database-side alternative (`pg_cron`) requires an extension
and privileges that aren't available everywhere, so it is a deliberate opt-in rather
than the default — expressed by installing `django_absurd.pg_cron`, not a separate
settings flag (see the static-checks section below for why). Cron is evaluated in
Django's configured timezone, not UTC, so an operator who writes "2am" gets local 2am.

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
`select public.django_absurd_run_scheduled('<source>', '<name>')` — not a dynamically
assembled SQL string. Task data (args, kwargs, options) lives in a `ScheduledTask`
projection table row; the wrapper function reads it at fire time and calls
`absurd.spawn_task`. This removes the SQL-string-from-data injection surface entirely:
there is no `format('%L')` over task arguments or kwargs, no escaped-string gymnastics,
no injection path — the cron command is a literal of the schedule name only, and that
name is charset-restricted by a static check. (`format('%L')` is used, but only over
source and name — fixed-charset identifiers, never free-form task data.)

The wrapper is defined `SET search_path = pg_catalog` and fully schema-qualifies every
object it touches. `pg_cron` fires each job as the stored role with that role's default
search path, not the reconcile session's — an unqualified reference would fail silently
into `cron.job_run_details`. The search-path-safe definition is load-bearing.

### Settings as source of truth; admin lane reserved

Settings declares the settings lane for both schedulers; `pg_cron` additionally supports
an admin-authored lane. The schedule admin is **`pg_cron`-only**: only `pg_cron` keeps a
row per schedule, so there is a projection-table row to surface and edit — the beat
declares nothing in the database, so there is nothing for an admin to show.
Settings-lane rows stay read-only in the admin (settings, not the admin, is their source
of truth); admin-authored `source="admin"` rows are writable.

The two lanes never clobber each other: `sync_crons` is scoped to `source="settings"`
and never touches admin rows, and the `_dj:settings:<name>` / `_dj:admin:<name>`
job-name split gives `pg_cron`'s prune the same scoping. The fire wrapper takes `source`
as a parameter, so both lanes fire through one path. Admin authoring is validation-heavy
by nature — the schedule rules that run at `check` time against settings also run at
row-save time — and a saved row must (un)schedule its `pg_cron` job immediately rather
than at the next migrate/sync, so emission is wired to save/delete signals (runtime job
emission the settings lane never needed).

### Static checks, validate at sync

`manage.py check` stays DB-free. Grammar, privilege, and extension facts are validated
by the real `cron.schedule` at sync time — loud in the command, skip-with-log at
migrate. A DB-probe variant of a scheduler-misconfiguration check (verifying the
extension is present at check time) was considered and dropped: a connectivity error at
`check` time is not a scheduling problem and should not block deployments.

Scheduler selection itself is derived, not a separate setting: `AbsurdBackend.scheduler`
reads `INSTALLED_APPS` (`django_absurd.pg_cron` present → `"pg_cron"`, absent →
`"beat"`) rather than a user-set `OPTIONS["SCHEDULER"]` key. The original design had a
static `absurd.E008` check catching `SCHEDULER="pg_cron"` set without the app installed;
deriving scheduler from app presence makes that misconfiguration unrepresentable, so
both the option and the check it needed are gone.

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

### Sync-on-migrate is gated against test databases, opt-in per side

An automatic, migrate-time sync of `pg_cron` schedules is dangerous specifically on a
**test** database: `pg_cron`'s launcher is a Postgres background worker, entirely
outside pytest/Django's control, so a schedule synced into a test database's catalog
keeps firing for real, on cadence, against test data, for the rest of that process —
independent of whether the test session itself has ended. This is invisible until it
happens, because most projects share `TASKS`/`OPTIONS["SCHEDULE"]` between real and test
settings.

The fix is two independent defaults, not one flag: sync-on-migrate stays on for a real
database (unchanged behavior, no break on upgrade) and defaults off for a database
Django's test framework has swapped in (safe out of the box, no settings changes
required for any existing consumer). A single before/after boolean would have forced a
choice between "break existing real-DB behavior" and "leave the hazard live by default"
— the two cases need opposite defaults, so they need two keys.

The guard belongs in the `post_migrate` receiver specifically, not in the shared sync
functions it calls: the explicit reconcile command is a separate, deliberate invocation,
and a user who types it wants it to sync regardless of which database they're pointed
at. Folding the guard into the shared functions would silently neuter that explicit
command too — an automatic side effect and a deliberate command must not share a gate.

Detecting "is this a test database" can't compare the live connection's database name
against the name in settings at signal-fire time — Django's test-database swap mutates
the settings object itself, so the "before" and "live" views are the same object and can
never differ. The name has to be snapshotted once, before any swap can happen, and
compared against the live value later. That snapshot can't assume it only ever runs once
per process either: Django re-runs every app's startup hook whenever `INSTALLED_APPS` is
reassigned (a real pattern in test suites that exercise install-order checks), including
well after a test database has already been swapped in — so a later re-run must never
overwrite a name already captured, only fill in one it hasn't seen yet.

## Cleanup & retention

Retention — deleting aged task history — is enforced by a plain function
(`cleanup_queues()`) plus an on-demand command (`absurd_cleanup`), and wired to a
cadence via `OPTIONS["CLEANUP"] = {"schedule": "<cron>"}`. No user code required: the
library drives cleanup in-process under beat, and schedules Absurd's own native cleanup
job (`absurd_cleanup_all`, the same identity `absurdctl cron` uses — not a parallel job)
under pg_cron — the same declarative config works for both schedulers. When
`OPTIONS["CLEANUP"]` is set, django-absurd is authoritative over that job (schedules and
unschedules it), so cleanup is driven one way only — via `OPTIONS["CLEANUP"]` or
`absurdctl cron`, not both (multi-manager arbitration deferred).

The first iteration shipped the retention logic and asked each project to wrap it in its
own `@task` and register that task in `SCHEDULE`. That was a reasonable first step, but
it imposed user code for a universal maintenance concern, required a concrete queue
binding that the task framework validates at import time (breaking projects whose queue
names differed from the assumed default), and the pg_cron path gave users the same
application-level wrapper rather than a native job. The declarative `CLEANUP` key
replaces that: zero user code, works uniformly under beat or pg_cron, and preserves the
no-shipped-`@task` property that originally motivated keeping cleanup out of the
library.

The function returns plain per-queue count dictionaries rather than a richer typed
object, because that return value becomes a task result and task results are stored as
JSON — a dataclass or named tuple would serialise as an unlabelled array or not at all,
losing the field names that make the stored result worth keeping.

Cleanup deliberately does not turn a missing schema into a friendly "run migrate" error
the way the configuration paths do: it is a maintenance operation, so the raw database
error is allowed to surface rather than adding a guard that implies the call was safe.

Wiping everything is a separate, guarded command that drops queues and their data while
leaving the schema, functions, and migration history intact — because a full schema
teardown is already what running migrations backwards achieves, and the migration system
should stay the sole owner of the schema. It follows the framework's own
destructive-command convention (confirm interactively, skip with a no-input flag) so the
safety model is one users already know. Dropping a queue removes only that queue's own
maintenance jobs and data; user-defined recurring schedules live elsewhere and survive,
so a reset never silently cancels recurring work.

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

Exactly one Absurd backend per project is a deliberate design boundary, not a temporary
limit: two backends pointing Absurd at the same database is nonsense (one schema, one
queue set, split config = confusion), and distinct databases are the same deferred
multi-DB boundary above. The backend is resolved by capability (is-an-`AbsurdBackend`),
never by the `"default"` name, so it may live at any `TASKS` alias; more than one is a
loud-but-liftable configuration error. The codebase already assumes a single Absurd
system throughout (one cleanup authority, one `pg_cron` database, per-database
migrations, cross-queue `UNION ALL` views) — enforcing one backend makes that assumption
real rather than leaving a silent "pick the first / pick `default`" guess. Non-Absurd
task backends coexist freely; the limit is on Absurd backends only.

## Deliberately not doing (yet)

Native async enqueue, one-shot deferred (scheduled-for-later) enqueue, and task priority
are unsupported on purpose: Absurd has no notion of priority, and async/one-shot
deferral aren't wired — we won't fake them behind a flag that implies otherwise.
(Recurring scheduling is supported — see above.)

A surface to enable Absurd's native `enable_cron` partition + detach maintenance jobs is
not built yet. That native scheduling is pg_cron-only, so until a project-facing surface
exists those partition/detach jobs are simply never created. (Retention itself is
already covered natively — the declarative `CLEANUP` job schedules a native database job
under pg_cron, see above; only the partition/detach half of `enable_cron` remains
unsurfaced.)
