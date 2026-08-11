# Changelog

Notable changes to django-absurd, for the people who depend on it.

Generated with [git-cliff](https://git-cliff.org) from the conventional-commit history
and then hand-edited at release time, so regenerating a release that has already shipped
will discard those edits — re-apply them, or render only the unreleased range and paste
it on top.

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
