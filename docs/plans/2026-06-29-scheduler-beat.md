# Scheduler (beat) — SP1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Plan style (project override of the skill template):** Implementation steps are
> described in **prose**, not finished code blocks — coding-ahead is banned. Only
> **test** code is shown verbatim (RED first). Prose is caveman-compressed; code/tests
> are normal.

**Goal:** Declare recurring tasks in `TASKS` settings; an `absurd_beat` process enqueues
them on a cron cadence through the existing enqueue path. No pg_cron, no new tables.

**Architecture:** New `django_absurd/scheduler.py` holds the shared core (`Schedule`,
settings provider, slot math, spawn) + the async `arun_beat` loop. An `absurd_beat`
management command runs it; `absurd_worker --beat` runs it in the worker's event loop.
Spec: `docs/specs/2026-06-29-scheduler-design.md`.

**Tech Stack:** Django 6 Tasks framework, Absurd SDK, croniter, freezegun (tests).

## Global Constraints

- Floor: Django ≥6.0, Python ≥3.12. Postgres + psycopg3 only.
- `import typing as t` (never `from typing import X`); absolute imports only; functions
  contain a verb; no leading-underscore module-level names; helpers placed BELOW the
  public fn that uses them.
- Tests: pytest, function-based only. Autouse `_enable_db(db)` gives DB access — do NOT
  add `@pytest.mark.django_db`. Add `pytest.mark.django_db(transaction=True)`
  (module-level `pytestmark`) only when the test does DDL/commits (queue create,
  enqueue, worker). No `unittest.mock` / monkeypatch — drive branches with real inputs
  or dependency injection.
- Checks/commands tested by RUNNING them and asserting full emitted text (capsys), per
  project convention.
- ruff `select = ["ALL"]` (minus configured ignores); mypy clean. No new ruff ignores
  without asking.
- 100% statement+branch coverage on lines this plan adds/changes.
- Cron interpreted in **Django `TIME_ZONE`**. **Fire-forward-only** (never backfill).
  **Single beat instance** is an operator contract (no idempotency guard). Logging:
  logger `"django_absurd"`, past-tense after an action.
- Beat is **async** (`arun_beat`); fires through the real `enqueue` path; no hand-built
  params.

---

## Task 1: Dependencies + test settings

**Files:**

- Modify: `pyproject.toml`, `tests/settings.py`
- Test: `tests/test_scheduler.py` (create)

**Interfaces:**

- Produces: `croniter` importable at runtime; `freezegun` in the dev group;
  `tests/settings.py` pins `TIME_ZONE = "UTC"`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_scheduler.py`:

```python
import croniter  # noqa: F401  -- runtime dep must be importable


def test_croniter_available():
    assert croniter.croniter.is_valid("*/5 * * * *")
```

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_scheduler.py -v` Expected: collection/import error —
`croniter` not installed.

- [ ] **Step 3: Implement (prose)**

Add `croniter` to `[project] dependencies` (permissive lower bound, `"croniter>=2.0"`).
Add `freezegun` to `[dependency-groups] dev`, pinned `==<resolved>` to match the
exact-pin style (run `uv add --group dev freezegun`, copy the resolved version from
`uv.lock`, keep the list alphabetised). Add `TIME_ZONE = "UTC"` to `tests/settings.py`
so cron/slot tests are deterministic without per-test overrides. Run `uv sync`.

