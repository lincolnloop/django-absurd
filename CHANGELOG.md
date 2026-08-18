<!-- Prepend-only. Reword or re-wrap this header and cliff.toml's must be changed to match byte for byte, or every --prepend duplicates it.
Never `git cliff -o`: it discards every hand edit, and no regeneration can reproduce the 0.1.0a2, 0.1.0a3 and 0.1.0a4 sections — they predate conventional commits and exist only as hand-written text. -->

# Changelog

## [0.1.0a7](https://github.com/lincolnloop/django-absurd/compare/v0.1.0a6...v0.1.0a7) - 2026-08-18

**Provisioning is a deploy step.** `migrate` provisions the queues you declare, and
nothing at runtime creates one any more: enqueuing to a declared but unprovisioned queue
raises `QueueNotProvisionedError`, and `absurd_worker` refuses to start on one
([#212](https://github.com/lincolnloop/django-absurd/pull/212)). A deploy that runs
`manage.py migrate` needs no change. A release step that doesn't must run
`manage.py absurd_sync_queues`; `--check` reports what it would do and exits non-zero,
for a step that asserts rather than acts. Absurd on a non-default database alias now
needs `migrate --database=<alias>` — a `migrate` on `default` no longer provisions it as
a side effect. The new
[Deploying](https://lincolnloop.github.io/django-absurd/deploying/) page documents the
deploy step, the `--database` nuance, and what `migrate` needs.

### Breaking changes

- Provisioning is a deploy step, not a runtime seam
  ([#212](https://github.com/lincolnloop/django-absurd/pull/212)) — enqueue and
  `absurd_worker` raise `QueueNotProvisionedError` instead of creating a missing queue.
  `post_migrate` provisions only the database being migrated. `migrate` fails when
  provisioning fails for any reason other than an absent schema. `absurd_sync_queues`,
  `absurd_cleanup` and `absurd_flush` exit non-zero when no Absurd backend is
  configured.
- Translate configuration failures to CommandError in one command base
  ([#191](https://github.com/lincolnloop/django-absurd/pull/191))

### Features

- Test and support Django 6.1
  ([#181](https://github.com/lincolnloop/django-absurd/pull/181))

### Bug fixes

- Serialize provisioning with an advisory lock
  ([#202](https://github.com/lincolnloop/django-absurd/pull/202))
- Make the queue checks agree, and close the coverage gaps
  ([#197](https://github.com/lincolnloop/django-absurd/pull/197))
- Quote string values in the logfmt-style log lines
  ([#194](https://github.com/lincolnloop/django-absurd/pull/194))
- Report a malformed OPTIONS["QUEUES"] as absurd.E014
  ([#190](https://github.com/lincolnloop/django-absurd/pull/190))

### Documentation

- Give the admin its own page
  ([#189](https://github.com/lincolnloop/django-absurd/pull/189))
- Rewrite the packaged integration guide, example-first
  ([#177](https://github.com/lincolnloop/django-absurd/pull/177))

## [0.1.0a6](https://github.com/lincolnloop/django-absurd/compare/v0.1.0a5...v0.1.0a6) - 2026-08-12

**Start from an empty database.** Absurd 0.5.0 no longer depends on a UUID extension.
Carrying that forward would have meant a migration that creates the extension purely to
tear it down again, so — nothing being released yet — the migrations were reset instead,
starting clean at 0.5.0 ([#169](https://github.com/lincolnloop/django-absurd/pull/169)).
There is no upgrade path from an earlier alpha: drop your database and migrate from
scratch.

### Breaking changes

- Regenerate the schema as a clean migration starting at 0.5.0
  ([#169](https://github.com/lincolnloop/django-absurd/pull/169))
- **pg_cron:** Validate schedule grammar in Python, not by probing the database
  ([#163](https://github.com/lincolnloop/django-absurd/pull/163))
- One worker mode, with working concurrency
  ([#156](https://github.com/lincolnloop/django-absurd/pull/156))
- Replace absurd_default_params and absurd_spawn_params with absurd_params
  ([#123](https://github.com/lincolnloop/django-absurd/pull/123))

### Features

- Move to Absurd 0.5.0 ([#167](https://github.com/lincolnloop/django-absurd/pull/167))
- **pg_cron:** Report a scheduler app with no backend
  ([#164](https://github.com/lincolnloop/django-absurd/pull/164))
- Log steps, replays, sleeps and event waits
  ([#154](https://github.com/lincolnloop/django-absurd/pull/154))
- Log Absurd's own lifecycle on django_absurd loggers
  ([#146](https://github.com/lincolnloop/django-absurd/pull/146))
- Send Django's task signals from AbsurdBackend
  ([#143](https://github.com/lincolnloop/django-absurd/pull/143))
- Support run_after by deferring on a durable sleep
  ([#135](https://github.com/lincolnloop/django-absurd/pull/135))
- **test:** `dj_absurd` fixture — freeze durable time, drain, emit, read
  ([#134](https://github.com/lincolnloop/django-absurd/pull/134))
- **pg_cron:** Schedule across databases, so Absurd need not live in
  `cron.database_name` ([#107](https://github.com/lincolnloop/django-absurd/pull/107))

### Documentation

- Rewrite the documentation site as an example-first how-to
  ([#161](https://github.com/lincolnloop/django-absurd/pull/161))
- Stop naming a pg_cron cleanup job that does not exist
  ([#141](https://github.com/lincolnloop/django-absurd/pull/141))
- Favour uv install with prerelease flag
  ([#105](https://github.com/lincolnloop/django-absurd/pull/105))

## [0.1.0a5](https://github.com/lincolnloop/django-absurd/compare/v0.1.0a4...v0.1.0a5) - 2026-07-23

### Breaking changes

- The scheduler is derived from whether `django_absurd.pg_cron` is installed; the
  `OPTIONS["SCHEDULER"]` key and its `absurd.E008` check are gone
  ([#81](https://github.com/lincolnloop/django-absurd/pull/81))
- More than one Absurd backend is now a configuration error (`absurd.E004`), and the
  schedule alias is gone ([#77](https://github.com/lincolnloop/django-absurd/pull/77))
- **pg_cron:** Managed job names moved from the `absurd:` prefix to `_dj:`
  ([#76](https://github.com/lincolnloop/django-absurd/pull/76))

### Features

- Automatic Absurd test-state cleanup (Django parity)
  ([#97](https://github.com/lincolnloop/django-absurd/pull/97))
- **pg_cron:** Schedules no longer sync into test databases on migrate;
  `SYNC_SCHEDULES_ON_MIGRATE` and `SYNC_SCHEDULES_ON_TEST_DB` control it
  ([#93](https://github.com/lincolnloop/django-absurd/pull/93))
- Durable events — a task can wait on an event with `await_event`, and a top-level
  `emit_event` resumes it ([#89](https://github.com/lincolnloop/django-absurd/pull/89))
- Durable steps and sleep — tasks can checkpoint work with Absurd's `step` and suspend
  on `sleep` ([#84](https://github.com/lincolnloop/django-absurd/pull/84))
- Cleanup and retention — `cleanup_queues()`, the `absurd_cleanup` and `absurd_flush`
  commands, and a declarative `OPTIONS["CLEANUP"]` schedule
  ([#65](https://github.com/lincolnloop/django-absurd/pull/65))
- **pg_cron:** Admin-writable pg_cron schedules (Phase B)
  ([#52](https://github.com/lincolnloop/django-absurd/pull/52))
- **pg_cron:** Extract shared schedule validators, model-first enforcement (Phase A)
  ([#49](https://github.com/lincolnloop/django-absurd/pull/49))
- **pg_cron:** Read-only ScheduledTask admin
  ([#44](https://github.com/lincolnloop/django-absurd/pull/44))
  ([#46](https://github.com/lincolnloop/django-absurd/pull/46))
- **pg_cron:** Database-side recurring schedules via the opt-in `django_absurd.pg_cron`
  app ([#43](https://github.com/lincolnloop/django-absurd/pull/43))
- Scheduled (recurring) tasks via a beat scheduler
  ([#31](https://github.com/lincolnloop/django-absurd/pull/31))
- Read-only admin and ORM access to the Absurd queue tables — tasks, runs, checkpoints,
  events and waits, unioned across every queue
  ([#17](https://github.com/lincolnloop/django-absurd/pull/17))

### Bug fixes

- **scheduler:** Treat 6-field cron seconds as leading, not trailing
  ([#40](https://github.com/lincolnloop/django-absurd/pull/40))

### Documentation

- Add Zensical documentation site
  ([#30](https://github.com/lincolnloop/django-absurd/pull/30))

## [0.1.0a4](https://github.com/lincolnloop/django-absurd/compare/v0.1.0a3...v0.1.0a4) - 2026-06-24

### Features

- `absurd_worker --queue` defaults to `"default"`, so the worker runs with no flags
  ([#15](https://github.com/lincolnloop/django-absurd/pull/15))
- Declared queues are created automatically on first enqueue or worker start, so
  `absurd_sync_queues` is optional (still available for eager provisioning and policy
  reconciliation) ([#13](https://github.com/lincolnloop/django-absurd/pull/13))
- System checks trimmed — the "schema not migrated" warning is gone (an unmigrated
  schema now raises a clear runtime error) and the out-of-sync warning is narrowed to
  immutable `storage_mode` drift
  ([#13](https://github.com/lincolnloop/django-absurd/pull/13))
- Native-async worker — one `absurd_worker` runs both sync (`def`) and async
  (`async def`) tasks; async tasks run on the event loop and may use Django's async ORM,
  and `--concurrency` sizes the loop plus the sync thread pool
  ([#11](https://github.com/lincolnloop/django-absurd/pull/11))

### Documentation

- README is a quickstart; AGENTS.md is the full integration guide
  ([#13](https://github.com/lincolnloop/django-absurd/pull/13))

## [0.1.0a3](https://github.com/lincolnloop/django-absurd/compare/v0.1.0a2...v0.1.0a3) - 2026-06-24

### Documentation

- In-package agent guide, discoverable from `help(django_absurd)`, plus README coverage
  of configuration, usage and deployment
  ([#8](https://github.com/lincolnloop/django-absurd/pull/8))

## [0.1.0a2](https://github.com/lincolnloop/django-absurd/compare/v0.1.0a1...v0.1.0a2) - 2026-06-24

_No user-visible changes._

## [0.1.0a1](https://github.com/lincolnloop/django-absurd/releases/tag/v0.1.0a1) - 2026-06-23

### Features

- Initial alpha release — an Absurd task backend for Django's Tasks API, with the
  `absurd_worker` and `absurd_sync_queues` commands, queues declared in `TASKS`
  `OPTIONS`, system checks, and the Absurd schema shipped as a migration
