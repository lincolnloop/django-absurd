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

### One worker mode, and its own claim loop

There is exactly one way to run a worker: a resident process that polls until it is
signalled. A second "drain the backlog and exit" mode existed for a while and was
removed. It had been built as an entry point for this project's own tests, escaped onto
the command line, and was then documented as a deployment mode it was never designed to
be. The drain concept survives where it belongs — in the test fixture, one task at a
time. Peers do ship a real drain-and-exit worker, so this forecloses a scale-to-zero
deployment story; that is a live option to revisit, on purpose, rather than an
oversight.

The worker does not use the engine client's own worker loop, and that is a deliberate,
temporary divergence with a written expiry. That loop rebuilds its in-flight set per
claimed batch and waits for the whole batch before claiming again, so one slow task
idles every other slot: an N-slot worker delivered roughly one slot's throughput on
mixed-duration work, wasting about three quarters of the capacity it was configured for.
The client's synchronous worker never had this — it keeps one in-flight set across
iterations and caps each claim by free capacity — so the fix is that shape, pushed
upstream and carried here until a released version has it. The condition for deleting
the local loop is recorded next to the code and in the upstream-asks document; nobody
should have to re-derive whether it is still needed.

Capping claims by free capacity has one consequence worth knowing before someone
"simplifies" it away: at a single slot, free capacity is one, so a configured batch size
would silently collapse from one round trip into one per task. A single-slot worker
therefore keeps claiming whole batches and running them in order.

Shutdown is two distinct things, and conflating them loses work. A stop request means
stop claiming and let in-flight runs finish; only an actual cancellation of the worker
cancels them, and it must also wait for them to unwind, because the connection every one
of those runs writes through is about to close. Abandoning them instead strands each run
marked as claimed until its lease expires, after which it silently runs again. The stop
signal is the worker's own rather than the client's flag, because that flag is read only
by the loop we no longer use.

A stop is acknowledged the moment it is heard, on both the log and the terminal, because
the wait that follows is unbounded and silence reads as a hung process — the operator's
next move is a second interrupt, and then a kill. Two things are deliberately absent.
The message names no count of remaining work: the number lives inside the loop, so
reporting it would tie this project to owning that loop permanently, which is precisely
what the upstream fix is meant to end. And there is no force-quit on a second interrupt,
because there is nothing honest to do with the runs it would abandon — the engine
exposes no way to hand a claimed run back, so they would sit unavailable until their
lease expired anyway.

### Spawn params: one factory, two sites

Absurd's per-spawn options (retry ceiling, retry strategy, cancellation policy, headers,
dedup key) attach through a params factory whose result is _bound_ to a task, rather
than riding an extra keyword on the enqueue call. The keyword form was tried and cannot
work: the task framework types its enqueue signature against the task function's own
parameters, so an outside keyword is unspellable to a type checker — and worse, it
squats the task's own namespace, so a task with a parameter of that name silently loses
its argument until a worker runs it.

Binding yields a real task object rather than a delegating wrapper, so identity checks,
the async enqueue, result retrieval, and routing all keep working through it. The params
ride as a dataclass **field**, not a slot, because the framework's own routing call is a
dataclass replace that rebuilds from fields only — a slot would be dropped silently,
with neither a static nor a runtime error.

Definition-time defaults and per-invocation values overlap but are not identical: a
dedup key or per-call headers are meaningless as a task-wide default. One factory serves
both sites, and the split is enforced by overload selection on the keyword set — then
again at runtime, because type checking is optional for our users. The runtime layer is
the contract; the static layer is a bonus.

No value validation on this surface. Wrong _data_ fails loudly on its own, and restating
the engine's types here is duplicated policy that drifts from the pinned SQL. Curated
errors are reserved for a different mistake — the caller at the wrong door: a routing
option that belongs on the framework's own routing call, or a per-invocation field used
at a definition site. Python's own message cannot point at the right API; a wrong type
already fails on its own.