Not adding `pytest-asyncio`: SP1's scheduler tests are **sync** — they drive the async
loop via `asyncio.run` internally and assert observable DB side effects (matching the
repo's existing `asyncio.run`-wrapper style). Adopting `pytest-asyncio` suite-wide +
converting existing async tests (e.g. `test_aenqueue_lands`, async-worker drain) is a
separate future cleanup.

- [ ] **Step 4: Run, verify it passes**

Run: `uv run pytest tests/test_scheduler.py -v` Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock tests/settings.py tests/test_scheduler.py
git commit -m "build: add croniter runtime dep + freezegun; pin test TIME_ZONE=UTC"
```

---

## Task 2: `Schedule` + settings provider

**Files:**

- Create: `django_absurd/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**

- Produces:
  - `Schedule` — frozen dataclass: `name: str`, `task: str`, `cron: str`,
    `queue: str | None = None`, `args: list = field(default_factory=list)`,
    `kwargs: dict = field(default_factory=dict)`.
  - `get_settings_schedules(backend: AbsurdBackend) -> list[Schedule]` — reads
    `backend.options.get("SCHEDULE", {})`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_scheduler.py`:

```python
from django_absurd.backends import get_absurd_backends
from django_absurd.scheduler import Schedule, get_settings_schedules


def schedule_tasks_setting(schedule):
    return {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "OPTIONS": {"QUEUES": {"default": {}, "reports": {}}, "SCHEDULE": schedule},
        }
    }


def test_settings_provider_reads_entries(settings):
    settings.TASKS = schedule_tasks_setting(
        {
            "nightly": {
                "task": "tests.tasks.add",
                "cron": "0 2 * * *",
                "args": [1, 2],
                "queue": "reports",
            }
        }
    )
    backend = get_absurd_backends()["default"]
    schedules = get_settings_schedules(backend)
    assert schedules == [
        Schedule(
            name="nightly",
            task="tests.tasks.add",
            cron="0 2 * * *",
            queue="reports",
            args=[1, 2],
            kwargs={},
        )
    ]


def test_settings_provider_defaults_and_empty(settings):
    settings.TASKS = schedule_tasks_setting(
        {"ping": {"task": "tests.tasks.add", "cron": "*/5 * * * *"}}
    )
    backend = get_absurd_backends()["default"]
    (s,) = get_settings_schedules(backend)
    assert s.queue is None and s.args == [] and s.kwargs == {}


def test_settings_provider_no_schedule_key(settings):
    settings.TASKS = {
        "default": {
            "BACKEND": "django_absurd.backends.AbsurdBackend",
            "QUEUES": ["default"],
        }
    }
    backend = get_absurd_backends()["default"]
    assert get_settings_schedules(backend) == []
```

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_scheduler.py -v` Expected: ImportError (`scheduler` has
no `Schedule`/`get_settings_schedules`).

- [ ] **Step 3: Implement (prose)**

Create `django_absurd/scheduler.py`. Define the frozen `Schedule` dataclass (mutable
defaults via `dataclasses.field(default_factory=...)`). Define
`get_settings_schedules(backend)`: read the `SCHEDULE` map from `backend.options`; for
each `name → spec`, build a `Schedule` taking `task`/`cron` directly and
`queue`/`args`/`kwargs` via `.get` with the documented defaults (coerce args→list,
kwargs→dict). Return the list. `import typing as t`; import `AbsurdBackend` from
`django_absurd.backends`.

- [ ] **Step 4: Run, verify it passes**

Run: `uv run pytest tests/test_scheduler.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/scheduler.py tests/test_scheduler.py
git commit -m "feat: Schedule dataclass + settings schedule provider"
```

---

## Task 3: `get_next_datetime` (croniter, Django timezone)

**Files:**

- Modify: `django_absurd/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**

- Produces: `get_next_datetime(cron: str, after: datetime) -> datetime` — next fire
  strictly after `after`, cron interpreted in Django current tz, returns an aware
  datetime.

- [ ] **Step 1: Write the failing test**

Append:

```python
import datetime as dt

from django.utils import timezone
from freezegun import freeze_time

from django_absurd.scheduler import get_next_datetime


@freeze_time("2026-01-01 01:59:00")
def test_get_next_datetime_same_day():
    expected = dt.datetime(2026, 1, 1, 2, 0, tzinfo=dt.timezone.utc)
    assert get_next_datetime("0 2 * * *", timezone.now()) == expected


