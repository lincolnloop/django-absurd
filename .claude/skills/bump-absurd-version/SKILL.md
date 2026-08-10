---
name: bump-absurd-version
description:
  Use when moving django-absurd to a new upstream Absurd release — a Renovate PR bumping
  the `absurdctl` pin, an `absurd-sdk` floor change, `ABSURD_SCHEMA_VERSION` drift, or a
  report that `manage.py migrate` ships an older schema than the pinned version. Covers
  generating the delta migration offline, the artifacts that must move together, and
  what a schema change forces in code, tests, and user docs.
---

# bump-absurd-version

## Overview

Absurd's schema ships as ordinary Django migrations, generated offline from the pinned
`absurdctl` wheel — never hand-written, never fetched at migrate time. A version bump is
therefore codegen plus a set of artifacts that must move together, and the delta itself
tells you what else changed.

**One migration per Absurd release. `0001` is frozen.**

## When to use

- A Renovate PR bumps `absurdctl==` in `pyproject.toml`
- Upstream announces a release and `ABSURD_SCHEMA_VERSION` no longer matches it
- Anything reports a schema version mismatch between the SDK, the pin, and the database

Not for: a Renovate bump of any other dependency, or an `absurd-sdk` patch release with
no schema change (check the delta is empty first — step 2 tells you).

## The artifacts that move together

Miss one and the failure is silent or lands in CI, not here:

| Artifact                | Where                         | Miss it and                                         |
| ----------------------- | ----------------------------- | --------------------------------------------------- |
| `absurdctl` pin         | `pyproject.toml` dev deps     | you generate the delta from the OLD bundled SQL     |
| `absurd-sdk` floor      | `pyproject.toml` dependencies | the SDK and schema can drift apart at install time  |
| Delta SQL + wrapper     | `django_absurd/migrations/`   | nothing ships                                       |
| `ABSURD_SCHEMA_VERSION` | `django_absurd/__init__.py`   | adopting-an-existing-DB guidance lies               |
| Root lockfile           | `uv.lock`                     | `--locked` fails in CI                              |
| **Example lockfiles**   | `examples/*/uv.lock` (three)  | **`uv sync --locked` fails all three example jobs** |
| SDK bounds in comments  | anything naming an SDK range  | a stale bound reads as a verified one               |

The example lockfiles embed the root package's `requires-dist`, because each example
depends on django-absurd by path. Any root dependency change invalidates all three. Run
`uv lock --project examples/<name>` for each.

## Procedure

1. **Bump the `absurdctl` pin first**, then `uv sync`. The delta comes from the wheel's
   bundled SQL, so the old pin can only generate the old range.

2. **Generate the delta, offline:**

   ```bash
   absurdctl migrate --from <current> --to <new> --dump-sql \
     > django_absurd/migrations/000N_absurd_<new_with_underscores>.sql
   ```

   `--dump-sql` needs no database and makes no network call — verify it printed SQL and
   exited 0. An empty or header-only bundle means there is no schema change: stop, and
   bump only the pins.

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
     strings, and a false positive here costs you transactional safety for nothing.

5. **Update the remaining artifacts** from the table above.

6. **Verify against a real database**, not just the suite:

   ```bash
   uv run pytest tests/core tests/pg_cron -q --no-cov        # both suites
   ```

   Then prove the migration actually applies from empty and the schema reports the new
   version:

   ```bash
   uv run python -c "
   import django, os
   os.environ['DJANGO_SETTINGS_MODULE']='tests.settings'
   django.setup()
   from django.core.management import call_command
   call_command('migrate', 'django_absurd')
   from django.db import connection
   with connection.cursor() as c:
       c.execute('select absurd.get_schema_version()')
       print('schema reports:', c.fetchone()[0])
   "
   ```

   That printed version must equal `ABSURD_SCHEMA_VERSION`. This is the one check that
   catches a wrapper reading the wrong file, a stale pin, and a half-applied delta.

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
| Adding `reverse_sql` so a test can reach zero                        | 13 tests once used migrate-zero to mean "schema absent"              | Rename the schema instead — see above                                                                                                                                                              |
| Committing without the example lockfiles                             | `git status` shows them, `git commit -am` on named paths misses them | `uv lock --project examples/<name>` ×3, and stage them                                                                                                                                             |
| Trusting a `concurrently` grep                                       | The word appears in error strings                                    | Read the match before setting `atomic = False`                                                                                                                                                     |
| Reaching for a one-off `--exclude-newer-package` on the command line | The release looks blocked by a cooloff                               | It is not: Absurd is exempt from both cooloffs, in `pyproject.toml` and `renovate.json`. A command-line override instead records a dated one in the lockfile, which fails `uv sync --locked` in CI |
| Treating it as dependency-only                                       | Nothing obviously breaks                                             | Read the delta's comments (step 3) and run `sync-docs`                                                                                                                                             |
| Believing green tests mean it applied                                | The suite reuses a database that may predate the delta               | Do step 6's from-empty check                                                                                                                                                                       |

## Red flags

- You are typing SQL rather than redirecting `--dump-sql` into a file
- You are copying function bodies out of `0001` for any reason
- You edited `0001` — it is frozen; deltas only
- You are adding `reverse_sql` so that a test can migrate to zero — fix the test
- `git status` shows `examples/*/uv.lock` at commit time
- You cannot say what the release changed in one sentence (you skipped step 3)
