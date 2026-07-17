# Cleanup Helper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `run_cleanup()` + an `absurd_cleanup` management command that enforce
per-queue retention (`absurd.cleanup_all_queues()`), with scheduling done by a
user-written `@task` wrapper.

**Architecture:** New `django_absurd/tasks.py` holds `run_cleanup()` — runs
`select … from absurd.cleanup_all_queues()` on the resolved Absurd DB, returns per-queue
deleted counts as `list[dict]`. The `absurd_cleanup` command wraps it for on-demand
runs. No shipped `@task` (would import-bind to a fixed backend+queue); users register
their own wrapper for `SCHEDULE`.

**Tech Stack:** Django 6.0, psycopg3, Absurd SDK, pytest (function-based, real
Postgres).

## Global Constraints

- Django 6.0 / Python 3.12 floor.
- `import typing as t` — never `from typing import X`. Absolute imports only.
- Functions contain a verb; no leading-underscore module constants/helpers; helpers
  below their public callers.
- Tests: pytest function-based only; no `unittest.mock` / monkeypatch; behavioral,
  through real entrypoints (public function, command, worker).
- Assert the COMPLETE emitted message text, never a fragment.
- Full patch coverage (100% statement+branch on added/changed lines).
- Schema-absent error message, verbatim:
  `Absurd schema is not installed. Run: manage.py migrate` (raised as
  `ImproperlyConfigured`).
- No-backend command message, verbatim: `No Absurd task backends configured.`
- Core suite needs the `db` compose service up (`docker compose up -d db`). Run:
  `uv run pytest tests/core`. `--create-db` only after migration changes (none here).

---

## File Structure

- `django_absurd/tasks.py` (create) — `run_cleanup() -> list[dict]`; module constant
  `CLEANUP_COLUMNS`.
- `django_absurd/management/commands/absurd_cleanup.py` (create) —
  `Command(BaseCommand)`; on-demand entrypoint.
- `tests/core/test_cleanup.py` (create) — behavioral tests for the function + command.
- `tests/tasks.py` (modify) — add a `cleanup_wrapper` `@task` proving the documented
  scheduling path.
- `django_absurd/AGENTS.md` + `docs/web/scheduling.md` (modify) — document the command +
  the user-wrapper pattern.

---

## Task 1: `run_cleanup()` function

**Files:**

- Create: `django_absurd/tasks.py`
- Test: `tests/core/test_cleanup.py`

**Interfaces:**

- Consumes: `django_absurd.queues.resolve_absurd_database() -> str`;
  `django.db.connections`.
- Produces: `run_cleanup() -> list[dict]` — one dict per queue, keys `queue_name` (str),
  `tasks_deleted` (int), `events_deleted` (int); ordered by `queue_name`. Raises
  `ImproperlyConfigured` when the `absurd` schema is absent.

Notes for the implementer:

- `absurd.cleanup_all_queues()` reads each queue's `cleanup_ttl` / `cleanup_limit` from
  `absurd.queues`. Eligibility = a run's terminal timestamp
  (`completed_at`/`failed_at`/`cancelled_at`) `<` `now() - cleanup_ttl`; only
  `completed`/`failed`/`cancelled` tasks qualify. One call deletes at most
  `cleanup_limit` rows **per queue**.
- Test seeding uses `cleanup_ttl: "0 seconds"` so a just-completed row is immediately
  eligible (cleanup runs strictly after the worker's terminal commit, so `now()` has
  advanced past `completed_at`).
- Schema-absent detection mirrors `django_absurd/queues.py:reconcile_queue` — catch
  `django.db.utils.ProgrammingError`, re-raise `ImproperlyConfigured` with the verbatim
  message.
- Bare tasks (e.g. `add`) emit no named events, so `events_deleted` is `0`. If the RED
  run shows otherwise, that is a real finding — assert the observed complete value,
  never drop the field.

- [ ] **Step 1: Write the failing execution test**

Add to `tests/core/test_cleanup.py`:

```python
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connection

from django_absurd.tasks import run_cleanup
from tests.tasks import add

pytestmark = pytest.mark.django_db(transaction=True)

ABSURD = "django_absurd.backends.AbsurdBackend"


def sync_queue(settings, cleanup_ttl="0 seconds", cleanup_limit=1000):
    settings.TASKS = {
        "default": {
            "BACKEND": ABSURD,
            "OPTIONS": {
                "QUEUES": {
                    "default": {
                        "cleanup_ttl": cleanup_ttl,
                        "cleanup_limit": cleanup_limit,
                    }
                }
            },
        }
    }
    call_command("absurd_sync_queues")


def drain(queue="default"):
    call_command("absurd_worker", queue=queue, burst=True)


def test_run_cleanup_deletes_aged_terminal_tasks(settings):
    sync_queue(settings)
    add.enqueue(2, 3)
    drain()
    assert run_cleanup() == [
        {"queue_name": "default", "tasks_deleted": 1, "events_deleted": 0}
    ]
```