@freeze_time("2026-01-01 02:00:00")
def test_get_next_datetime_rolls_forward():
    expected = dt.datetime(2026, 1, 2, 2, 0, tzinfo=dt.timezone.utc)
    assert get_next_datetime("0 2 * * *", timezone.now()) == expected


@freeze_time("2026-01-01 12:00:00")  # 06:00 in Chicago (UTC-6)
def test_get_next_datetime_uses_django_timezone(settings):
    # cron interpreted in Django TIME_ZONE: 02:00 Chicago already passed -> tomorrow
    settings.TIME_ZONE = "America/Chicago"
    expected = dt.datetime(2026, 1, 2, 8, 0, tzinfo=dt.timezone.utc)  # 02:00 CST = 08:00 UTC
    result = get_next_datetime("0 2 * * *", timezone.now()).astimezone(dt.timezone.utc)
    assert result == expected
```

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_scheduler.py -k get_next_datetime -v` Expected:
ImportError (`get_next_datetime` undefined).

- [ ] **Step 3: Implement (prose)**

Add `get_next_datetime(cron, after)`: convert `after` into the Django current timezone
with `django.utils.timezone.localtime(after)`, build `croniter(cron, local_after)`,
return `croniter.get_next(datetime)` (aware, in the local tz). No mutation of global
state.

- [ ] **Step 4: Run, verify it passes**

Run: `uv run pytest tests/test_scheduler.py -k get_next_datetime -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/scheduler.py tests/test_scheduler.py
git commit -m "feat: get_next_datetime cron slot computation in Django timezone"
```

---

## Task 4: `spawn_scheduled`

**Files:**

- Modify: `django_absurd/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**

- Produces: `spawn_scheduled(schedule: Schedule) -> None` — resolve `schedule.task`
  (dotted path) to a Django Task, route to `schedule.queue` via `.using(queue_name=...)`
  when set, enqueue with `*args, **kwargs`. Closes stale connections around the call
  (safe to run in a worker thread).

- [ ] **Step 1: Write the failing test**

Append. (These do DDL/commits + enqueue → the module needs
`pytestmark = pytest.mark.django_db(transaction=True)`; add it once near the top of the
file when first required.)

```python
from django.contrib.auth.models import Group
from django.core.management import call_command

from django_absurd.scheduler import spawn_scheduled


def test_spawn_scheduled_runs_task_with_args():
    call_command("absurd_sync_queues")
    spawn_scheduled(
        Schedule(name="n", task="tests.tasks.make_group", cron="* * * * *", args=["sched-args"])
    )
    call_command("absurd_worker", queue="default", burst=True)
    assert Group.objects.filter(name="sched-args").exists()


def test_spawn_scheduled_passes_kwargs():
    call_command("absurd_sync_queues")
    spawn_scheduled(
        Schedule(name="n", task="tests.tasks.make_group", cron="* * * * *", kwargs={"name": "sched-kwargs"})
    )
    call_command("absurd_worker", queue="default", burst=True)
    assert Group.objects.filter(name="sched-kwargs").exists()


def test_spawn_scheduled_routes_to_queue():
    # route make_group to "other": only a worker draining "other" runs it.
    call_command("absurd_sync_queues")
    spawn_scheduled(
        Schedule(name="n", task="tests.tasks.make_group", cron="* * * * *", queue="other", args=["routed"])
    )
    call_command("absurd_worker", queue="default", burst=True)
    assert not Group.objects.filter(name="routed").exists()  # not on default
    call_command("absurd_worker", queue="other", burst=True)
    assert Group.objects.filter(name="routed").exists()  # ran from "other"
```

(`tests/settings.py` declares queues `["default", "other"]`. Asserting via a real worker
run keeps the test behavioral — no raw queue-row inspection.)

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_scheduler.py -k spawn_scheduled -v` Expected: ImportError
(`spawn_scheduled` undefined).

- [ ] **Step 3: Implement (prose)**

