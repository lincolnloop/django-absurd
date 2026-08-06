# One worker mode, with working concurrency

Covers [#155](https://github.com/lincolnloop/django-absurd/issues/155) (slot refill)
plus removal of `absurd_worker --burst`. One change: both touch the same loop, the same
command, and the same ~50 test call sites, so splitting them means rebasing the test
migration onto a rewritten loop.

## Problem

Two defects, one cause — the worker never re-claims until the whole claimed batch
finishes.

**Slot refill.** `run_blocking_worker` (`worker.py:493`) delegates to the SDK's
`AsyncAbsurd.start_worker`, which rebuilds `executing` per batch and ends each iteration
with `await asyncio.gather(*executing)` (`absurd_sdk/__init__.py:2190-2210`). One slow
task idles every other slot. Measured on the load harness, 4 slots, mixed backlog: one
worker at `--concurrency 4` took 47.99s (sync) / 45.68s (async) against 16.36s / 15.12s
for four workers at `--concurrency 1`. Mean slots busy 1.06 of 4. Idle slot-seconds
while the backlog still held work: 141 vs 13 — 73% of offered capacity wasted. Uniform
durations showed only 1.07x, which identifies the batch boundary, not task length, as
cause.

The SDK's **sync** worker has no such bug (`__init__.py:1665-1706`): one `executing` set
across iterations, claims of `min(effective_batch_size, concurrency - len(executing))`.
That shape came from the fix for the sync-side bug
([upstream #91](https://github.com/earendil-works/absurd/issues/91)) and was never
applied to the async path. TypeScript and Go workers already cap claims by free
capacity; async Python was the only worker without refill.

**`--burst`.** Same barrier in `adrain_queue` (`worker.py:271`), with no windowing at
all. Not fixed — removed. See below.

## Decisions

Locked in design discussion; do not re-derive.

- **Carry a local loop now.**
  [Upstream PR #137](https://github.com/earendil-works/absurd/pull/137) fixes this in
  the SDK but has sat unreviewed since 2026-08-03. Port its structure into `worker.py`
  over the public `claim_tasks`; drop the local loop when a released SDK carries the
  fix.
- **`--burst` is removed, not fixed.** Absurd has no burst concept to inherit: the SDK
  offers `work_batch` (one batch, sequential, no drain-to-empty) and `start_worker`
  (resident); `absurdctl` ships no worker command at all. `--burst` was invented here
  for tests, and the drain-to-empty concept stays where it belongs — the test fixture.
  Peers do ship it (RQ `--burst`, Procrastinate `wait=False`), so this forfeits the
  scale-to-zero / cron-drain deployment story. Accepted: one worker mode is worth more
  than a deployment mode nobody has asked for.
- **`--concurrency` becomes a blocking-worker knob only.**
- **Graceful stop; cancel only on cancellation.** Stop signal → stop claiming, await
  in-flight to completion. Coroutine cancelled → cancel in-flight, then await them.
- **Own stop handle, passed in.** `stop: asyncio.Event | None = None` keyword on
  `run_blocking_worker`.
- **Command-test parity is a requirement.** Every assertion that runs the command today
  keeps running the command.
- **`--concurrency < 1` stays out of scope.** Already dies in
  `ThreadPoolExecutor(max_workers=0)` (`worker.py:186`) before the loop; ugly,
  unchanged.

## Non-goals

Burst refill (no burst). Any change to `dj_absurd.drain()` semantics. Priority.
`WorkerOptions` validation. Multi-queue workers.

## Design

### Refill loop

Port #137's structure into `worker.py`, over `client.claim_tasks` and the SDK's
`_execute_task`, keeping the existing signal wiring:

- `concurrency <= 1`: claim `batch_size or concurrency`, execute sequentially. Fast
  path, straight from the SDK's sync worker — without it, capping claims by free
  capacity silently reduces `--batch-size N --concurrency 1` from one round trip to N.
- otherwise: one `executing` set across iterations; reap finished tasks; claim
  `min(effective_batch_size, concurrency - len(executing))`; on an empty claim, wait for
  progress (`asyncio.wait`, `FIRST_COMPLETED`, `timeout=poll_interval`) or sleep
  `poll_interval` when nothing is in flight.
- `finally`: stop set → await the window; cancelled → cancel the window, then await it.
  `asyncio.wait` neither cancels nor awaits what it waits on, so without this a
  cancelled worker leaves handlers running against a connection about to close, and
  their runs sit `running` until lease expiry. The sync loop gets this free from
  `ThreadPoolExecutor.__exit__`.

Dispatch stays `client._execute_task`, the counted path `adrain_queue` already uses — a
third `SLF001` on the same rationale (SDK exposes no public counted dispatch), not a new
kind of reach.

**Claim-while-executing is safe.** The loop issues a claim while handlers run on the
client's single `AsyncConnection`. psycopg3 serializes: `AsyncConnection` holds an
`asyncio.Lock` (`psycopg/connection_async.py:81`) and `cursor_async.execute` acquires it
(`cursor_async.py:112,168`). Not new either — at `concurrency > 1` the SDK already runs
several `_execute_task`s against that one connection.

### Stop signal

`client.stop_worker()` sets `_worker_running`, read only by the SDK's own loop, and
`_worker_running` initializes `False` (`__init__.py:1215`) — reusing it would mean
writing another library's private attribute whose job we just took. So:
`run_blocking_worker(client, options, *, stop=None)`, creating an `asyncio.Event` when
omitted. `SIGINT`/`SIGTERM` handlers set it. `run_worker_with_beat` passes the same
event it uses for the beat thread, so one signal stops both.

### Burst removal

- `absurd_worker` loses `--burst`, and with it the `--burst`/`--beat` conflict check.
- `run_worker` loses `burst=`; blocking only.
- `drain_queue` keeps `adrain_queue` as its private, sequential engine — behavior
  unchanged, it already runs at `concurrency=1`. Public surface is `dj_absurd.drain()`.

Straight removal, no deprecation: alpha, and the package says so.

## Tests

RED first, from the issue, verified failing on `origin/main` for the right reason —
batch one claims `[slow, fast-1]` (`batch_size` defaults to `concurrency`), `fast-1`
finishes, `fast-2` is never claimed because the loop joins the whole batch:

```
AssertionError: the worker never started the third task while the slow one held a slot:
started ['slow', 'fast-1']
```

Deterministic: the slow task blocks on an `Event` the test owns, each task announces its
own start, and the only `wait_for` exists to turn "never started" into an assertion
failure instead of a hang. Adapted from the issue's version to drive shutdown through
`stop=` rather than `client.stop_worker()`.

**Time is not frozen here**, against the usual rule for tests that execute: a live
blocking worker and a real thread pool are the subject, so `dj_absurd.freeze_time` would
deadlock rather than help. `tests/core/test_scheduler.py` holds the same exemption for
its live worker.

Alongside it: graceful stop with work in flight (in-flight completes, no new claims),
cancel-and-drain (cancelled worker leaves no run `running`), and
`--batch-size N --concurrency 1` still claiming in one round trip.

### Migrating the ~50 `burst=True` call sites

Three classes:

1. **Execution-only (~32)** — `test_enqueue`, `test_orm_*`, `test_admin*`,
   `test_cleanup`, most of `test_scheduler`. Move to `dj_absurd.drain()`. One of those
   sites is `run_absurd_worker` (`tests/utils.py:68`), reached by 34 tests, so a single
   edit covers most of the class. Each test needs `transaction=True` (already true — a
   burst worker on its own connection only ever saw committed rows) and the `dj_absurd`
   fixture param.
2. **Raise before the loop (~6)** — alias rejection, no/multiple backends, undeclared
   queue, schema-absent. Drop the kwarg; the command validates and the schema probe
   fires before the worker loop starts.
3. **Command-level (~12)** — provisioning report, `Started/Stopped worker` lines, the
   `worker started: …` log line, mutable-policy reconcile output. These keep calling
   `call_command("absurd_worker", …)`; a shared helper in `tests/utils.py` runs a
   watcher thread that waits on a **condition** (expected rows terminal, or the started
   line) and then fires `SIGTERM`. Same shape as `test_scheduler.py:704-737`, which
   already does this for `--beat`. Signals are the command's real stop path, so this is
   more faithful than `--burst` was; the shape lives in one helper, and nothing sleeps
   on a timer.

`test_async_concurrency_is_not_serial` (`test_async_worker.py:122`) currently proves
async overlap through `--burst --concurrency 4`. It moves onto the blocking worker,
where `--concurrency` now lives.

## Docs

- `AGENTS.md:249-267` — one worker mode; drop the burst bullet; `--concurrency`
  described honestly as refilling a slot as soon as it frees.
- `AGENTS.md:852`, `docs/web/testing.md:122` — describe `dj_absurd.drain()` on its own
  terms instead of as "the fixture counterpart of `absurd_worker --burst`".

## Divergence

Carrying the loop forfeits future SDK worker fixes. When a released SDK carries #137 (or
an equivalent), prefer taking it via a version bump and deleting the local loop; the
`stop=` handle stays either way, since it is what makes an in-process stop testable.
