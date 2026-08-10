---
name: bump-absurd-version
description:
  Use when moving django-absurd to a new upstream Absurd release — a Renovate PR bumping
  the `absurdctl` pin, an `absurd-sdk` floor change, `ABSURD_SCHEMA_VERSION` drift, or a
  report that `manage.py migrate` ships an older schema than the pinned version.
---

# bump-absurd-version

## Overview

Absurd's schema ships as ordinary Django migrations, generated offline from the pinned
`absurdctl` wheel — never hand-written, never fetched at migrate time. A version bump is
therefore codegen plus a set of artifacts that must move together, and the generated SQL
itself tells you what else changed.

**Two ways to generate, and the choice comes first:**

|            | Add a delta                                        | Regenerate from scratch                                                                                          |
| ---------- | -------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| Produces   | `000N_absurd_<version>` on top                     | ONE `0001_initial_<version>` replacing every migration                                                           |
| Source     | `absurdctl migrate --from … --to … --dump-sql`     | `absurdctl.BUNDLED_SCHEMA_SQL`                                                                                   |
| Costs      | replays history the current version has moved past | EVERY existing database, which must drop the `absurd` schema, clear its `django_migrations` rows, and re-migrate |
| Right when | **the default, and always safe**                   | ONLY when the maintainer says so in this session                                                                 |

**Adding a delta is the default. Never regenerate unless the maintainer says so in this
session** — "the project still looks pre-release" is not their decision made for you.
The reason it was chosen once, at 0.5.0: replaying history reintroduces what upstream
has since removed, because the earliest schema created `uuid-ossp`, so a delta chain
creates an extension only to drop it again and demands privileges the package no longer
needs. That argument expires the day a real installed base exists.

Regenerating also breaks harder than "re-migrate" suggests: with `django_absurd.pg_cron`
installed, its own migration is recorded as applied while its parent is now a migration
that is not, so **every** `migrate` in the project raises `InconsistentMigrationHistory`
until `django_migrations` is edited by hand. Say so wherever the change is announced.

**With deltas: one migration per Absurd release, and the initial is frozen.**

## When to use

- A Renovate PR bumps `absurdctl==` in `pyproject.toml`
- Upstream announces a release and `ABSURD_SCHEMA_VERSION` no longer matches it
- Anything reports a schema version mismatch between the SDK, the pin, and the database

Not for: a Renovate bump of any other dependency, or an `absurd-sdk` patch release with
no schema change (check the delta is empty first — step 2 tells you).

## The artifacts that move together

Miss one and the failure is silent or lands in CI, not here:

| Artifact                                   | Where                                                                                                                                                                                                          | Miss it and                                                                                                                                         |
| ------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `absurdctl` pin                            | `pyproject.toml` dev deps                                                                                                                                                                                      | you generate the delta from the OLD bundled SQL                                                                                                     |
| `absurd-sdk` floor                         | `pyproject.toml` dependencies                                                                                                                                                                                  | the SDK and schema can drift apart at install time                                                                                                  |
| Delta SQL + wrapper                        | `django_absurd/migrations/`                                                                                                                                                                                    | nothing ships                                                                                                                                       |
| `ABSURD_SCHEMA_VERSION`                    | `django_absurd/__init__.py`                                                                                                                                                                                    | adopting-an-existing-DB guidance lies                                                                                                               |
| Root lockfile                              | `uv.lock`                                                                                                                                                                                                      | `--locked` fails in CI                                                                                                                              |
| **Example lockfiles**                      | `examples/*/uv.lock` (three)                                                                                                                                                                                   | **`uv sync --locked` fails all three example jobs**                                                                                                 |
| SDK bounds in comments                     | anything naming an SDK range                                                                                                                                                                                   | a stale bound reads as a verified one                                                                                                               |
| Admin surfaces reading Absurd's own tables | `admin_views.py`, its entity specs, the admin tests                                                                                                                                                            | a renamed or added column 500s the admin at runtime and no migration test notices                                                                   |
| Anything naming the migration by filename  | its dependency in `django_absurd/pg_cron/migrations/0001_initial.py`, the scratch-schema loader in `tests/pg_cron/utils.py`, line-number citations in `admin_views.py` and `tests/core/test_admin/test_run.py` | a renamed initial breaks the migration graph, and the pg_cron suite fails in SETUP (455 errors, `NodeNotFoundError`) — nothing points at the rename |