Add `spawn_scheduled(schedule)`: call `django.db.close_old_connections()`, then in a
`try/finally` (finally also `close_old_connections()`):
`task = import_string(schedule.task)`; if `schedule.queue is not None`,
`task = task.using(queue_name=schedule.queue)`;
`task.enqueue(*schedule.args, **schedule.kwargs)`. The connection bookkeeping mirrors
`worker.build_handler`'s `call_sync` so the function is safe when invoked from the
loop's executor thread.

- [ ] **Step 4: Run, verify it passes**

Run: `uv run pytest tests/test_scheduler.py -k spawn_scheduled -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/scheduler.py tests/test_scheduler.py
git commit -m "feat: spawn_scheduled enqueues a scheduled task via the real enqueue path"
```

---

## Task 5: `arun_beat` loop

**Files:**

- Modify: `django_absurd/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**

- Produces:

  ```python
  async def arun_beat(
      backend: AbsurdBackend,
      *,
      now: t.Callable[[], datetime] = timezone.now,
      sleep: t.Callable[[float], t.Awaitable[None]] = asyncio.sleep,
      stop: asyncio.Event | None = None,
  ) -> None
  ```

  `now`/`sleep`/`stop` are runtime injection seams (clock, wait, shutdown) — tests use
  them to drive time deterministically. Each due schedule is fired via `spawn_scheduled`
  run in the loop's default executor (so the loop never blocks on sync DB I/O).
  Fire-forward-only; a per-schedule spawn failure is logged and does not stop the loop.

- [ ] **Step 1: Write the failing test**

Behavioral + deterministic: drive the loop with a fake clock + `stop`, fire real
enqueues, drain with a worker, assert observable rows. Sync tests (run the loop via
`asyncio.run`). Append:

```python
import asyncio

from tests.models import Payload


def schedule_backend(settings, schedule):
    settings.TASKS = schedule_tasks_setting(schedule)
    return get_absurd_backends()["default"]


def test_beat_fires_each_due_slot(settings):
    backend = schedule_backend(
        settings, {"p": {"task": "tests.tasks.create_payload", "cron": "*/1 * * * *", "args": ["tick"]}}
    )
    call_command("absurd_sync_queues")
    stop = asyncio.Event()
    cutoff = dt.datetime(2026, 1, 1, 0, 2, 30, tzinfo=dt.timezone.utc)

    with freeze_time("2026-01-01 00:00:00") as frozen:
        async def fake_sleep(seconds):
            frozen.tick(dt.timedelta(seconds=seconds))
            if timezone.now() >= cutoff:
                stop.set()

        asyncio.run(arun_beat(backend, sleep=fake_sleep, stop=stop))

    call_command("absurd_worker", queue="default", burst=True)
    expected_fires = 2  # slots 00:01 and 00:02
    assert Payload.objects.count() == expected_fires


def test_beat_no_schedules_returns(settings):
    backend = schedule_backend(settings, {})
    asyncio.run(arun_beat(backend, stop=asyncio.Event()))  # returns immediately, no hang


def test_beat_isolates_failing_schedule(settings):
    backend = schedule_backend(
        settings,
        {
            "bad": {"task": "tests.tasks.does_not_exist", "cron": "*/1 * * * *"},
            "good": {"task": "tests.tasks.create_payload", "cron": "*/1 * * * *", "args": ["ok"]},
        },
    )
    call_command("absurd_sync_queues")
    stop = asyncio.Event()
    cutoff = dt.datetime(2026, 1, 1, 0, 1, 30, tzinfo=dt.timezone.utc)

    with freeze_time("2026-01-01 00:00:00") as frozen:
        async def fake_sleep(seconds):
            frozen.tick(dt.timedelta(seconds=seconds))
            if timezone.now() >= cutoff:
                stop.set()

        asyncio.run(arun_beat(backend, sleep=fake_sleep, stop=stop))

    call_command("absurd_worker", queue="default", burst=True)
    expected_good = 1  # "bad" raised in spawn (unimportable, logged); "good" still ran
    assert Payload.objects.count() == expected_good
