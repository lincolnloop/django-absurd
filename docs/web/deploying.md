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

Those two lines are the whole deploy for most projects — queue changes are rare, and
`migrate` covers them when they happen. Add `python manage.py absurd_sync_queues` only
when your release step doesn't run `migrate` at all.

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

Read-only: reports what would change and exits non-zero if anything would.

```
🗃️ Would create: pending
CommandError: Queues are not in sync. Run: manage.py absurd_sync_queues
```

`Would reconcile:` and `Would repair:` cover drifted policy and a queue whose catalog
row outlived its tables. In sync, it prints `🗃️ No queues to sync.` and exits 0.

Reach for it in a release step that asserts rather than acts, or to ask a production
database whether it is in sync without needing DDL rights. It is also the way to make
provisioning failure loud: the `post_migrate` receiver reports nothing when it can't
provision, so a `migrate` that provisioned no queue still exits 0.

## `migrate --check` doesn't see queues

[`migrate --check`](https://docs.djangoproject.com/en/6.0/ref/django-admin/#cmdoption-migrate-check)
exits before `post_migrate` fires — it is a predicate about unapplied migrations, not a
migration. Declaring a queue touches no migration file, so it exits 0:

```bash
python manage.py migrate --check || python manage.py migrate
```

That pipeline skips the only step that would have provisioned a newly declared queue,
and reports success. Run `absurd_sync_queues --check` for the queue half.