Sweep for those with `grep -rn`, not `ag`: in a worktree checkout `ag` silently missed
two of the four above.

The example lockfiles embed the root package's `requires-dist`, because each example
depends on django-absurd by path. Any root dependency change invalidates all three. Run
`uv lock --project examples/<name>` for each.

## Procedure

1. **Bump the `absurdctl` pin first**, then `uv sync`. The SQL comes from the wheel, so
   the old pin can only generate the old version.

2. **Generate the SQL, offline.** To regenerate from scratch, read the install SQL out
   of the wheel and delete every existing migration:

   ```bash
   uv run python -c "
   import absurdctl, pathlib
   pathlib.Path('django_absurd/migrations/0001_initial_<version>.sql').write_text(
       absurdctl.BUNDLED_SCHEMA_SQL
   )
   print(absurdctl.ABSURD_SCHEMA_TARGET_VERSION)"
   ```

   That constant IS the fresh install (`absurd.sql`), bundled — the printed target
   version must be the one you are moving to. **`migrate --dump-sql` cannot produce a
   fresh install:** it only emits ranges, `--from` is mandatory, and `--from 0.0.0`
   silently starts at an early delta with no schema bootstrap at all. Carry the
   initial's other operations over when rewriting its wrapper — the psycopg check, and
   the `Queue` state-only model, which mirrors `absurd.queues` and must be updated
   together with `django_absurd/models.py` if the release touched that table.

   For a delta instead:

   ```bash
   absurdctl migrate --from <current> --to <new> --dump-sql \
     > django_absurd/migrations/000N_absurd_<new_with_underscores>.sql
   ```

   `--dump-sql` needs no database and makes no network call — verify it printed SQL and
   exited 0. An empty or header-only bundle means there is no schema change: stop, and
   bump only the pins.

3. **Read the delta's own comments before writing any wrapper.** Upstream states what a
   release changes at the top of each bundled migration, and that is your list of
   downstream work. **Regenerating leaves you no delta to read** — the install SQL's
   header describes the whole system, not the release — so generate one into the
   scratchpad anyway, purely to read its comments, then discard it. A release that drops
   an extension dependency, changes a function signature, or renames a column has
   consequences in this package's code and in user docs that no test will find for you.

4. **Wrap it** as `000N_absurd_<version>.py`, mirroring `0001`: read the `.sql` with
   `importlib.resources`, depend on the previous migration.
   - **No `reverse_sql`.** `absurdctl` refuses downgrades, so there is nothing to
     generate; a hand-written reverse is unverifiable and rots, and `RunSQL.noop` lets a
     rollback claim success while leaving the database at the newer schema. A delta with
     no downgrade SQL is genuinely irreversible, and says so.
   - **Know what that costs.** What is irreversible is the SQL operation — but an
     unapply chain is only as reversible as its least reversible step, so one such
     operation blocks `migrate django_absurd zero` too, not just a one-step-back. That
     is accepted here: `migrate <app> zero` is stock Django, never a django-absurd
     feature, and nothing in this package may depend on it. See "Tests that need the
     schema absent" below.
   - Set `atomic = False` **only** if the delta contains non-transactional DDL. Grep for
     `concurrently` as a statement, not as a word — it appears inside error-message
     strings, and a false positive here costs you transactional safety for nothing. If
     it genuinely does contain `CONCURRENTLY`, do not flip the whole migration: split
     the SQL so only the non-transactional part is non-atomic, and ask the maintainer.
     No release has needed this yet — treat it as unexercised ground.

5. **Update the remaining artifacts** from the table above.

