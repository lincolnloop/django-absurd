# django-absurd's own lifecycle logging

Unit 2 of [#25](https://github.com/lincolnloop/django-absurd/issues/25). Unit 1
(Django's task signals) shipped in #143.

## What this is for

Report what Absurd itself is doing, in Absurd's own vocabulary, on the package's own
loggers. `django.tasks` covers only the Django-visible lifecycle, and its record is
lossy by design (see the task-signals spec).

**The logger emits plain text. Always.** No emoji, no colour, no formatter of ours.
Emoji belongs to the management commands' own console output, where Django's
`self.style` already honours `--no-color` and `NO_COLOR`.

This deviates from the issue's "two presentations" framing, deliberately. Decorating log
records means an emoji the stream cannot encode raises `UnicodeEncodeError` **inside**
logging, and `StreamHandler.emit` swallows it into `handleError` — the whole line is
lost, silently. Decoration is not worth a dropped log line, and every mitigation for it
costs more than the decoration is worth.

## Layered — this unit is the cheap half

Cheap because nothing public changes:

- the SDK's own hooks (`before_spawn`, `wrap_task_execution`), which we pass **none** of
  today;
- the 16 log statements we already emit across 7 modules.

The expensive surface — durable primitives (steps, step replays, sleeps, event waits) —
needs a wrapper around the task context and lands in the NEXT unit, with sync/async
parity. `aget_absurd_context()` returns the raw SDK object today, so wrapping changes a
public return type; that is that unit's decision.

## Events

| Source                    | Event                                               | Level   |
| ------------------------- | --------------------------------------------------- | ------- |
| `before_spawn`            | task spawned — name, queue, max_attempts, dedup key | DEBUG   |
| `wrap_task_execution`     | task started — name, attempt/max                    | INFO    |
| `wrap_task_execution`     | task completed — + duration                         | INFO    |
| `wrap_task_execution`     | task suspended — a durable wait; the run re-enters  | INFO    |
| `wrap_task_execution`     | task cancelled                                      | WARNING |
| `wrap_task_execution`     | run already failed elsewhere                        | WARNING |
| `wrap_task_execution`     | task failed — + attempt/max, with traceback         | ERROR   |
| `worker.py` (existing)    | worker started / stopped                            | INFO    |
| `scheduler.py` (existing) | beat fired                                          | INFO    |
| `queues.py` (NEW)         | queues provisioned                                  | INFO    |
| `cleanup.py` (NEW)        | cleanup ran                                         | INFO    |

`queues.py` and `cleanup.py` log nothing today, so those two are additions rather than
retitles. The 16 existing statements live in `worker.py` (5), `scheduler.py` (6),
`deferred.py`, `dispatch.py`, `tasks.py` and the two `pg_cron` modules.

DEBUG for the high-frequency one, INFO for lifecycle transitions, WARNING where Absurd
ended a run without the task's code failing, ERROR with a traceback for a real failure.
The failure line carries attempt and max_attempts, because "attempt 2 of 5 failed" and
"final attempt failed" read differently and Django's own log cannot tell them apart.

`wrap_task_execution` replaces `build_handler`'s current per-task logging. One seam then
covers every handler the SDK dispatches, including the `:run_after` deferred wrapper —
so a deferral waiting for its due time becomes visible with no special case, and
`deferred.py`'s own "waiting" line becomes redundant and goes.

## Two hook contracts that break execution, not logging

Both verified against the installed SDK. Getting either wrong fails tasks.

1. **`before_spawn` must return the spawn options.** The SDK assigns its return value:
   `spawn_options = before_spawn(task_name, params, spawn_options)`. A hook that logs
   and returns `None` breaks every spawn. Return them unmodified.
2. **A hook that raises fails the task, silently.** Both hooks run inside
   `_execute_task`'s `try`, so an exception from ours reaches the SDK's
   `except Exception` → `fail_run` → the attempt is consumed and the run marked failed.
   `_execute_task` then returns normally: no re-raise, and the SDK has no logging of its
   own, so nothing reaches stderr. Our bug lands in `failure_reason` as though the
   user's task raised it. Worse, a hook raising _after_ the work succeeded means the
   body re-runs.

So every hook body is wrapped: catch `Exception`, log on `django_absurd`, continue. Same
rule as unit 1's signal containment, for a sharper reason — there, Django still logged
something.

There is no `ctx.spawn`. `AsyncTaskContext` exposes `step`, `begin_step`,
`complete_step`, `sleep_for`, `sleep_until`, `await_event`, `await_task_result`,
`emit_event` and `heartbeat`. A task spawning another task goes through Django's
`enqueue`, so `before_spawn` surfaces no event Django cannot see — what it adds is the
Absurd-side detail Django's line omits: queue, retry ceiling, dedup key.

## Logger names

`getLogger(__name__)` per module → `django_absurd.worker`, `.scheduler`, `.deferred`,
`.dispatch`, `.tasks`, plus the new `.queues` and `.cleanup`. The parent `django_absurd`
catches all of them, satisfying the issue's "named logger so projects can route/level it
themselves", while letting a project silence just the beat.

Every existing message drops its hand-written `"django-absurd "` prefix — it duplicates
`%(name)s`, which any formatter already has.

## Who attaches a handler

**One handler, in one place, with no formatter of ours.** `absurd_worker` and
`absurd_beat` attach a plain `StreamHandler` at INFO for the `django_absurd` logger, and
only when the project has configured nothing that would already catch those records.

This is not decoration — it is the difference between the feature working and being
invisible. Django's `DEFAULT_LOGGING` configures the `django` logger only;
`django_absurd` is not under it and the root logger is unconfigured, so
`logging.lastResort` applies and only WARNING and above reach stderr. Without this
handler, `absurd_worker` shows no task lines at all until the developer writes a
`LOGGING` dict.

Nothing else attaches anything, ever — not at import, not in `AppConfig.ready`. A web
process enqueuing a task, a test, or a task under someone else's runner gets whatever
the project's `LOGGING` says. A library must not fight that.

## Emoji: command output only

The commands decorate their own console writes through `self.stdout` and `self.style`,
which already respect `--no-color` and `NO_COLOR`:

- `absurd_worker` — 🐘 on the start and stop banner
- `absurd_sync_queues` — 🗃️ on what it provisioned
- `absurd_beat` — 🥁 on start

Command output is not log records, so a stream that cannot encode a glyph cannot cost us
a log line. Per-task lines stay on the logger, and stay plain.

## Testing

- Both hooks are wired onto both clients, and a spawn still works with `before_spawn`
  attached — the return-value contract, asserted rather than assumed.
- A hook that raises does not fail the task: force a logging fault, assert the task
  still completes and the fault is logged.
- Each hook event fires on the expected `django_absurd.*` logger at the expected level,
  with the full message asserted, not a substring.
- The worker command's handler makes INFO lines visible with no project `LOGGING`, and
  does not duplicate them when the project has configured its own handler.
- Importing the package, `AppConfig.ready`, and enqueuing attach no handler.
- Command output carries the emoji; log records never do.

## Docs

One short section: the logger names a project can route or level, and the fact that the
worker commands make their own lines visible by default. That is the whole knob surface
— no setting, no formatter to document.