```

(`TIME_ZONE` is pinned to UTC in `tests/settings.py`, so the frozen instants and slot
assertions stay UTC-clean. The fake `sleep` advances the frozen clock and trips `stop`
once the cutoff passes — time/shutdown seams, not observation hooks; the assertions are
real enqueued rows.)

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_scheduler.py -k test_beat -v` Expected: ImportError
(`arun_beat` undefined).

- [ ] **Step 3: Implement (prose)**

Add `arun_beat`. Resolve schedules via `get_settings_schedules(backend)`; if empty, log
"no schedules declared" and return. Build a
`dict name → get_next_datetime(cron, now())`. Loop while `stop` is None-or-unset: pick
the earliest upcoming time; `delay = (earliest - now()).total_seconds()`; if
`delay > 0`, `await sleep(delay)`; if `stop` set, break; recompute `current = now()`;
for every schedule whose upcoming time `<= current`, fire it and advance its upcoming
time to `get_next_datetime(cron, current)`. Fire by awaiting
`asyncio.get_running_loop().run_in_executor(None, spawn_scheduled, schedule)` inside its
own `try/except` (log via `logger.exception`, continue) so one bad schedule never stops
the loop. Extract a verb-named helper below `arun_beat` if the fire-one-schedule block
needs it; keep names underscore-free.

- [ ] **Step 4: Run, verify it passes**

Run: `uv run pytest tests/test_scheduler.py -k test_beat -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/scheduler.py tests/test_scheduler.py
git commit -m "feat: arun_beat scheduler loop (fire-forward, failure-isolated)"
```

---

## Task 6: `absurd_beat` management command

**Files:**

- Create: `django_absurd/management/commands/absurd_beat.py`
- Modify: `django_absurd/scheduler.py` (add `run_beat` sync wrapper)
- Test: `tests/test_scheduler.py`

**Interfaces:**

- Produces:
  - `run_beat(backend: AbsurdBackend, *, stop: asyncio.Event | None = None) -> None` —
    sync wrapper: `validate_backend(backend.database)` then
    `asyncio.run(arun_beat(backend, stop=stop))`.
  - `absurd_beat` command: selects the backend exactly like `absurd_worker` (`--alias`;
    auto-select when single; `CommandError` listing aliases otherwise), installs
    SIGINT/SIGTERM → set a `stop` event, logs the declared-schedule count, calls
    `run_beat`.

- [ ] **Step 1: Write the failing test**

The blocking loop is covered by Task 5; here test the command's own logic — backend
selection. Append:

```python
from django.core.management.base import CommandError


def test_absurd_beat_unknown_alias_errors(settings):
    settings.TASKS = schedule_tasks_setting(
        {"m": {"task": "tests.tasks.add", "cron": "*/5 * * * *"}}
    )
    with pytest.raises(CommandError, match="not an Absurd backend alias"):
        call_command("absurd_beat", alias="nope")


def test_absurd_beat_multiple_backends_requires_alias(settings):
    settings.TASKS = {
        "default": {"BACKEND": "django_absurd.backends.AbsurdBackend", "QUEUES": ["default"]},
        "second": {"BACKEND": "django_absurd.backends.AbsurdBackend", "OPTIONS": {"DATABASE": "default", "QUEUES": {"default": {}}}},
    }
    with pytest.raises(CommandError, match="Use --alias"):
        call_command("absurd_beat")
```

