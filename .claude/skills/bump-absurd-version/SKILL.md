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

**One migration per Absurd release, added on top. The initial is frozen, and the schema
is never regenerated from scratch** — that was done once, late in pre-release, and it is
not a tool on the shelf. Regenerating renames the initial and breaks every existing
database: each must drop the `absurd` schema AND clear its `django_migrations` rows,
because with `django_absurd.pg_cron` installed its migration stays recorded as applied
while its parent no longer is, so every `migrate` raises `InconsistentMigrationHistory`
until someone edits that table by hand.

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

2. **Generate the delta, offline:**

   ```bash
   absurdctl migrate --from <current> --to <new> --dump-sql \
     > django_absurd/migrations/000N_absurd_<new_with_underscores>.sql
   ```

   `--dump-sql` needs no database and makes no network call — verify it printed SQL and
   exited 0. An empty or header-only bundle means there is no schema change: stop, and
   bump only the pins. `--from` is mandatory, and the command only ever emits a RANGE —
   no flag produces a fresh install, which is one more reason the initial stays where it
   is.

3. **Read the delta's own comments before writing any wrapper.** Upstream states what a
   release changes at the top of each bundled migration, and that is your list of
   downstream work. A release that drops an extension dependency, changes a function
   signature, or renames a column has consequences in this package's code and in user
   docs that no test will find for you.

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

**The FIRST delta flips that test.** Until one exists the schema installs in a single
migration that reverses, and
`tests/core/test_migrations.py::test_reverse_drops_absurd_schema` asserts exactly that.
Adding a delta makes the chain irreversible, so that test has to become an
assert-the-refusal test in the same commit — it is on the move-together list, and
nothing else in the tree will remind you.

## Common mistakes

| Mistake                                                              | Why it happens                                                       | Do instead                                                                                                                                                                                         |
| -------------------------------------------------------------------- | -------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Hand-writing a reverse migration                                     | `absurdctl` refuses downgrades, so it looks like your job            | Omit `reverse_sql` — see step 4                                                                                                                                                                    |
| Adding `reverse_sql` to a delta so a test can reach zero             | tests once used migrate-zero to mean "schema absent"                 | Rename the schema instead — see above. The initial legitimately has a reverse; a delta never does                                                                                                  |
| Committing without the example lockfiles                             | `git status` shows them, `git commit -am` on named paths misses them | `uv lock --project examples/<name>` ×3, and stage them                                                                                                                                             |
| Trusting a `concurrently` grep                                       | The word appears in error strings                                    | Read the match before setting `atomic = False`                                                                                                                                                     |
| Reaching for a one-off `--exclude-newer-package` on the command line | The release looks blocked by a cooloff                               | It is not: Absurd is exempt from both cooloffs, in `pyproject.toml` and `renovate.json`. A command-line override instead records a dated one in the lockfile, which fails `uv sync --locked` in CI |
| Treating it as dependency-only                                       | Nothing obviously breaks                                             | Read the delta's comments (step 3) and run `sync-docs`                                                                                                                                             |
| Believing green tests mean it applied                                | The suite reuses a database that may predate the delta               | Do step 6's from-empty check                                                                                                                                                                       |

## Red flags

- You are typing SQL by hand rather than extracting it from the wheel
- You are copying function bodies out of the initial migration for any reason
- You edited or renamed the initial migration — it is frozen; add a delta instead
- You are adding `reverse_sql` to a delta so a test can migrate to zero — fix the test
- `git status` shows `examples/*/uv.lock` at commit time
- You cannot say what the release changed in one sentence (you skipped step 3)
