---
icon: lucide/ship
---

# Deploying

```bash
python manage.py check --deploy
python manage.py migrate
```

`migrate` installs Absurd's schema and provisions the
[declared queues](configuration.md#declaring-queues) through `post_migrate` — on every
run, whether or not a migration was applied. Nothing at runtime creates a queue:
enqueuing to a declared but unprovisioned queue raises
[`QueueNotProvisionedError`](configuration.md#exceptions), and
[`absurd_worker`](workers.md#run-a-worker) refuses to start on one.

Those two lines are the whole deploy for most projects. Everything below is for a
release step that departs from them.

## Updating queues explicitly

```bash
python manage.py absurd_sync_queues
```

`migrate` already does this on every run; this command does only that part — for a
release step that doesn't run `migrate`, or to pick up a queue change between deploys.

## What `migrate` needs

Absurd's schema ships as a Django
[migration](https://docs.djangoproject.com/en/6.0/topics/migrations/). The SQL comes
from the pinned Absurd version and is never fetched at migrate time.

- **Privileges:** `migrate` needs `GRANT CREATE ON DATABASE <db>` — no extension, so no
  superuser and no managed-Postgres allow-list entry. `CREATE SCHEMA IF NOT EXISTS`
  checks that privilege _before_ the schema's existence, so pre-creating `absurd`
  yourself doesn't avoid the grant. The schema name is fixed. Provisioning then creates
  tables _inside_ `absurd`, so the role needs `CREATE` on the schema as well — implicit
  when `migrate` created it, and not when someone else did.
- **Already running Absurd?** `python manage.py migrate --fake django_absurd` records
  the migration as applied without re-running the DDL. Only do this when the existing
  schema matches `django_absurd.ABSURD_SCHEMA_VERSION` exactly — faking doesn't check,
  and a mismatch fails at runtime in ways Django can't detect. Faking skips the DDL but
  still fires `post_migrate`, so the declared queues are provisioned either way — which
  needs `CREATE` on the schema the other role created. Without it `migrate` fails, where
  earlier releases provisioned nothing and left the first worker or enqueue to heal it.

→ [Absurd: database setup](https://earendil-works.github.io/absurd/database/).

## Non-default database

```bash
python manage.py migrate --database=absurd   # Absurd's schema + queues
python manage.py migrate                     # Django's own tables
```

| Command                      | How it picks a database                                                                                                                                   |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `migrate --database=<alias>` | Django's [`--database`](https://docs.djangoproject.com/en/6.0/ref/django-admin/#cmdoption-migrate-database) flag — this is what installs Absurd's schema. |
| `migrate`                    | The `default` alias, where Django's own `LogEntry`, session, and `ContentType` tables live. The [admin](admin.md) needs them.                             |
| `absurd_sync_queues`         | No flag — resolves the Absurd alias from `TASKS`.                                                                                                         |
| `absurd_worker`              | No flag — same.                                                                                                                                           |

- `post_migrate` is per-database, so `migrate --database=absurd` is the run that
  provisions — a `migrate` on `default` leaves the Absurd alias untouched. Which alias
  that is comes from [`OPTIONS["DATABASE"]`](configuration.md#backend-options).
- A non-default alias needs `django_absurd.routers.AbsurdRouter` in `DATABASE_ROUTERS`
  (`absurd.E005`) — see [Non-default database](configuration.md#non-default-database).

## Assert instead of act

```bash
python manage.py absurd_sync_queues --check
```

Read-only: reports what would change and exits non-zero if anything would. It answers
about queues; the admin views are rebuilt by every real run, so a dropped view is not
something it reports.

```
🗃️ Would create: pending
CommandError: Queues are not in sync. Run: manage.py absurd_sync_queues
```

`Would reconcile:` and `Would repair:` cover drifted policy and a queue whose catalog
row outlived its tables. In sync, it prints `🗃️ No queues to sync.` and exits 0.

Reach for it in a release step that asserts rather than acts, or to ask a production
database whether it is in sync without needing DDL rights. It is not a second line of
defence behind `migrate`, which fails on a provisioning error itself — it answers the
question a release step that never runs `migrate`, or runs it against another database,
would otherwise leave unasked.

The one condition `migrate` reports without failing is an absent schema: it prints
`Not provisioned: the Absurd schema is absent; this migrate did not install it.` and
exits 0, because there is nothing to provision into yet. That is the state
`migrate --fake` leaves behind.

## `migrate --check` doesn't see queues

[`migrate --check`](https://docs.djangoproject.com/en/6.0/ref/django-admin/#cmdoption-migrate-check)
exits before `post_migrate` fires — it is a predicate about unapplied migrations, not a
migration. Declaring a queue touches no migration file, so it exits 0:

```bash
python manage.py migrate --check || python manage.py migrate
```

That pipeline skips the only step that would have provisioned a newly declared queue,
and reports success. Run `absurd_sync_queues --check` for the queue half.