(Mirror the exact `CommandError` wording from `absurd_worker.py` so the `match=` strings
line up; adjust if the wording differs.)

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_scheduler.py -k absurd_beat -v` Expected:
`CommandError: Unknown command: 'absurd_beat'` (command not found).

- [ ] **Step 3: Implement (prose)**

Add `run_beat` to `scheduler.py` (sync wrapper as in Interfaces). Create the command
subclassing `django.core.management.base.BaseCommand`. `add_arguments`: `--alias`
(default None). `handle`: copy `absurd_worker`'s backend-selection block
(get_absurd_backends, alias validation, single-auto-select, multi → CommandError) —
extract that block into a shared helper only if trivial; otherwise duplicate the few
lines (DRY is nice-to-have, not worth a risky refactor here). Build an `asyncio.Event`
stop; register SIGINT/SIGTERM handlers that set it (guard for platforms without signal
support is unnecessary — same assumptions as the worker). `self.stdout.write` the
declared-schedule count (e.g. `f"Started beat with {n} schedule(s)."`). Call
`run_beat(backend, stop=stop)`.

- [ ] **Step 4: Run, verify it passes**

Run: `uv run pytest tests/test_scheduler.py -k absurd_beat -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/scheduler.py django_absurd/management/commands/absurd_beat.py tests/test_scheduler.py
git commit -m "feat: absurd_beat management command + run_beat wrapper"
```

---

## Task 7: `absurd_worker --beat`

**Files:**

- Modify: `django_absurd/worker.py` (`arun_worker`, `run_worker` gain `run_beat`)
- Modify: `django_absurd/management/commands/absurd_worker.py` (add `--beat` flag)
- Test: `tests/test_scheduler.py`

**Interfaces:**

- Consumes: `arun_beat` (Task 5).
- Produces: `run_worker(..., run_beat: bool = False)` /
  `arun_worker(..., run_beat: bool = False)`. When `run_beat` and NOT burst, the worker
  gathers the blocking worker with `arun_beat(backend, stop=<shared>)` on one event
  loop; SIGINT/SIGTERM stop both. `--beat` flag on `absurd_worker` (store_true, blocking
  mode only).

- [ ] **Step 1: Write the failing test**

End-to-end at the command level: run `absurd_worker --beat` (main thread — asyncio
signal handlers require it); a watcher thread sends SIGTERM once the scheduled side
effect lands. `freeze_time(tick=True)` near a minute boundary makes the schedule fire
within ~1s of real time. Append:

```python
import os
import signal
import threading
import time

from django.contrib.auth.models import Group
from django.core.management.base import CommandError


def test_worker_beat_rejects_burst(settings):
    settings.TASKS = schedule_tasks_setting(
        {"g": {"task": "tests.tasks.make_group", "cron": "*/1 * * * *", "args": ["x"]}}
    )
    with pytest.raises(CommandError, match="--beat"):
        call_command("absurd_worker", queue="default", burst=True, beat=True)


def test_worker_with_beat_runs_scheduled_task(settings):
    settings.TASKS = schedule_tasks_setting(
        {"g": {"task": "tests.tasks.make_group", "cron": "*/1 * * * *", "args": ["beat-ran"]}}
    )
    call_command("absurd_sync_queues")

    def watch():
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if Group.objects.filter(name="beat-ran").exists():
                break
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)  # stop worker + beat (main-thread handler)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    # tick=True near a boundary: next "*/1" slot is ~1s away in real time, so beat fires
    # almost immediately; the live worker (fast poll) drains it.
    with freeze_time("2026-01-01 00:00:59", tick=True):
        call_command("absurd_worker", queue="default", beat=True, poll_interval=0.05)
    watcher.join(timeout=5)

    assert Group.objects.filter(name="beat-ran").exists()
```

(Caveats: the worker runs in the **main thread** — asyncio signal handlers require it.
The watcher thread polls via its own ORM connection — sees committed rows under
`transaction=True` — and sends a single SIGTERM, which the command's handler turns into
a clean stop of both worker and beat. The 15s deadline is a safety bound; on success the
Group appears in ~1s.)

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_scheduler.py -k worker_beat -v` Expected: error —
`absurd_worker` has no `--beat` option yet.

- [ ] **Step 3: Implement (prose)**

