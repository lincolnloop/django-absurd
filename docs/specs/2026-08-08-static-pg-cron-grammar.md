# Validate pg_cron schedule grammar in Python

Reframes [#66](https://github.com/lincolnloop/django-absurd/issues/66). That issue asked
for a check-time grammar check. The stronger reason emerged from measuring: the database
probe we have today **cannot detect the expressions pg_cron silently truncates**, so a
static matcher is not merely cheaper — it is more correct.

## Problem

`ScheduledTask.clean()` validates its cron by calling `probe_cron_grammar`: schedule a
throwaway `_dj:__probe__:<uuid>` job on the central `cron.database_name` database, then
unschedule it. Three costs:

- Validating a string requires the central database reachable and the role holding
  schedule/unschedule rights. `full_clean()` therefore cannot run in a plain unit test,
  a data migration, or any environment without that wiring — it is gated inert under
  tests.
- It writes to a cluster-shared catalog to answer a pure question.
- It couples models → validators → catalog → central connection. That coupling already
  forced `TextChoices` into their own module to break an import cycle.

And the defect the probe cannot see: **pg_cron accepts expressions it then misreads.**

## Measured, pg_cron 1.6 (`postgresql-18-cron`, the pinned image)

| expression                                                         | pg_cron                         |
| ------------------------------------------------------------------ | ------------------------------- |
| `0 2 * * *`, `*/5 * * * *`, `1-5 * * * *`, `1,3,5 * * * *`         | accept                          |
| `0 0 * JAN *`, `0 0 * * MON`, `0 0 * * mon`, `0 0 * * 7`           | accept                          |
| `30 seconds`, `1 seconds`, `59 seconds`, `30 second`, `30 SECONDS` | accept                          |
| `0 seconds`, `60 seconds`                                          | **reject**                      |
| `@daily`, `@hourly`                                                | accept                          |
| `0 2 * * * *` (six fields)                                         | **accept — silently truncated** |
| `0 2 * * 5#2`                                                      | **accept — silently truncated** |
| `0 2 * *` (four fields), `0 2 * * ?`, `0 2 L * *`                  | reject                          |
| `  0   2 * * *  ` (padded)                                         | accept                          |

Why the two silent cases, from source:

- `src/entry.c` is the vendored Vixie parser. Its syntax is
  `minutes hours doms months dows` **`cmd`** — it reads five fields and treats the rest
  as command text. A sixth field is swallowed, not validated: `0 2 * * * *` schedules
  `0 2 * * *`. `#` is not implemented at all, so `5#2` parses `5` and swallows `#2` —
  every Friday, not the second Friday.
- `src/job_metadata.c::TryParseInterval` handles the interval form with
  `sscanf(" %u secon%c%c %c", …)` over a lowercased copy, enforcing `0 < n < 60` and
  rejecting trailing text. The regex below is a port of that, not a guess.

The `@` aliases are genuinely implemented in `entry.c` (`@reboot`, `@restart`,
`@yearly`, `@annually`, `@monthly`, `@weekly`, `@daily`, `@midnight`, `@hourly`).

## Decisions

- **The static matcher replaces the probe.** `probe_cron_grammar` and its
  central-database write go away; `clean()` becomes pure.
- **Deliberate divergence: reject what pg_cron silently truncates.** Exactly five
  fields, and no `#`/`L`/`W`/`?`/`R`/`H`. `L` and `?` pg_cron rejects anyway; six-field
  and `#` it accepts and misreads, which is the case worth protecting a user from.
  Diverging on acceptance is the point of the change, not a side effect.
- **`@reboot` and `@restart` are rejected** despite being implemented: neither expresses
  a recurring schedule, and a task scheduled for "restart" would never fire on cadence.
- **Split of labour.** Hand-rolled regex and sets decide the SHAPE (interval form, alias
  set, field count, forbidden tokens) — the parts where we must be stricter than
  croniter. croniter decides FIELD CONTENTS (ranges, steps, lists, month/day names,
  per-field bounds) — already a runtime dependency for beat, and reimplementing its
  field parser would be the actual mistake.
- **The settings lane gains the same validation.** Once validation is pure, the DB-free
  `manage.py check` can validate cron grammar for `pg_cron` schedules, which it skips
  today — closing #66 as filed. Both lanes then enforce one rule from one callable.

## Accepted consequences

- **Some invalid expressions still reach sync.** croniter is more permissive than Vixie
  in corners our token filter does not cover, so those keep surfacing as pg_cron's own
  error at sync — exactly today's behavior for settings-lane schedules. No regression.
- **Grammar drift.** We now own an approximation of pg_cron's grammar. Mitigation is the
  existing re-verify trigger on the `postgresql-*-cron` pin (see the pg_cron reference
  in `.claude/skills/pg-cron`), plus the asymmetry: drift mostly produces false accepts,
  which degrade to sync-time errors rather than blocking a valid schedule.
- **Save-time rejection is lost for the corners the matcher cannot refute.** Acceptable:
  the settings lane has never had save-time rejection at all.

## Tests

Table-driven from the measured data above, parametrized over both real entrypoints — the
system check and `full_clean()` — per this project's validator-testing convention, never
re-asserting the same rule per entrypoint. Cases must include each silent-truncation
expression (`0 2 * * * *`, `0 2 * * 5#2`) asserting we now reject, the interval
boundaries (`0`, `1`, `59`, `60`, singular, uppercase, padded), the alias set including
the two rejected ones, and the field-content forms croniter owns (`*/5`, `1-5`, `1,3,5`,
`JAN`, `mon`, `7`).

No test asserts "no `cron.*` write happens during `full_clean()`", and none is written:
a probe on a valid expression schedules and unschedules, and on an invalid one never
inserts, so a job-count assertion cannot tell probe-present from probe-absent, and this
suite forbids monkeypatching. The rule tables are the guard instead — their subjects
never opt pg_cron in, and four of their cases are ones the extension ACCEPTS, so any
implementation that asked the database (inert or not) fails them. The residual gap,
stated rather than claimed away: a redundant probe alongside the matcher would change no
outcome and no test would notice.

## Follow-ups, not in scope

- The pg_cron reference skill states "No 6-field seconds form" and `AGENTS.md` describes
  the grammar as "a 5-field cron or the interval form". Both are incomplete — the
  aliases work, and six fields are accepted-then-truncated. Correct them where the docs
  live.
- Silent truncation is arguably an upstream bug worth reporting.