One floor is load-bearing rather than cosmetic. An omitted retry ceiling is stored as
NULL, and the engine retries while it is NULL — that is, forever. So every path that
builds spawn options fills it explicitly, and persisted schedule rows keep a concrete
default for the same reason. This already caused an unbounded retry loop on a scheduled
task once.

Whether params will actually apply is only knowable at enqueue, never at bind: a task
bound while on another backend and then routed onto Absurd applies them fine, so warning
at bind time would cry wolf over a legal ordering while staying silent about the
reverse.

### Deferring one enqueue

Absurd's spawn has no "available at" — nothing expresses "exist, but don't run until
later." The first implementation claimed the caller's task immediately and slept inside
it. That set the task's start timestamp before any of its work existed, which broke both
cancellation rules at once: a maximum-duration policy measured the wait rather than the
work, and a maximum-start-delay policy measured from the wrong instant. So a deferred
enqueue instead spawns a separate wrapper run that sleeps and then enqueues the real
task — nothing of the caller's is claimed until its work is about to happen.

The wrapper is deliberately **not** a task the library declares. Declaring one resolves
the default backend and validates its queue at _decoration_ time, so any project whose
declared queues omit the default name would fail on import — surfacing at dispatch,
where it takes the worker down. The wrapper is a synthetic name the worker's own
resolver recognises instead, formed by suffixing the target's path: leading with the
target sorts the row beside it in the admin and names the argument that caused it, and
the separator cannot appear in a dotted path, so the name can never collide with a real
task.

Two details that look arbitrary and aren't. The wrapper carries a _derived_ dedup key
rather than the caller's verbatim — with the caller's own key it would reserve that key
against its own row and then dedupe the very enqueue it woke up to make. And the inner
enqueue is a checkpointed step, so a wrapper retry replays the stored id instead of
enqueueing a second task. While the wrapper waits, the caller's id reports "ready": the
truthful answer to "has my task started", and the framework has no status meaning "the
deferral is retrying."

The wrapper's own durable calls stay unlogged. Its sleep and its inner enqueue run
against the raw SDK context, not the wrapper this project hands user code, so no
per-primitive lines appear for them — deliberate, because the run-level suspension line
already says a deferral is waiting, and the primitive log exists to narrate what _user_
code asked for. Threading it through the logging wrapper is a one-line change if that
ever stops being true.

### Lifecycle signals: sent for the framework's logging, not offered as a hook

The framework logs a task's lifecycle by receiving its own three task signals, so a
backend that sends none logs nothing. Sending them is therefore part of the backend
contract, and not sending them was a plain defect rather than a missing feature. That —
wiring Absurd tasks into the framework's built-in logging — is the whole reason they are
sent. They are deliberately not promoted as an extension point: the stored result and
the queue-state models answer the same questions without sitting inside a live run, so
the docs point there instead.

The package registers no receiver of its own either. An unfiltered receiver fires for
every configured backend, so listening would mean reporting tasks that never touched the
engine.

The engine's own execution-wrapping hook looks like the natural seam and is not used. It
sees a task's name rather than the task object the signals must carry, it fires for the
synthetic deferral wrapper too — which must announce nothing — and the code it would
replace is already ours, so there is no private reach it would legitimise.

Two asymmetries on the enqueue announcement are known and deliberate. It is sent when
the enqueue happens, not when the surrounding transaction commits, so a rollback can
discard a task whose enqueue was already announced; deferring it to commit would put
this backend's timing out of step with every other one, which is a worse trade than the
gap it closes. And a database-side schedule announces nothing at enqueue at all: it
spawns through the database rather than the library's enqueue path, so a receiver sees a
start with no enqueue before it. The in-process scheduler goes through the normal path
and does not have this gap.