Extend `arun_worker` with `run_beat: bool = False` and an optional shared
`stop: asyncio.Event | None = None` (create one when None). In the non-burst branch,
when `run_beat`:
`await asyncio.gather(run_blocking_worker(client, options), arun_beat(backend, stop=stop))`;
otherwise behave exactly as today. Extend the SIGINT/SIGTERM handlers (set in
`run_blocking_worker`) to also set the shared `stop` so beat exits on the same signal.
Thread `run_beat` + `stop` through `run_worker`. No beat clock/sleep seams on
`arun_worker` — the command runs real time; the test drives timing via
`freeze_time(tick=True)`. Add `--beat` (store_true) to `absurd_worker`; pass
`run_beat=options["beat"]` into `run_worker`. Burst + `--beat` is not supported — if
both given, raise `CommandError`.

- [ ] **Step 4: Run, verify it passes**

Run: `uv run pytest tests/test_scheduler.py -k worker_with_beat -v` → PASS. Then full
file: `uv run pytest tests/test_scheduler.py -v`.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/worker.py django_absurd/management/commands/absurd_worker.py tests/test_scheduler.py
git commit -m "feat: absurd_worker --beat runs the scheduler in the worker loop"
```

---

## Task 8: `E007` schedule validation checks

**Files:**

- Modify: `django_absurd/checks.py`
- Test: `tests/test_scheduler_checks.py` (create)

**Interfaces:**

- Consumes: `get_settings_schedules`, `Schedule` (Task 2); `get_absurd_backends`,
  `get_declared_queues`.
- Produces: a new `@register("absurd")` check `check_absurd_schedule_config` emitting
  `absurd.E007` per invalid `SCHEDULE` entry. No DB access.

Validation per entry: `task` imports (ImportError → E007) and is a `django.tasks.Task`
(else E007); `cron` valid (`croniter.croniter.is_valid(cron)` False → E007); only known
keys `{task, cron, queue, args, kwargs}` (unknown key → E007); `args`/`kwargs`
JSON-serializable (`json.dumps` raises → E007); `queue` (when set) in
`get_declared_queues(backend)` (else E007). `msg` states the problem; `hint` states the
fix.

- [ ] **Step 1: Write the failing test**

Create `tests/test_scheduler_checks.py`:

```python
import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


def run_check(capsys, settings, schedule):
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {"QUEUES": {"default": {}}, "SCHEDULE": schedule},
        }
    }
    try:
        call_command("check", "django_absurd")
    except SystemCheckError as exc:
        cap = capsys.readouterr()
        return cap.out + cap.err + str(exc)
    cap = capsys.readouterr()
    return cap.out + cap.err


def test_valid_schedule_no_error(capsys, settings):
    out = run_check(capsys, settings, {"ok": {"task": "tests.tasks.add", "cron": "0 2 * * *"}})
    assert "absurd.E007" not in out


def test_unimportable_task(capsys, settings):
    out = run_check(capsys, settings, {"x": {"task": "tests.tasks.nope", "cron": "0 2 * * *"}})
    assert "absurd.E007" in out
    assert "could not be imported" in out


def test_not_a_task(capsys, settings):
    out = run_check(capsys, settings, {"x": {"task": "tests.tasks.Payload", "cron": "0 2 * * *"}})
    assert "absurd.E007" in out
    assert "is not a Django task" in out


def test_bad_cron(capsys, settings):
    out = run_check(capsys, settings, {"x": {"task": "tests.tasks.add", "cron": "not-cron"}})
    assert "absurd.E007" in out
    assert "invalid cron expression" in out


def test_unknown_key(capsys, settings):
    out = run_check(capsys, settings, {"x": {"task": "tests.tasks.add", "cron": "0 2 * * *", "bogus": 1}})
    assert "absurd.E007" in out
    assert "unknown key 'bogus'" in out


def test_non_serializable_args(capsys, settings):
    out = run_check(capsys, settings, {"x": {"task": "tests.tasks.add", "cron": "0 2 * * *", "args": [object()]}})
    assert "absurd.E007" in out
    assert "not JSON-serializable" in out


def test_undeclared_queue(capsys, settings):
    out = run_check(capsys, settings, {"x": {"task": "tests.tasks.add", "cron": "0 2 * * *", "queue": "ghost"}})
    assert "absurd.E007" in out
    assert "queue 'ghost' is not declared" in out