- [ ] **Step 2: Run it — verify it fails**

Run:
`uv run pytest tests/core/test_cleanup.py::test_run_cleanup_deletes_aged_terminal_tasks -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'django_absurd.tasks'`.

- [ ] **Step 3: Implement `run_cleanup()`**

Create `django_absurd/tasks.py`:

```python
from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.db.utils import ProgrammingError

from django_absurd.queues import resolve_absurd_database

CLEANUP_COLUMNS = ("queue_name", "tasks_deleted", "events_deleted")


def run_cleanup() -> list[dict]:
    using = resolve_absurd_database()
    try:
        with connections[using].cursor() as cur:
            cur.execute(
                "select queue_name, tasks_deleted, events_deleted "
                "from absurd.cleanup_all_queues()"
            )
            rows = cur.fetchall()
    except ProgrammingError as exc:
        msg = "Absurd schema is not installed. Run: manage.py migrate"
        raise ImproperlyConfigured(msg) from exc
    return [dict(zip(CLEANUP_COLUMNS, row, strict=True)) for row in rows]
```

- [ ] **Step 4: Run it — verify it passes**

Run:
`uv run pytest tests/core/test_cleanup.py::test_run_cleanup_deletes_aged_terminal_tasks -v`
Expected: PASS.

- [ ] **Step 5: Write the deletion-boundary test (non-terminal skipped)**

Add to `tests/core/test_cleanup.py`:

```python
def test_run_cleanup_skips_non_terminal_tasks(settings):
    sync_queue(settings)
    add.enqueue(2, 3)  # pending — worker not run, so not terminal
    assert run_cleanup() == [
        {"queue_name": "default", "tasks_deleted": 0, "events_deleted": 0}
    ]
    drain()  # now completed → terminal
    assert run_cleanup() == [
        {"queue_name": "default", "tasks_deleted": 1, "events_deleted": 0}
    ]
```

- [ ] **Step 6: Write the batch-limit test**

Add to `tests/core/test_cleanup.py`:

```python
def test_run_cleanup_respects_batch_limit(settings):
    sync_queue(settings, cleanup_limit=2)
    for _ in range(3):
        add.enqueue(2, 3)
    drain()
    assert run_cleanup() == [
        {"queue_name": "default", "tasks_deleted": 2, "events_deleted": 0}
    ]
    assert run_cleanup() == [
        {"queue_name": "default", "tasks_deleted": 1, "events_deleted": 0}
    ]
    assert run_cleanup() == [
        {"queue_name": "default", "tasks_deleted": 0, "events_deleted": 0}
    ]
```

- [ ] **Step 7: Write the schema-absent test**

Mirrors the drop-then-restore pattern in
`tests/core/test_enqueue.py:test_enqueue_with_absent_schema_raises_clear_error`. Add to
`tests/core/test_cleanup.py`:

```python
def test_run_cleanup_screams_when_schema_absent():
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS absurd CASCADE")
    try:
        with pytest.raises(
            ImproperlyConfigured, match="Absurd schema is not installed"
        ):
            run_cleanup()
    finally:
        call_command("migrate", "django_absurd", "zero", verbosity=0)
        call_command("migrate", verbosity=0)  # restore absurd schema
```

- [ ] **Step 8: Run the full file — all pass**

Run: `uv run pytest tests/core/test_cleanup.py -v` Expected: 4 passed.

- [ ] **Step 9: Commit**

```bash
git add django_absurd/tasks.py tests/core/test_cleanup.py
git commit -m "feat: add run_cleanup() enforcing per-queue retention"
```

---

## Task 2: `absurd_cleanup` management command

**Files:**

- Create: `django_absurd/management/commands/absurd_cleanup.py`
- Test: `tests/core/test_cleanup.py` (append)

**Interfaces:**

- Consumes: `run_cleanup()` (Task 1);
  `django_absurd.backends.get_absurd_backends() -> dict[str, AbsurdBackend]`.
- Produces: `call_command("absurd_cleanup")` — writes one
  `"<queue>: <n> tasks, <m> events deleted"` line per queue to stdout; when no Absurd
  backend is configured, writes `No Absurd task backends configured.` and returns
  without calling `run_cleanup()`.

