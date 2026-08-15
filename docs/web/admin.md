---
icon: lucide/table-2
---

# Admin

![A task's admin page, with its runs, checkpoints, and waits inline](assets/admin-task.png)

One page per task, showing everything that happened to it: every attempt under **Runs**,
every completed step under **Checkpoints**, and anything it blocked on under **Waits**.
Registered automatically with `django.contrib.admin` installed.

- Read-only. There is no retry or cancel — the admin reports, it does not drive.
- Runs, Checkpoints, Events, Waits, and the Queues catalog get their own changelists
  too, each spanning every queue with a queue filter.
- A queue created only by an enqueue is not in the views yet, so its tasks do not
  appear. The changelist says so and names the queues — run
  `manage.py absurd_sync_queues` or start a worker on them.
- Toggle with `ENABLE_ADMIN`, or register on your own site with `ADMIN_SITE`
  ([Configuration](configuration.md#backend-options)).

## Register schedules

![The ScheduledTask add form](assets/admin-schedule.png)

[pg_cron](cron-jobs.md#postgres-side-pg_cron) schedules are the one writable surface.
Add one (name, task, cron — the rest resolves from the task's
[decorators](tasks.md#retries-spawn-options)), then change it to fill `args` / `kwargs`
and tick **Enabled**. Saving or deleting an enabled row (un)schedules its job at once.

- Rows declared in `SCHEDULE` are read-only; settings own those.
- `name` and the resolved options are frozen at create.
- `max_attempts` defaults to `5`; clearing it means retry forever.
- Writes bypassing `.save()` (data migrations, `bulk_create`, `update`, raw SQL) emit on
  the next [reconcile](cron-jobs.md#reconcile-without-migrating), not immediately.
- `loaddata` bypasses the router — pass `--database=<alias>`. Another database raises
  `NotImplementedError`.

→ [Django: the admin site](https://docs.djangoproject.com/en/6.0/ref/contrib/admin/).
