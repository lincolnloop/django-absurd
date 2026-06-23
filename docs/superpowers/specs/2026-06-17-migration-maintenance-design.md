# django-absurd — Spec (deferred): Migration maintenance

Date: 2026-06-17 Status: intention-only (not yet brainstormed/planned)

Pulled OUT of spec 1 (migration wrapping) to keep that focused on getting the initial
migration running. This file saves the intention; brainstorm + plan it on its own when
spec 1 ships.

## Intent

Automate keeping django-absurd's migration graph in step with Absurd releases — all
offline, sourced only from the installed pinned `absurdctl` wheel (no network, ever).

## Scope (when built)

- **Codegen** (`gen_migrations`, maintainer-only, NOT shipped): append per-release delta
  migrations via `absurdctl migrate --from <prev> --to <next> --dump-sql` (offline,
  bundled deltas). One migration per Absurd release; `0001` (the bootstrap from spec 1)
  is frozen. Naming `{seq}_absurd_{ver}`. Each delta stamps its target version; set
  `atomic = False` when the SQL is non-transactional (`concurrently`).
- **Drift tests** (offline): sql↔migration bijection; head version == highest
  migration; upstream check
  `ABSURD_SCHEMA_VERSION == absurdctl.ABSURD_SCHEMA_TARGET_VERSION`.
- **SDK floor automation:** when head bumps, gen rewrites the `absurd-sdk` constraint
  floor in `pyproject.toml` (`>=<head>,<next-minor>`); a test asserts floor ==
  `ABSURD_SCHEMA_VERSION`.
- **Upgrade loop:** Renovate bumps the `absurdctl==` pin → upstream/drift test fails →
  maintainer runs gen → commits deltas + bumped version/floor → tests gate → release.

## Deferred follow-ups (from spec 1 build)

- **tox matrix for Django floor + ceiling.** `uv.lock` resolves `Django>=5.2` to the
  latest (6.0.x) in dev, so CI tests only the ceiling. Add tox (or equivalent) to run
  the suite against both the floor (Django 5.2 LTS) and the current ceiling, so the
  declared `Django>=5.2` range is actually exercised. Wire into CI.

## Open questions for its brainstorm

- Whether codegen is a script vs a (maintainer-only) entrypoint, and how to keep it out
  of the wheel.
- Exactly how `--from/--to` walks intermediate released versions from
  `absurdctl.BUNDLED_MIGRATIONS`.
- Squashing policy if the delta chain grows long.