- [ ] **Step 1: Write the failing happy-path test (complete output text)**

Append to `tests/core/test_cleanup.py`:

```python
def test_cleanup_command_reports_per_queue_counts(settings, capsys):
    sync_queue(settings)
    add.enqueue(2, 3)
    drain()
    capsys.readouterr()  # discard sync/worker output
    call_command("absurd_cleanup")
    assert capsys.readouterr().out == "default: 1 tasks, 0 events deleted\n"
```

- [ ] **Step 2: Run it — verify it fails**

Run:
`uv run pytest tests/core/test_cleanup.py::test_cleanup_command_reports_per_queue_counts -v`
Expected: FAIL — `CommandError: Unknown command: 'absurd_cleanup'`.

- [ ] **Step 3: Implement the command**

Create `django_absurd/management/commands/absurd_cleanup.py`:

```python
from django.core.management.base import BaseCommand

from django_absurd.backends import get_absurd_backends
from django_absurd.tasks import run_cleanup


class Command(BaseCommand):
    help = "Delete expired task and event history per each queue's retention policy."

    def handle(self, *args: object, **options: object) -> None:
        if not get_absurd_backends():
            self.stdout.write("No Absurd task backends configured.")
            return
        for row in run_cleanup():
            self.stdout.write(
                f"{row['queue_name']}: "
                f"{row['tasks_deleted']} tasks, {row['events_deleted']} events deleted"
            )
```

- [ ] **Step 4: Run it — verify it passes**

Run:
`uv run pytest tests/core/test_cleanup.py::test_cleanup_command_reports_per_queue_counts -v`
Expected: PASS.

- [ ] **Step 5: Write the no-backend test (complete output text)**

Append to `tests/core/test_cleanup.py`:

```python
def test_cleanup_command_reports_no_backends(settings, capsys):
    settings.TASKS = {}
    call_command("absurd_cleanup")
    assert capsys.readouterr().out == "No Absurd task backends configured.\n"
```

- [ ] **Step 6: Run the full file — all pass**

Run: `uv run pytest tests/core/test_cleanup.py -v` Expected: 6 passed.

- [ ] **Step 7: Commit**

```bash
git add django_absurd/management/commands/absurd_cleanup.py tests/core/test_cleanup.py
git commit -m "feat: add absurd_cleanup management command"
```

---

## Task 3: scheduling wrapper — proof + docs

Proves the documented `@task` wrapper path end-to-end (result stored on the run) and
documents the feature. No shipped `@task`; the wrapper lives in user code.

**Files:**

- Modify: `tests/tasks.py` (add `cleanup_wrapper`)
- Test: `tests/core/test_cleanup.py` (append)
- Modify: `django_absurd/AGENTS.md`, `docs/web/scheduling.md`

**Interfaces:**

- Consumes: `run_cleanup()` (Task 1). `cleanup_wrapper` binds to the `default` queue
  (declared in `tests/settings.py`, so it validates at import).

- [ ] **Step 1: Add the wrapper task**

Append to `tests/tasks.py`:

```python
from django_absurd.tasks import run_cleanup


@task
def cleanup_wrapper():
    return run_cleanup()
```

(Place the `run_cleanup` import with the other module imports at the top; shown here
inline for locality.)

- [ ] **Step 2: Write the failing worker-result test**

Append to `tests/core/test_cleanup.py` (add
`from tests.tasks import add, cleanup_wrapper` to the imports):

```python
def test_wrapper_task_result_is_deleted_counts(settings):
    sync_queue(settings)
    add.enqueue(2, 3)
    drain()  # one completed task now eligible
    result = cleanup_wrapper.enqueue()
    drain()
    got = cleanup_wrapper.get_result(result.id)
    assert got.return_value == [
        {"queue_name": "default", "tasks_deleted": 2, "events_deleted": 0}
    ]
```

Note: after the first `drain()`, one `add` run is terminal. Enqueuing `cleanup_wrapper`
and draining again completes it too; when `cleanup_wrapper` executes, both the `add` run
and its own predecessor state are terminal. Expected `tasks_deleted` = the count of
terminal-eligible tasks at execution time. If the RED run shows a different integer,
assert the observed complete value (this depends on how many runs are terminal when
cleanup fires) — keep the full dict, never a fragment.

- [ ] **Step 3: Run it — verify it fails then passes**

Run:
`uv run pytest tests/core/test_cleanup.py::test_wrapper_task_result_is_deleted_counts -v`
Expected: FAIL first (import error on `cleanup_wrapper`) until Step 1 lands; then adjust
the expected `tasks_deleted` to the observed value and confirm PASS.