**The sends must not be able to damage a task, and are contained in one place.**
Uncontained, an exception from any receiver — including the framework's own — escapes
into the engine's error handling, which reads it as the _task_ failing, so one buggy
receiver would consume every attempt of every task until the queue was uniformly failed.
Each seam has a worse variant: a raiser on the success send re-executes an
already-succeeded body, one on the failure send persists the receiver's error as the
task's failure reason, and one on the enqueue seam leaves a deferred wrapper's
checkpoint uncommitted and duplicates the very enqueue it woke to make. One helper, so
there is one rule and one branch.

**Worker-side sends happen inline, on the loop.** The framework's built-in receivers
only log, and nothing that only logs needs a database — so the sends stay where the
handler already is. A receiver that does reach for the ORM meets the framework's
async-safety guard and is contained and logged like any other broken receiver, which is
the honest outcome once the signals are not an extension point. Two costs come with
being inline and are accepted rather than mitigated: the sends sit inside the claim
lease, because the run's completion is not persisted until the handler returns, and a
receiver runs on the same loop as the task's own durable operations.

**Only provable terminality is announced, and a start is per episode.** The framework's
four result statuses have no member for "retrying", "cancelled" or "sleeping", so a
failed non-final attempt has nowhere honest to go but "ready" — reporting a finish per
attempt would make the framework log an error and a traceback for a task that later
succeeds. Symmetrically, a suspended task re-enters its handler from the top on every
wake, and the information freely available cannot distinguish a replay of this run from
a retry of a task that had already completed a step — so a start is reported per
episode, and the docs say so rather than pretending otherwise.

**Endings the engine decides on its own are outside what these signals describe.** They
report the lifecycle the framework knows: enqueued, started, and finished on success or
on a failure the task's own code raised. The engine ends tasks on several paths of its
own — a delay or duration budget expiring before or between claims, an expired claim,
cancelling a task nothing has claimed — none of which reaches a handler or reports an
outcome back, and two more (a cancel, or a run failed elsewhere) which a handler DOES
observe, as an error out of the run's next durable write.

Reporting those last two was tried and dropped, and that is what makes the rule one rule
instead of two plus a list of exceptions. A handler sees them only when the deciding
event happens to land while a worker is mid-run; the same cancel arriving a moment
earlier is one of the unreported paths. Announcing a finish that depends on that race is
worse than not announcing one, and it cost two exported exception types to dress the
engine's internal classes up as ours for a payload field only a receiver reads.
Predicting the unreported paths instead would mean replicating the engine's retry and
duration arithmetic in Python, duplicating pinned-SQL policy, and still would not turn
an engine decision into a framework lifecycle event. The handler's arm for those three
exceptions therefore only logs and re-raises — but it must exist, because they are
ordinary exception subclasses and the generic failure arm below would otherwise report
them as the task failing. The stored result and the queue-state models are the record of
what happened.

**The worker is authoritative for a run's queue and backend, never the task object.** A
re-imported task carries its _definition's_ queue and alias, so a task defined on one
backend and enqueued onto Absurd would otherwise be reported under one backend at
enqueue and another at execution, and its result id would name the wrong queue and fail
to resolve. Both the worker and the result read therefore force queue and alias together
— separately is not an option, because the framework's routing call re-validates the
queue against whichever backend the definition named, an alias the project need not have
configured at all.

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

### Events: waiting and emitting

Alongside durable sleep, a task can durably wait for a named event and resume when it
arrives. A wait with no timeout is a genuine "wait forever" state, not an oversight — it
is stored as Postgres's infinity sentinel rather than a magic far-future timestamp, so
nothing has to guess whether a distant date means "really far off" or "never." Emitting
an event is a top-level operation callable from outside any task, because the producer
is usually a web request or other ordinary code, not another task — so the emitter is a
plain function riding Django's connection, not a method reachable only from inside a
running handler. Letting one task block on another task's _result_ was considered and
cut: it invites synchronous cross-task coupling that the durable-step model already
expresses more safely.

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

