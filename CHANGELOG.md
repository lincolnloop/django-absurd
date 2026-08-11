# Changelog

Notable changes to django-absurd, for the people who depend on it.

Generated with [git-cliff](https://git-cliff.org) from the conventional-commit history
and then hand-edited at release time, so regenerating a release that has already shipped
will discard those edits — re-apply them, or render only the unreleased range and paste
it on top.

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

- Dream — capture admin/ORM + admin-lane why, retire 18 shipped specs/plans
  ([#47](https://github.com/lincolnloop/django-absurd/pull/47))
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
  ([#15](https://github.com/lincolnloop/django-absurd/pull/15))

## [0.1.0a3](https://github.com/lincolnloop/django-absurd/compare/v0.1.0a2...v0.1.0a3) - 2026-06-24

### Documentation

- In-package agent guide, discoverable from `help(django_absurd)`, plus README coverage
  of configuration, usage and deployment
  ([#8](https://github.com/lincolnloop/django-absurd/pull/8))

## [0.1.0a2](https://github.com/lincolnloop/django-absurd/compare/v0.1.0a1...v0.1.0a2) - 2026-06-23

_No user-visible changes._

## [0.1.0a1](https://github.com/lincolnloop/django-absurd/releases/tag/v0.1.0a1) - 2026-06-23

### Features

- Initial alpha release — an Absurd task backend for Django's Tasks API, with the
  `absurd_worker` and `absurd_sync_queues` commands, queues declared in `TASKS`
  `OPTIONS`, system checks, and the Absurd schema shipped as a migration