- [ ] **Step 4: Document in `AGENTS.md`**

In `django_absurd/AGENTS.md`, add a "Cleanup / retention" subsection under the
scheduling material: explain that `run_cleanup()` enforces each queue's `cleanup_ttl` /
`cleanup_limit`; that `manage.py absurd_cleanup` runs it on demand; and that scheduling
is done by registering a one-line `@task` wrapper and putting it in `SCHEDULE`. Include:

```python
# myapp/tasks.py
from django.tasks import task
from django_absurd.tasks import run_cleanup

@task
def cleanup_queues():
    return run_cleanup()
```

```python
# settings.py
"SCHEDULE": {"absurd-cleanup": {
    "task": "myapp.tasks.cleanup_queues", "cron": "0 3 * * *"}}
```

State: retention is configured via the existing `OPTIONS["QUEUES"][<queue>]`
`cleanup_ttl` / `cleanup_limit` knobs (link the Configuration section); the wrapper's
queue (decorator or `SCHEDULE` entry) is where cleanup runs; the return value is stored
as the task result.

- [ ] **Step 5: Mirror into the docs site**

In `docs/web/scheduling.md`, add the same cleanup material at the site's altitude,
cross-linking to `configuration.md` (retention knobs) and Absurd's storage docs. Then
build to confirm:

Run: `uvx zensical build` Expected: `No issues found`.

- [ ] **Step 6: Run the full suite + commit**

Run: `uv run pytest tests/core -q` Expected: all pass (7 new tests green, no
regressions).

```bash
git add tests/tasks.py tests/core/test_cleanup.py django_absurd/AGENTS.md docs/web/scheduling.md
git commit -m "docs: document cleanup command + scheduling wrapper; test wrapper result path"
```

---

## Self-Review

**Spec coverage:**

- `run_cleanup()` (shared fn, `list[dict]`, resolved DB, direct SQL) → Task 1. ✓
- `absurd_cleanup` command (synchronous, per-queue stdout) → Task 2. ✓
- Command error handling (schema-absent `ImproperlyConfigured`; no-backend message) →
  Task 1 Step 7 + Task 2 Step 5. ✓
- Deletion boundary (terminal-timestamp, non-terminal skipped) → Task 1 Step 5. ✓
- Batch limit → Task 1 Step 6. ✓
- Config = existing knobs, nothing new → no task needed (asserted via `sync_queue`
  helper using `cleanup_ttl`/`cleanup_limit`). ✓
- Scheduling = user-wrapper `@task`; result stored → Task 3 (proof + docs). ✓
- Out of scope (partitions #61, drop-all #26, shipped-`@task`/multi-alias #63,
  ttl-override args) → no tasks; unpartitioned-only tests. ✓

**Placeholder scan:** none — every code step carries full code; commands have expected
output.

**Type consistency:** `run_cleanup() -> list[dict]` with keys
`queue_name`/`tasks_deleted`/`events_deleted` used identically in Tasks 1–3;
`CLEANUP_COLUMNS` matches the SQL select list and the dict keys; command reads those
exact keys.

---

## Deviations from this plan (as-built, 2026-07-16)

Bookkeeping — plan Tasks 1–3 landed, then a review round + follow-up work diverged:

- **Names/locations.** `django_absurd/tasks.py`→`cleanup.py`; `run_cleanup()`→
  `cleanup_queues()`; test wrapper `cleanup_wrapper`→`cleanup`; docs
  `docs/web/scheduling.md`→`cleanup.md` (+ nav).
- **Return type.** `list[dict]` + `CLEANUP_COLUMNS` tuple → `list[QueueCleanup]`
  (`TypedDict`); constant removed.
- **Schema-absent handling REMOVED.** Task 1's `ImproperlyConfigured` guard + its test
  dropped by decision (raw error bubbles).
- **Command signature.** `absurd_cleanup` gained positional `queues` (`nargs="*"`) →
  `cleanup_queues(names or None)`; `handle` stays `-> None` (Django echoes truthy
  returns).
- **Per-queue targeting ADDED** — `cleanup_queues(queues=None)`; behavioral tests
  parametrized over command + direct via a `cleanup` fixture (command path parses
  stdout).
- **`absurd_flush` ADDED** (not in this plan) — destructive drop-all-queues, Django
  `flush` UX (interactive confirm + `--noinput`); tested both ways (real `sys.stdin` +
  `--noinput`). Delivers #26's dangerous-reset half.
- **Delivers all of #26** (manual cleanup: all + per-queue; dangerous drop-all), beyond
  this plan's original single-command scope.