The two lanes never clobber each other: `sync_crons` is scoped to the settings source
and never touches admin rows, and a job-name split namespaced by both the app database
and the source (settings vs. admin) gives `pg_cron`'s prune that same scoping — and lets
one central catalog safely hold jobs for many different app databases at once. That
database namespace is a safety requirement, not tidiness: `pg_cron`'s job catalog is
unique on the job name and role alone, not scoped by database, so scheduling a job whose
name already exists silently retargets that job to the newly-named database. Without the
database baked into every name, a job scheduled from a test database (or a second app
database) would hijack an identically-named production job and point it at the wrong
data; namespacing makes each database's jobs disjoint so that retarget can't happen. The
fire wrapper takes `source` as a parameter, so both lanes fire through one path. Admin
authoring is validation-heavy by nature — the schedule rules that run at `check` time
against settings also run at row-save time — and a saved row must (un)schedule its
`pg_cron` job immediately rather than at the next migrate/sync, so emission is wired to
save/delete signals (runtime job emission the settings lane never needed).

### Static checks, validate at sync

A plain `manage.py check` stays DB-free: schedule-content problems (bad task path, bad
cron expression, undeclared queue) are validated statically, and grammar/privilege facts
are validated by the real `cron.schedule_in_database` at sync time — loud in the
command, skip-with-log at migrate. Whether the central extension itself is present is a
different kind of fact — deployment topology, not schedule content — so its check is
scoped to run only alongside `migrate` and an explicit `check --database`, never a plain
DB-free `check`: a connectivity error there is a deploy-readiness signal, not something
that should block routine, DB-free invocations of `check`.

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
`pg_cron` crons. Their grammar is verified by the real `cron.schedule_in_database` at
sync (and, for admin-authored rows, a save-time savepoint trial). Consequently `pg_cron`
sub-minute (down to `1 seconds`) is allowed — an admin authoring one accepts the high
`cron.job_run_details` growth. (An earlier design rejected a _croniter 6-field_ "N
seconds" shim; that is different — the shim faked seconds on top of croniter's grammar,
whereas this is `pg_cron`'s own native interval syntax.)

### The extension never lives on the app database (cross-database scheduling)

`cron.database_name` is a single cluster-wide setting, and the extension it names can
only be created in that one database. An earlier design created the extension inside the
app's own migration — but if the extension lived on the app database, every test
database (and every parallel test worker's own copy) would have to _be_ that one special
database, which is fundamentally incompatible with standard test-database isolation and
running tests in parallel. So the extension is instead operator-managed once, on a
central metadata database, and every schedule is created cross-database, naming the app
database as the target explicitly rather than assuming "here." Fail-fast for a
misconfigured or missing extension moved from migration time to a deploy-time check
instead, since a migration can no longer be the thing that creates it.

A plain test database therefore never has the schema the extension provides. Because of
that, any attempt to touch it there would error immediately ("no such schema") — so the
scheduling code deliberately treats itself as a no-op whenever it detects a test
database or an active test run, unless a test explicitly opts in. This means the common
case — a test that has nothing to do with scheduling — never touches the extension and
never errors; scheduling code silently does nothing. Only a test that specifically wants
to exercise real scheduling flips that switch, and only for the backend it names.

Reaching the central database can't go through Django's own multi-database
configuration, because Django's test runner manages every database it knows about —
creating a fresh, empty, extension-less copy of it per test run and per parallel worker.
Routing through that machinery would recreate the exact problem this design avoids. So
the connection to the central database is opened directly, borrowed from the same
connection parameters as the app database (same server, same credentials) with only the
target database name swapped — a connection Django's test infrastructure never sees or
manages. That path is identical in production and under test, which is what makes the
test-side opt-out safe: nothing about how the central database is reached changes
between the two.

Finally, disabling a previously-scheduled job needs its own dedicated call, not just a
repeat of the schedule call with different arguments. Scheduling is an upsert, but the
enabled/disabled flag it accepts only takes effect the first time a job is created — an
update to an existing job's schedule doesn't change whether it's enabled. So turning a
job off requires a second, explicit step, and a deployment where the scheduling role
doesn't own the central extension outright needs a grant for that second step
specifically (a deployment where it does own the extension needs no extra grant at all).

`shared_preload_libraries = pg_cron` (a server restart GUC), installing the extension,
and the grants above remain operator-side prerequisites that nothing in django-absurd
can deliver for you. Those stay documented as manual setup steps.

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
library drives cleanup in-process under beat, and under pg_cron schedules a job calling
Absurd's own native cleanup function — the same declarative config works for both
schedulers.

That job rides the ordinary schedule seam, on its own source lane namespaced by the app
database, rather than reusing the identity Absurd's built-in maintenance scheduler would
have used. Two reasons, and only the second is about tidiness. Absurd's own maintenance
scheduling needs `cron.schedule` in the Absurd database itself, which the cross-database
topology above never provides — so that path is unavailable here, not merely declined.
And riding the normal lane means the same per-database scoping, teardown, and flush that
every other schedule gets, instead of a second bespoke reconcile path.

The cost is that the two mechanisms are now invisible to each other: a maintenance job
created by Absurd's own tooling is outside the namespace this package manages, so
nothing here can see or remove it and both would fire. Hence cleanup must be driven one
way only (multi-manager arbitration deferred) — a warning, not an implementation detail.

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

## Testing: Django-parity cleanup on Absurd's own tables

Django's test isolation reverts each test by rolling back a transaction, but Absurd's
state lives where that rollback can't reach it: per-queue tables the worker writes on
its own connection, and scheduled jobs sitting in the database catalog. Left alone, that
state leaks from one test into the next. The library restores Django-level isolation on
exactly those non-Django tables — automatically, no per-test opt-in — so a project
testing against Absurd gets the same clean-slate-per-test it already expects from
Django.

The cleanup hangs off Django's own post-test teardown hook, not a pytest fixture. A
fixture was the first shape and cannot work: an autouse fixture's teardown runs _after_
the test framework has already re-blocked the database, so the flush hits a blocked
connection and hard-errors. The post-teardown hook instead fires while the database is
still unblocked, and only for the database-backed cases that actually dirtied it — the
correct seam. It also mirrors Django's own guarantee: a transactional test case already
gets a full rollback, so the flush runs only for the commit-based cases Django itself
would otherwise leave dirty.

What ships is a deliberate split. Parity isolation on Absurd's tables is a guarantee
every consumer wants, so it is automatic and unconditional. Aggressively clearing queues
before _and_ after a test is specific to testing the library's own internals — not
something a normal project needs — so it stays an internal helper, never forced on
users.

The pytest integration loads as a plugin imported before Django is configured, on every
pytest run in any environment where the package is installed — Django project or not.
That forces strict import-safety: the plugin touches nothing Django at import time and
defers every Django-dependent import to call time. The non-pytest (`manage.py test`)
path is not auto-wired yet; unittest users opt in by calling the same install hook from
a test-runner subclass.

### Testing durable time: both clocks or neither

A suspended run is gated twice — by Postgres, whose claim predicate compares a deadline
against the engine's own now-function, and by the Python SDK, which recomputes the wake
time on replay and re-suspends if it hasn't arrived. Moving one without the other does
not fail benignly. Move only the database clock and the run becomes claimable, the SDK
re-suspends, and the loop re-arms the same wake time forever; with a synchronous task
the drain then deadlocks inside a thread join — unkillable except by SIGKILL, with no
test output at all. The kill strands the database-level override, so the _next_ run's
first draining test hangs the same way before any per-test cleanup gets to run.

Three consequences shape the design. There is no database-only knob. Advancing is one
operation over both clocks. And recovery needs a sweep at session start, not only
per-test teardown, because a randomised test order can schedule the poisoned test before
any resetting code.

An asymmetry fixes the apply order: Python ahead of Postgres is harmless (a run is
merely not claimable yet), Postgres ahead of Python is the deadlock. So the Python clock
moves first, and a failure between the two halves lands on the benign side. The same
reasoning bans a naive or string instant — the engine's now-function casts the stored
text using the _reading_ session's timezone, and the worker connects on its own
connection, whose timezone is the server default rather than Django's. The same naive
text is therefore a different instant per session, and on a server west of UTC it puts
the database ahead of Python. Only an aware datetime is accepted, and it is always
written with its offset.

freezegun cannot be the Python half, structurally: it patches the monotonic clock, and
that clock _is_ asyncio's event-loop clock, so a frozen freezegun deadlocks the drain.
It works only while ticking — and ticking violates the requirement above, since the
database override is a static literal that never ticks, so Python must not either. One
clock library in the repo, and it is the one that leaves the monotonic clock alone.

Reads go on a fresh connection pinned to UTC rather than Django's. Django's session
never sees a database-level default applied after it opened, a savepoint rollback
reverts a session-level one, and reporting what we _intended_ to apply could never
reveal a Python/Postgres desync — the exact failure this design most needs to keep
visible.

Two record shapes, because one of them would lie. What a run did and where a task stands
are separate views. A task-level view cannot express an in-flight retry: its attempt
counter counts attempts _created_ rather than completed, its "sleeping" state covers
both a durable sleep and a retry backoff, and mid-backoff the failure is invisible
because the newest run is already pending. A test asserting "my workflow is asleep"
would pass on a task that had crashed. Per-run records read attempt-by-attempt; the
task-level one is for terminal state. The framework's own result type can serve neither,
because its status vocabulary folds "sleeping" into running and "cancelled" into failed
— precisely the distinctions a durable test exists to make.

Freezing is lexically scoped rather than a pair of imperative calls, so the release
point doesn't depend on fixture teardown, and nesting raises instead of stacking: two
frozen instants cannot both be "now".

Two things a freeze deliberately does not reach. The database-side scheduler's launcher
runs in another database on its own clock, so advancing durable time cannot make a
schedule fire — testing that stays "reconcile, then inspect the job catalog". And a
subprocess worker is only half-frozen: the database override reaches it, its Python
clock stays real, which is the deadlock direction — so it is out of scope rather than
half-supported.

## Our own exception types

The package raises its own hierarchy for its own failure modes, under one base. Three
reasons, in the order they mattered. First, **the exception owns its message**: the
original shape had a message-builder function imported wherever a raise site needed
text, including one lazy import that existed purely to reach it across a module cycle.
Encapsulating the message in the class removed the helper and the lazy import together,
because the raise site already holds the data. Second, a specific type names a specific
condition, where a generic one only says someone passed something bad — and catching the
base then means "this package rejected something". Third, the base is named for the
distributing package rather than the upstream engine, because modules import from both
and the SDK's own exceptions share no base that would anchor the shorter name.

Honest limit: catching the base is not universal. Several configuration, connection, and
test-guard paths still raise plain framework or stdlib errors, so the docs must not
promise otherwise.

## Deliberately not doing (yet)

Task **priority** is unsupported because Absurd has no notion of it — we won't fake it
behind a flag that implies otherwise.

A **natively** async enqueue is a deliberate non-goal, and that is narrower than it
sounds: async task bodies run on the worker, and the async enqueue call exists and works
— it wraps the synchronous path rather than issuing its own async round-trip. Every
public surface gets an async twin by wrapping one implementation, not by growing a
second one. (Async task bodies, one-shot deferred enqueue, result retrieval, and
recurring scheduling are all supported — see above.)

A surface to enable Absurd's native `enable_cron` partition + detach maintenance jobs is
not built yet. That native scheduling is pg_cron-only, so until a project-facing surface
exists those partition/detach jobs are simply never created. (Retention itself is
already covered natively — the declarative `CLEANUP` job schedules a native database job
under pg_cron, see above; only the partition/detach half of `enable_cron` remains
unsurfaced.)
