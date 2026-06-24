# django-absurd — Spec: lazy task discovery (SP4)

Date: 2026-06-22 Status: approved-for-planning

Simplifies the worker's task discovery. SP3 shipped a SCAN:
`autodiscover_modules("tasks")`

- walking each installed app's `<app>.tasks` for `Task` instances, then pre-registering
  a handler per task. This couples the worker to a "tasks must live in `tasks.py`"
  contract, needs a zero-tasks startup guard, and is ~3 functions of machinery. SP4
  replaces it with **lazy resolution by `module_path`** — the pattern Django's reference
  `db_worker` uses.

## Why

Django keeps NO task registry (verified — `@task` returns a `Task` bound to a
module-level name; nothing global to enumerate). A claimed task's name IS the dotted
import path of its function. So the worker can resolve each task on demand with
`import_string(task_name)` — no scanning, and tasks run wherever they live (including
single-file / nanodjango apps). Validated by spike: a lazy registry ran a task by
`module_path` with no pre-scan, and an unknown name deferred (not failed).

## From `@task` to execution (the `module_path` contract)

The whole pipeline is keyed on the task's dotted import path. There is no registry to
consult — the _name_ is the locator:

1. **Define:** `@task def foo(...)` in e.g. `myapp/tasks.py`. The decorator returns a
   `Task` bound to that module-level name, so `task.module_path == "myapp.tasks.foo"`
   and `task.func == foo`.
2. **Enqueue (SP2):** `foo.enqueue(2, 3)` → `AbsurdBackend.enqueue` →
   `client.spawn(task.module_path, {"args": [2, 3], "kwargs": {}})`. The name persisted
   in Absurd is the string `"myapp.tasks.foo"`.
3. **Claim:** the worker claims the row; `task["task_name"] == "myapp.tasks.foo"`.
4. **Resolve (this spec):** `LazyTaskRegistry.get("myapp.tasks.foo")` →
   `import_string("myapp.tasks.foo")`. Because step 1 bound the `Task` to that exact
   module-level name, `import_string` returns **that same `Task` object** — no scan.
   Then `build_handler(task)` wraps it.
5. **Run:** the handler calls `task.func(*args, **kwargs)` (via the `takes_context`
   bridge when the task takes context).

So `import_string(module_path)` yields the `Task` itself precisely because `@task` left
it at the module-level name the producer spawned under. (Spike-confirmed:
`import_string("tests.tasks.make_group")` returned the task and it ran.)

## Mechanism: `LazyTaskRegistry`

The Absurd SDK dispatches via `client._registry`: `_execute_task` (burst) and
`start_worker` (blocking) both read it via `self._registry.get(task_name)`; a falsy
result defers the task (`schedule_run`). So a registry whose `.get()` lazily resolves
serves BOTH modes with full concurrency and no custom loop.

`LazyTaskRegistry(dict)` (in `django_absurd/worker.py`):

```
class LazyTaskRegistry(dict):
    def __init__(self, queue):
        super().__init__()
        self.queue = queue

    def get(self, name, default=None):
        if name not in self:
            try:
                task = import_string(name)
            except ImportError:
                return default          # unknown/removed -> SDK defers
            if not isinstance(task, Task):
                return default          # not a task -> defer
            self[name] = {
                "name": name,
                "queue": self.queue,   # the worker's queue -> no SDK queue-mismatch
                "default_max_attempts": None,
                "default_cancellation": None,
                "handler": build_handler(task),
            }
        return super().get(name, default)
```

- Resolution caches per name (resolved once per worker process). Under `concurrency>1`
  several pool threads may first-resolve the same uncached name concurrently — benign
  under the GIL (idempotent import, atomic dict assignment, at worst a redundant
  `build_handler`); no lock needed.
- `queue` is the worker's queue, so the SDK's "queue mismatch" guard never trips —
  whatever name lands on this queue is run, regardless of the task's declared
  `queue_name` (routing is decided by what the producer spawned, e.g. via
  `using(queue_name=…)`).
- **Backend-alias contract (deliberate):** the registry does NOT check the resolved
  task's declared `@task` backend alias — any task whose name lands on this queue runs.
  The old scan filtered `task.backend == alias`; SP4 drops that. With the single-alias
  config (SP4 scope = `DATABASES['default']` only) this never arises. If multi-alias
  workers are added later, re-evaluate whether a worker should refuse a task bound to a
  different alias (deferred with the rest of multi-DB routing).
- **Error contract:** `ImportError` (path/attribute not found — e.g. a producer on a
  newer deploy, or a removed task) → `None` → the SDK defers (transient-skew tolerant,
  matching today's unknown-name behavior). A module that EXISTS but raises on import (a
  real bug) lets the exception propagate — loud failure, not a silent forever-defer.

## Install point

`worker_client(backend, queue)` (the contextmanager) sets
`client._registry = LazyTaskRegistry(queue)` immediately after constructing the client,
before `yield`. This is the SINGLE new SDK-internal touch — one commented
`# noqa: SLF001` (the SDK exposes no public "fallback resolver" hook). Every consumer
(burst `drain_queue`, blocking `start_worker`, and tests via `worker_client`) gets lazy
resolution from this one place.

## Deletions (the SP3 complexity removed)

- `discover_tasks`, `register_tasks`, `collect_tasks_from_module` — gone.
- `from django.utils.module_loading import autodiscover_modules` — gone.
- The zero-tasks `ImproperlyConfigured` ("no tasks registered…") — gone (nothing to
  enumerate; a name that doesn't resolve simply defers).
- The "tasks must live in `<app>/tasks.py`" contract — gone. Tasks run from any
  importable module.
- `run_worker`'s startup log line drops `tasks=<count>` (no enumeration); it still logs
  alias / queue / database / burst / concurrency.

## What stays unchanged

`build_handler` + the `takes_context` bridge (the lazy registry calls `build_handler`),
`drain_queue` (burst), `run_blocking_worker` (signals), `WorkerOptions`, the
`absurd_worker` command (alias/queue resolution, `--burst`, exit codes),
`worker_client`'s connection lifecycle + provisioning check, the two existing
`# noqa: SLF001` (`_execute_task`, `ctx._task["attempt"]`). `tests` remains an installed
app (harmless; no longer required for discovery).

## Testing (pytest, function-based, real Postgres; command-driven)

Behavioral tests still drive
`call_command("absurd_worker", queue="default", burst=True)` / `run_worker(burst=True)`
and assert DB rows + Absurd result snapshots — they pass unchanged because tasks resolve
by `module_path`.

- **Existing tests stay green:** end-to-end execute, failure recorded, `takes_context`
  attempt + real args, `using(queue_name=)` routing, observability logging, unknown-name
  defers, concurrency smoke (update it: the registry is auto-installed by
  `worker_client`, so drop the `register_tasks` call).
- **New — tasks resolve from ANY module (the point of SP4):** add a `@task` in a
  NON-`tasks.py` module (e.g. `tests/jobs.py`), enqueue it, run the worker (burst),
  assert it executed. Proves the tasks.py contract is gone.
- **New — module-that-errors-on-import propagates** (optional, if cleanly testable): a
  module whose import raises surfaces loudly rather than deferring. If awkward to test
  without a fixture module that breaks collection, document the contract and cover the
  `ImportError`-defers path instead (the unknown-name test already does).
- **Delete** `test_zero_tasks_for_alias_errors` (the guard is removed).

## Out of scope (unchanged from prior, still deferred)

Result retrieval (`get_result`), native async worker, `ALWAYS_EAGER`, idempotency keys,
`run_after`/defer, priority, connection-per-thread concurrency.
