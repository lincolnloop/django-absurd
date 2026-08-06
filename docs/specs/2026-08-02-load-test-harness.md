# Load / perf harness — first cut

Issue: https://github.com/lincolnloop/django-absurd/issues/112

Goal: repeatable harness + baseline numbers for two questions. **Measure, don't tune.**

1. Admin at data volume — millions of task/run rows across several queues. Where's cost?
2. Worker concurrency / throughput — only ever exercised at `concurrency=1`.

Non-goal this cut: fixing anything. Harness ships **zero** production code. Probe
needing something `django_absurd/` doesn't expose = finding, not patch.

## Layout

```
loadtest/
  compose.yaml          # db_load service, PGPORT_LOAD (default 5436), own volume
  settings.py           # standalone settings: TASKS -> AbsurdBackend, admin, 4 queues
  manage.py
  tasks.py              # workload tasks, sync + async twins
  management/commands/
    load_seed.py
    load_admin.py
    load_drain.py
  results/              # gitignored
```

Persistent DB, NOT a pytest test DB. Data survives runs → admin browsable by hand in a
real browser alongside the scripted probes. Nothing auto-drops; destructive paths are
explicit flags (`--truncate`).

Four declared queues, lopsided: one carries the bulk, three carry token rows. Gives the
admin UNION-ALL views real arms in a realistic shape.

Provisioning free via `manage.py migrate` — `django_absurd/apps.py` connects
`provision_queues_after_migrate` on `post_migrate`, which creates per-queue tables and
admin views. No setup command.

## `load_seed`

Three phases.

**1. Template.** Real `enqueue()` of ~20 tasks, then run a worker briefly so rows exist
in genuine terminal shapes: succeeded, failed-with-retries, still-queued. Templates are
rows Absurd itself wrote. Never hand-author a row.

**2. Clone.** Per queue, server-side:

```sql
INSERT INTO absurd.t_<q> (...)
SELECT ... FROM absurd.t_<q> tmpl, generate_series(1, :n)
```

Chunked ~100k with progress. Same for `absurd.r_<q>`, FK'd to new task ids, same txn.

Real `spawn()` is one round-trip per task → ~1e5 rows ceiling in tolerable wall-clock.
Clone-SQL reaches millions in seconds and inherits the pinned absurdctl schema instead
of restating it.

**3. ANALYZE** every touched table. Non-negotiable — stale planner stats make every
downstream EXPLAIN a lie.

### Drift guard

Clone is `SELECT tmpl.*` with an override map:

- `task_id` → fresh uuidv7
- timestamps → jittered across `--window` (default 30d)
- state → drawn from a distribution

Seeder reads real column list from `information_schema` and **hard-fails when it doesn't
match the map's expected set**. A pinned-absurdctl bump adding a column then surfaces as
`loadtest: unknown column "x", update OVERRIDES` — not a duplicated id silently cloned
into millions of rows.

### Scope

Tasks + runs bulked. Checkpoint / Event / Wait get only template-phase token rows: same
view machinery, interesting cost sits in the big tables. Bulk them later if the admin
probe says otherwise.

## `load_admin`

Timed HTTP + captured SQL + plans.

- Superuser login via Django test client against the persistent DB.
- Fixed URL list: each admin entity × {unfiltered, `?queue=`, `?state=`, page 1, deep
  page `?p=500`}.
- Per URL: wall-clock, query count, slowest queries off `connection.queries`.
- Then re-run the changelist's COUNT query and paged SELECT under
  `EXPLAIN (ANALYZE, BUFFERS)`; plans dumped to `results/`.

Wall-clock alone can't separate the COUNT from the ordering key from the union. Plans
can — `Sort` over the whole `Append` vs `Merge Append` with LIMIT stopping early. Prior
unfiled measurement says the `natural_key` expression sort dominates, not the COUNT;
this probe confirms or kills that at real volume.

## `load_drain`

Fixed-backlog drain matrix.

- Seed N queued tasks via real `enqueue()` — claim semantics must be genuine, so no
  clone-SQL here.
- Spawn M `absurd_worker` **subprocesses** × `--concurrency C`. Poll until drained.
- Report tasks/sec, wall-clock, duplicate-execution count.
- Duplicates counted, not assumed: workload task appends `(task_id, pid)` to a plain
  side table. Rows > distinct task_ids = at-least-once redelivery. No run id — the SDK
  exposes no public accessor for it, and duplicate detection doesn't need one.
- Matrix `(1x1, 1x4, 4x1, 4x4) x (sync task, async task)`. Sync arm is the point — the
  sync↔async bridge under `concurrency>1` is the top break-list suspect and has only run
  at 1.
- Each cell truncates + re-seeds. Cells independent.

## Results

Readable table to stdout. Each run also writes a timestamped JSON + plan dump under
`loadtest/results/` (gitignored). Keep what's worth keeping by hand.

No committed baseline file: numbers are machine- and load-dependent, and a checked-in
baseline invites treating one laptop's number as the project's number. Revisit once we
know which numbers are stable enough to be worth diffing.

## Deliberately out of first cut

**Multi-queue throughput.** `load_drain` drains ONE queue (`bulk`). The four declared
queues exist for the admin's UNION-ALL arms, not for throughput. Next cut: `--queue`
repeatable on `load_drain`, cells gain a queue-spread dimension, workload tasks reroute
per cell via `.using()` (that path already exists — they're declared
`queue_name="bulk"`). Hypothesis worth testing: Absurd gives each queue its own
`t_`/`r_` tables, so claim contention should be queue-LOCAL — predicting 4 queues × 1
worker beats 4 workers × 1 queue at equal worker count. If it doesn't, the bottleneck is
shared (connections, WAL, DB CPU), which is the more interesting finding. Neither number
exists today.

Sustained-rate producer / steady-state backlog. Soak + leak runs. SIGKILL chaos +
durable-resume. Clock skew (host vs DB). Durable-sleep / `await_event` fan-out at scale.
pg_cron fan-out. Everything security (injection, JSON DoS, privilege boundary).

Harness grows into these. None block a baseline.
