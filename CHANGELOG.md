# Changelog

Notable changes to django-absurd, for the people who depend on it.

Generated with [git-cliff](https://git-cliff.org) from the conventional-commit history
and then hand-edited at release time, so regenerating a release that has already shipped
will discard those edits — re-apply them, or render only the unreleased range and paste
it on top.

## Unreleased

### Breaking changes

- Regenerate the schema as a single 0.5.0 install
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
- **test:** Dj_absurd fixture — freeze durable time, drain, emit, read
  ([#134](https://github.com/lincolnloop/django-absurd/pull/134))

### Documentation

- Fold two config corrections into the backfill task
- Correct why three alpha releases render no section
- Use chore for the plan's own tooling commits
- Plan the git-cliff changelog implementation
- Keep floor changes visible, hand-fix the backfill
- Drop Renovate commits from the changelog design
- Rewrite the documentation site as an example-first how-to
  ([#161](https://github.com/lincolnloop/django-absurd/pull/161))
- Capture worker reasoning, retire consumed specs
  ([#158](https://github.com/lincolnloop/django-absurd/pull/158))
- Stop naming a pg_cron cleanup job that does not exist
  ([#141](https://github.com/lincolnloop/django-absurd/pull/141))
- Trim agent memory, capture why, retire consumed specs
  ([#139](https://github.com/lincolnloop/django-absurd/pull/139))
- Post-#107 sync + archive consumed pg_cron spec/plan
  ([#111](https://github.com/lincolnloop/django-absurd/pull/111))
- Favour uv install with prerelease flag
  ([#105](https://github.com/lincolnloop/django-absurd/pull/105))

## [0.1.0a5](https://github.com/lincolnloop/django-absurd/compare/v0.1.0a4...v0.1.0a5) - 2026-07-23

### Features

- Automatic Absurd test-state cleanup (Django parity)
  ([#97](https://github.com/lincolnloop/django-absurd/pull/97))
- **pg_cron:** Admin-writable pg_cron schedules (Phase B)
  ([#52](https://github.com/lincolnloop/django-absurd/pull/52))
- **pg_cron:** Extract shared schedule validators, model-first enforcement (Phase A)
  ([#49](https://github.com/lincolnloop/django-absurd/pull/49))
- **pg_cron:** Read-only ScheduledTask admin
  ([#44](https://github.com/lincolnloop/django-absurd/pull/44))
  ([#46](https://github.com/lincolnloop/django-absurd/pull/46))
- Scheduled (recurring) tasks via a beat scheduler
  ([#31](https://github.com/lincolnloop/django-absurd/pull/31))

### Bug fixes

- **scheduler:** Treat 6-field cron seconds as leading, not trailing
  ([#40](https://github.com/lincolnloop/django-absurd/pull/40))

### Documentation

- Dream — capture admin/ORM + admin-lane why, retire 18 shipped specs/plans
  ([#47](https://github.com/lincolnloop/django-absurd/pull/47))
- Add Zensical documentation site
  ([#30](https://github.com/lincolnloop/django-absurd/pull/30))

## [0.1.0a1](https://github.com/lincolnloop/django-absurd/releases/tag/v0.1.0a1) - 2026-06-23

### Requirements

- Mirror Renovate cooloff via uv exclude-newer (7 days)