6. **Verify against a real database, rebuilt:**

   ```bash
   uv run pytest tests/core tests/pg_cron tests/multidb --create-db -q --no-cov
   ```

   `--create-db` is not optional here. Every suite bakes in `--reuse-db`, so without it
   a schema change never reaches the test database and green means nothing. With it,
   `tests/core/test_migrations.py` IS the from-empty check: the version the database
   reports equals `ABSURD_SCHEMA_VERSION`, and no extension was created. A file left
   over from an older pin fails that version assertion; a hand-edit that keeps
   `get_schema_version()` intact is caught by nobody, which is why the red flags forbid
   touching the SQL at all. Do not hand-roll a `migrate` against the dev database
   instead — it is not empty on the second bump, and `create schema if not exists` sails
   straight over a half-applied leftover.

   Set `ABSURD_SCHEMA_VERSION` from the version upstream ANNOUNCES, never from
   `absurdctl.ABSURD_SCHEMA_TARGET_VERSION` — taking both sides from the same wheel
   makes that assertion compare the wheel to itself, so a stale pin passes.

   **Then sweep for the old filename** if anything was renamed:
   `grep -rn '<old stem>' .` from the repo root (e.g. `0001_initial_0_4_0`), repeated
   until it returns nothing. Not `ag`, which silently missed two of four in a worktree
   checkout. A stale name in another app's migration dependency fails every `migrate`
   with `NodeNotFoundError`, surfacing as hundreds of SETUP errors that name neither the
   rename nor the file.

7. **Run `sync-docs`.** A schema change is a user-facing change: privileges, extensions,
   and supported syntax all live in `AGENTS.md` and `docs/web/`.

   Do not document stock Django behaviour as a django-absurd feature while you are in
   there. `migrate <app> zero` is Django's, not ours; describing it as a teardown
   feature is what made an irreversible delta look like a regression rather than a
   property of having no downgrade SQL.

## Tests that need the schema absent

Express absence by RENAMING the schema, not by unapplying migrations — one rename each
way, no DDL replay, and no migration state involved. `tests.utils.hide_absurd_schema` is
that context manager:

```python
with utils.hide_absurd_schema():
    ...  # assert the schema-absent behaviour
```

`flush_absurd_state(drop_schema=True)` is NOT this — it drops each queue's tables, not
the `absurd` schema.

Two neighbouring cases are different, and conflating them is what previously made
`reverse_sql` look load-bearing:

- **"Watch `migrate` provision from scratch"** needs no absence at all. `post_migrate`
  fires unconditionally on every `migrate`, so drop the queues, run `migrate`, and
  assert the end state.
- **"Unapplying is refused"** is a real contract test of our own migration: assert that
  unapplying raises, rather than arranging a teardown.

## Common mistakes

| Mistake                                                              | Why it happens                                                       | Do instead                                                                                                                                                                                         |
| -------------------------------------------------------------------- | -------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Hand-writing a reverse migration                                     | `absurdctl` refuses downgrades, so it looks like your job            | Omit `reverse_sql` — see step 4                                                                                                                                                                    |
| Adding `reverse_sql` to a DELTA so a test can reach zero             | tests once used migrate-zero to mean "schema absent"                 | Rename the schema instead — see above. The initial legitimately has a reverse; a delta never does                                                                                                  |
| Committing without the example lockfiles                             | `git status` shows them, `git commit -am` on named paths misses them | `uv lock --project examples/<name>` ×3, and stage them                                                                                                                                             |
| Trusting a `concurrently` grep                                       | The word appears in error strings                                    | Read the match before setting `atomic = False`                                                                                                                                                     |
| Reaching for a one-off `--exclude-newer-package` on the command line | The release looks blocked by a cooloff                               | It is not: Absurd is exempt from both cooloffs, in `pyproject.toml` and `renovate.json`. A command-line override instead records a dated one in the lockfile, which fails `uv sync --locked` in CI |
| Treating it as dependency-only                                       | Nothing obviously breaks                                             | Read the delta's comments (step 3) and run `sync-docs`                                                                                                                                             |
| Believing green tests mean it applied                                | The suite reuses a database that may predate the delta               | Do step 6's from-empty check                                                                                                                                                                       |

## Red flags

- You are typing SQL by hand rather than extracting it from the wheel
- You are copying function bodies out of the initial migration for any reason
- You edited the initial migration while on the DELTA path — there it is frozen
- You are regenerating without the maintainer having said to in this session
- You are adding `reverse_sql` to a delta so a test can migrate to zero — fix the test
- `git status` shows `examples/*/uv.lock` at commit time
- You cannot say what the release changed in one sentence (you skipped step 3)