```

(`tests.tasks.Payload` import path: use any importable non-Task symbol; if `Payload`
isn't importable there, point at `tests.models.Payload`. Confirm while implementing.)

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_scheduler_checks.py -v` Expected: failures —
`absurd.E007` never present (check not implemented).

- [ ] **Step 3: Implement (prose)**

Add `E007` message/hint constants to `checks.py` following the existing
`Exxx_MSG`/`Exxx_HINT` style (prefix `"django-absurd: "`; the per-entry detail strings
must contain the exact substrings the tests assert: `could not be imported`,
`is not a Django task`, `invalid cron expression`, `unknown key '<k>'`,
`not JSON-serializable`, `queue '<q>' is not declared`). Add
`check_absurd_schedule_config(*, app_configs, **kwargs)` decorated
`@register("absurd")`: iterate `get_absurd_backends().values()`; for each,
`get_settings_schedules(backend)` and the declared-queue set
`get_declared_queues(backend)`; validate each `Schedule` per the rules above, appending
one `Error(..., id="absurd.E007")` per problem. Put a
`validate_schedule(schedule, declared_queues) -> list[CheckMessage]` helper BELOW the
check (verb name, no leading underscore). Reading settings only — no DB — so it runs
pre-migrate. Import `json`, `croniter`, `Task` (`from django.tasks import Task`).

- [ ] **Step 4: Run, verify it passes**

Run: `uv run pytest tests/test_scheduler_checks.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add django_absurd/checks.py tests/test_scheduler_checks.py
git commit -m "feat: absurd.E007 system checks for SCHEDULE entries"
```

---

## Task 9: User docs (AGENTS.md + README)

**Files:**

- Modify: `django_absurd/AGENTS.md`
- Modify: `README.md`

**Interfaces:** none (docs).

Use the `sync-docs` skill's audience map. The Zensical site lives on the unmerged
`docs-site` branch (PR #30) — its scheduling page is a follow-up once that merges; do
NOT create `docs/guide/` here.

- [ ] **Step 1: AGENTS.md — add a "Scheduling recurring tasks" section**

Document: `OPTIONS["SCHEDULE"]` entry shape (`task`, `cron`, `queue?`, `args?`,
`kwargs?`); cron is 5-field, interpreted in Django `TIME_ZONE`; `OPTIONS["SCHEDULER"]`
default `"beat"` (pg_cron is a future opt-in); run `python manage.py absurd_beat` (or
`absurd_worker --beat`); **run exactly one beat process** (concurrent beats
double-fire); fire-forward-only (no backfill); validation surfaces as `absurd.E007` from
`manage.py check`. Link the Absurd cron pattern
(`https://earendil-works.github.io/absurd/patterns/cron/`) and Django Tasks docs per the
docs-cross-link convention.

- [ ] **Step 2: README.md — one-line mention + link**

In the existing Documentation/feature area, add a single line that recurring tasks are
supported via `absurd_beat`, linking to the AGENTS.md section. Keep README trim — no
expansion beyond the line.

- [ ] **Step 3: Verify wording matches code**

Cross-check command name (`absurd_beat`), flag (`--beat`), setting keys (`SCHEDULE`,
`SCHEDULER`), and the check id (`absurd.E007`) against the implemented code verbatim.

- [ ] **Step 4: Commit**

```bash
git add django_absurd/AGENTS.md README.md
git commit -m "docs: document settings-declared scheduled tasks (beat)"
```

---

## Notes for the executor

- Run the suite with Postgres up: `docker compose up -d db` (host `PGPORT` per
  `.envrc`). Single-DB suite: `uv run pytest`. Don't run `tests/multidb` for this work.
- After all tasks: full `uv run pytest` green + `uvx --with tox-uv tox` (matrix + mypy)
  before the branch wrap-up.
- Branch: `scheduler` (already cut from `origin/main`). Avoid `git add -A` — the
  untracked Zensical `site/` build is not gitignored on this branch.
