# django-absurd's own lifecycle logger

Unit 2 of [#25](https://github.com/lincolnloop/django-absurd/issues/25). Unit 1
(Django's task signals) shipped in #143.

## What this is for

Two presentations of one log, per the issue: **pretty for a developer watching a worker
in a terminal** — colours and emoji — and **plain text for a log aggregator**. The
terminal is the reason to build it; the plain form is what production reads.

`django.tasks` reports the Django-visible lifecycle only, and its record is lossy by
design (see the task-signals spec). This logger reports what Absurd itself is doing, in
Absurd's own vocabulary.

**Emoji is decoration, never a substitute.** Every message reads correctly with the
emoji stripped, so the plain presentation is the same line minus decoration. No meaning
lives in a glyph.

## Layered — this unit is the cheap half

Two surfaces are cheap because they need no public API change:

- the SDK's own hooks (`before_spawn`, `wrap_task_execution`), which we pass **none** of
  today;
- the 16 log statements we already emit across 7 modules.

The expensive surface — durable primitives (steps, step replays, sleeps, event waits) —
needs a wrapper around the task context and lands in the NEXT unit, with sync/async
parity. `aget_absurd_context()` returns the raw SDK object today, so wrapping changes a
public return type; that is a decision for that unit, not this one.

## Events

| Source                    | Event                                                     | Level   |
| ------------------------- | --------------------------------------------------------- | ------- |
| `before_spawn`            | 🌱 task spawned — name, queue, max_attempts, dedup key    | DEBUG   |
| `wrap_task_execution`     | 🚀 task started — name, attempt/max                       | INFO    |
| `wrap_task_execution`     | ✅ task completed — + duration                            | INFO    |
| `wrap_task_execution`     | 😴 task suspended — a durable wait; the run will re-enter | INFO    |
| `wrap_task_execution`     | 🚫 task cancelled                                         | WARNING |
| `wrap_task_execution`     | ⚠️ run already failed elsewhere                           | WARNING |
| `wrap_task_execution`     | 💥 task failed — + attempt/max so retry-vs-final is plain | ERROR   |
| `worker.py` (existing)    | 🐘 worker started / stopped                               | INFO    |
| `scheduler.py` (existing) | 🥁 beat fired                                             | INFO    |
| `queues.py` (NEW)         | 🗃️ queues provisioned                                     | INFO    |
| `cleanup.py` (NEW)        | 🧹 cleanup ran                                            | INFO    |

Note which are new: `queues.py` and `cleanup.py` log nothing today, so provisioning and
cleanup are additions rather than retitles. The 16 existing statements live in
`worker.py` (5), `scheduler.py` (6), `deferred.py`, `dispatch.py`, `tasks.py` and the
two `pg_cron` modules.

DEBUG for the high-frequency one, INFO for lifecycle transitions, WARNING where Absurd
ended a run without the task's code failing, ERROR with a traceback for a real failure.
The failure line carries attempt and max_attempts because "attempt 2 of 5 failed" and
"final attempt failed" are different events to a reader and Django's own log cannot tell
them apart.

`wrap_task_execution` replaces `build_handler`'s current per-task logging. One seam then
covers every handler the SDK dispatches, including the `:run_after` deferred wrapper —
so a deferral waiting for its due time becomes visible with no special case, and
`deferred.py`'s own "waiting" line becomes redundant and goes.

## Two facts about the hooks that the implementation must respect

Both were verified against the installed SDK, and getting either wrong breaks task
execution rather than just logging.

1. **`before_spawn` must return the spawn options.** The SDK assigns its return value:
   `spawn_options = before_spawn(task_name, params, spawn_options)`. A hook that logs
   and returns `None` breaks every spawn. Return the options unmodified.
2. **A hook that raises fails the task.** Both hooks are invoked inside
   `_execute_task`'s `try`, so an exception from ours reaches the SDK's
   `except Exception`, which calls `fail_run` — consuming an attempt and eventually
   failing a task that was fine. Every hook body is wrapped so a logging fault can never
   do that. This is the same rule unit 1 settled for signal receivers, for the same
   reason.

There is no `ctx.spawn`: `AsyncTaskContext` exposes `step`, `begin_step`,
`complete_step`, `sleep_for`, `sleep_until`, `await_event`, `await_task_result`,
`emit_event` and `heartbeat`. A task spawning another task goes through Django's
`enqueue`, so `before_spawn` surfaces no event Django cannot see. What it adds is the
Absurd-side detail Django's line omits — the queue, the retry ceiling, the dedup key.

## Logger names

`getLogger(__name__)` per module, giving `django_absurd.worker`, `.scheduler`,
`.deferred`, `.dispatch`, `.tasks`. The parent `django_absurd` catches all of them,
satisfying the issue's "named logger so projects can route/level it themselves", while
letting a project silence just the beat.

Every existing message drops its hand-written `"django-absurd "` prefix — it duplicates
`%(name)s`, which any formatter already has.

## Presentation and where it attaches

Pretty is emoji + the sentence + a `key=value` tail. Plain is the same line without the
emoji.

`absurd_worker` and `absurd_beat` attach the handler at startup. Two independent
predicates decide what it renders, because the signals involved mean different things:

**Emoji** is on when stderr is a tty AND its encoding can represent the glyph. The
encoding half is a correctness gate, not cosmetics: an emoji a stream cannot encode
raises `UnicodeEncodeError` **inside** logging and the record is dropped — the line is
lost entirely rather than degrading. The tty half keeps decoration out of files and
aggregators, which is the plain form the issue asks for in production.

**Colour** is on when stderr is a tty AND `NO_COLOR` is unset AND `TERM` is not `dumb`.
Those last two are colour preferences and say nothing about emoji, so they do not strip
it: a developer with `NO_COLOR` set still gets 🚀 and 💥 in their terminal, without ANSI
escapes.

Neither is keyed on `DEBUG` — people run `DEBUG=True` with output piped to a file.

Nothing else attaches a handler, ever — not at import, not in `AppConfig.ready`. A web
process enqueuing a task, a test, or a task running under someone else's runner gets
whatever the project's own `LOGGING` says. A library must not fight that.

The formatter is importable, so a project that wants this presentation in its own
`LOGGING` dict can have it.

🐘 is the worker's start/stop banner only. Per-event glyphs everywhere else: a repeated
identity glyph carries no information in a scrolling log, while per-event ones make it
scannable.

## Testing

- The two hooks are wired onto both clients, and a spawn still works with `before_spawn`
  attached — the return-value contract, asserted rather than assumed.
- A hook that raises does not fail the task: force a logging fault and assert the task
  still completes and the fault is logged.
- Each hook event fires with the right level and text: `caplog` on the specific
  `django_absurd.*` logger, asserting the full message, not a substring.
- Emoji and colour are decided independently, and the cases that distinguish them are
  the point: a tty with a capable encoding gets both; redirected output gets neither;
  `NO_COLOR` and `TERM=dumb` in a tty get emoji WITHOUT colour; an encoding that cannot
  represent the glyph gets colour WITHOUT emoji. Drive both predicates with real stream
  objects rather than patching.
- Emoji is decoration: the plain rendering of a record equals the pretty rendering with
  the glyph stripped.
- No handler is attached by importing the package, by `AppConfig.ready`, or by enqueuing
  — only by the two commands.

## Docs

Only if there are knobs to turn — and there are two, both small: the logger names a
project can route or level, and the importable formatter. One short section covering
those, plus the fact that the worker commands make their own console pretty when a human
is watching. No tour of the vocabulary; the log speaks for itself.
