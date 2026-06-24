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

## Routing & multiple databases

The router claims only this app's models; it never dictates routing for the rest of a
host project, and it is a no-op when the default database is used. Spreading the engine
across more than one database is intentionally unsupported for now — the added surface
and the cross-database atomicity questions aren't worth it yet.

## Deliberately not doing (yet)

Native async enqueue, deferred scheduling, and task priority are unsupported on purpose:
Absurd has no notion of priority, and async/deferral aren't wired — we won't fake them
behind a flag that implies otherwise.
