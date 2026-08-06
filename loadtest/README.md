# loadtest — django-absurd load harness

A standalone Django project for measuring django-absurd under volume. It exists to
answer questions with numbers instead of guesses:

- how the [admin](../docs/web/how-it-works.md#admin-orm-introspection) behaves once the
  entity views span millions of task rows,
- what worker throughput looks like above `--concurrency 1`,
- whether a task suspended in a [durable sleep](../docs/web/workflows.md#sleep) costs
  the worker a concurrency slot, and
- what the worker's batch barrier costs once a backlog's tasks are not all the same
  length.

**This is a dev harness.** It is not part of the distributed package, it is not run in
CI, and nothing under `django_absurd/` may depend on it. Its own tests only prove the
harness itself is wired up — they are not a performance gate.

## Layout

| Path                                   | What it is                                                               |
| -------------------------------------- | ------------------------------------------------------------------------ |
| `settings.py`                          | Its own project settings — four queues: `bulk`, `alpha`, `beta`, `gamma` |
| `tasks.py`                             | `burn_*` / `nap_*` / `toil_*` / `burn_workflow` — the workload tasks     |
| `models.py`                            | `ExecutionLog` and `OccupancyLog` — the evidence a run leaves behind     |
| `schema.py`                            | What the clone knows about the queue tables, plus the drift guard        |
| `management/commands/load_seed.py`     | Template → clone → `ANALYZE`                                             |
| `management/commands/load_admin.py`    | The admin probe — timings, query counts, `EXPLAIN` dumps                 |
| `management/commands/load_drain.py`    | The worker probe — throughput over a worker × concurrency matrix         |
| `management/commands/load_sleepers.py` | The sleep probe — does a suspended task hold a worker slot?              |
| `management/commands/load_barrier.py`  | The barrier probe — what one slow task in a batch costs its siblings     |
| `results.py`                           | Where a run's JSON and plan dumps land                                   |
| `compose.yaml`                         | `db_load`, the harness's own Postgres on its own port                    |
| `tests/`                               | Wiring tests for the harness                                             |

`bulk` carries the volume; `alpha`/`beta`/`gamma` exist so a probe can spread work over
several queues.

## Set up

Bring up the harness database — its own server on port 5436, separate from the suites'
so a multi-million-row seed never lands in a throwaway test database:

```console
$ docker compose -f loadtest/compose.yaml up -d db_load
```

The named volume keeps the data across `docker compose down`, so a seed that took an
hour to build survives. `docker compose -f loadtest/compose.yaml down -v` is what
actually throws it away.

Then create the schema:

```console
$ PGPORT=5436 python -m loadtest.manage migrate
```

`migrate` also provisions all four
[declared queues](../docs/web/configuration.md#declaring-queues) — django-absurd does
that from `post_migrate`, so there is no separate sync command to run.

`loadtest/settings.py` already defaults to port 5436 and database `absurd_load`. The
explicit `PGPORT=5436` shown throughout is there because this repo's other suites export
a different `PGPORT`; drop it if your shell exports none. Point compose somewhere else
with `PGPORT_LOAD`, and the client side follows with `PGPORT`.

## Seed the data

```console
$ PGPORT=5436 python -m loadtest.manage load_seed --tasks 2000000 --truncate
```

`load_seed` enqueues a handful of real tasks per queue and drains them with a burst
worker, so every seeded row is one Absurd itself wrote — completed, failed with a retry
history, still pending, and one suspended [workflow](../docs/web/workflows.md) leaving a
checkpoint, an event and a wait behind. It then clones those templates server-side in
100k chunks until the queue holds `--tasks` rows, jittering every timestamp across
`--window` days (default 30), and `ANALYZE`s each table it touched.

`--tasks` applies to `bulk`; `alpha`, `beta` and `gamma` get a fixed 1000 each, so the
admin's union views have arms of realistically mismatched size. `--queue` (repeatable)
narrows the run to specific queues.

`--truncate` is the only destructive flag and is never implied — without it a seed adds
to whatever is already there. It empties the selected queues' tables, and the execution
log in full: the log has no queue column, so `--queue bulk --truncate` still clears
every queue's executions. Checkpoints, events and waits are never cloned: the
interesting cost sits in the two big tables.

If a pinned-`absurdctl` bump changes the per-queue table columns, the run fails before
writing a row, naming the column and pointing at `loadtest/schema.py`. That is the guard
doing its job — update the clone column list and override map there.

## Run the probes

The whole sequence, from an empty machine to numbers:

```console
$ docker compose -f loadtest/compose.yaml up -d db_load
$ PGPORT=5436 python -m loadtest.manage migrate
$ PGPORT=5436 python -m loadtest.manage load_seed --tasks 2000000 --truncate
$ PGPORT=5436 python -m loadtest.manage load_admin
$ PGPORT=5436 python -m loadtest.manage load_drain
```

`load_admin` reads the seeded data: it drives every admin changelist through the real
request cycle and records wall clock, query count and the `EXPLAIN (ANALYZE, BUFFERS)`
plan behind the paginator's `COUNT` and the paged `SELECT`. `--entity` (repeatable,
default: every entity) narrows it; `--deep-page` (default 500) is the deep-pagination
arm's page number. On an entity whose rows reach that far it must exist in the seeded
data, or the changelist redirects and the probe refuses to time it. On one too small to
paginate at all — checkpoints, events and waits carry only the workflow templates' few
rows — the admin ignores `?p=` and renders page 1, so the probe drops that arm instead
of recording a page-1 render under a deep-page label.

`load_drain` writes its own: each cell empties the `bulk` queue and the execution log,
enqueues `--tasks` tasks (default 1000) and drains them with `--cell` workers at that
cell's concurrency (repeatable, default `1x1 1x4 4x1 4x4`) — `--workload` picks `sync`,
`async` or `both` (default `both`). Its `elapsed_s` is wall clock around the whole
worker phase, including each child's start-up, so cells are comparable with each other
rather than absolute. `--timeout` (default 900) bounds a cell; outlasting it fails the
run rather than recording a number.

**Run `load_drain` last.** It truncates `bulk` per cell, so a seed does not survive it —
reseed before the next `load_admin`.

`load_sleepers` answers a different question and is run on its own:

```console
$ PGPORT=5436 python -m loadtest.manage load_sleepers --queue gamma
```

It drains `--quick` ordinary tasks (default 200) with one worker at `--concurrency`
(default 4), twice: once with nothing else on the queue, and once with `--sleepers`
tasks (default 100) already suspended in a `--sleep-seconds` durable sleep (default an
hour — long enough that none of them can wake mid-measurement). If a sleep releases its
slot the two timings are comparable; if it holds one, the quick batch starves and the
ratio explodes. Alongside the timings it counts the sleepers' own runs by state while
the quick batch drains — `running_max` is a sleeper holding a claim, `sleeping_min` one
holding nothing — which answers the same question without a clock. `--workload` picks
`sync`, `async` or `both` (default `both`); only the sync path crosses the worker's
thread pool, so the two are worth reading separately. A quick batch that outlasts
`--timeout` (default 300) fails the run instead of recording a number.

It truncates the queue it probes, so point `--queue` at `alpha`, `beta` or `gamma` — it
refuses `bulk` outright rather than eat the seeded dataset — and top the queue back up
afterwards with `load_seed --queue <name>` if you still want its admin arm populated.

`load_barrier` is run on its own too:

```console
$ PGPORT=5436 python -m loadtest.manage load_barrier --queue gamma
```

The worker claims a batch and waits for all of it before claiming again, so one slow
task in a batch can hold slots its siblings could have used. A uniform backlog hides
that entirely, which is why the probe runs a control: `uniform` gives every task the
same length, `mixed` puts `--slow` long tasks (`--slow-seconds`, default 1) evenly
through `--fast` short ones (`--fast-seconds`, default 0.01), and the uniform length is
the mixed backlog's mean, so the two carry identical total work and differ only in
variance. Each is drained twice at equal total slots — `pooled` is one worker at
`--concurrency` (default 4), `split` is that many workers at concurrency 1 — because
separate processes each run their own loop and a straggler stalls only its own.

The ratio between the two is not the deliverable; a slow task really is slow, so a ratio
alone proves nothing. Each execution records the interval its slot was occupied
(`OccupancyLog`), and the arm's timeline is rebuilt from those intervals:
`mean_busy`/`utilization` are how much of the concurrency was really executing, and
`idle_slot_s` integrates idle slots **while the backlog still held work** — slot-seconds
that were available, wanted and unused. `ramp_s` is how late the last worker joined,
which bounds how much of a multi-worker arm's idle time is interpreter start-up rather
than a stall. `--workload` picks `sync`, `async` or `both` (default `both`); an arm that
outlasts `--timeout` (default 600) fails the run rather than record a number.

Like `load_sleepers` it truncates the queue it probes and refuses `bulk`.

Each probe prints a table, writes its numbers to
`loadtest/results/<probe>-<UTC stamp>.json` and echoes that path as its final line.
`loadtest/results/` is gitignored, so runs accumulate and you keep what is worth keeping
by hand; `load_admin` puts its plan dumps in a directory of the same stamp beside the
JSON.

## Run the harness's own tests

```console
$ PGPORT=5436 uv run pytest loadtest -v
```

These are excluded from the root `pytest` run (`--ignore=loadtest` in the root
`pyproject.toml`) and from coverage, exactly like the three real suites are invoked
explicitly.
