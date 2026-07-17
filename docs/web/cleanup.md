---
icon: lucide/trash-2
---

# Cleanup / retention

Absurd stores task rows in Postgres — they accumulate unless you prune them. Each queue
exposes two retention knobs (see
[Configuration — Declaring queues](configuration.md#declaring-queues)):

| Option          | What it controls                                                                                         |
| --------------- | -------------------------------------------------------------------------------------------------------- |
| `cleanup_ttl`   | Minimum age a terminal task must reach before it is deleted.                                             |
| `cleanup_limit` | Max terminal rows deleted **per queue** per run — applied separately to task and event rows (batch cap). |

**Terminal** means completed, failed, or cancelled — running and pending tasks are never
touched. See [Absurd's cleanup docs](https://earendil-works.github.io/absurd/cleanup/)
for the retention model, and
[Absurd's storage docs](https://earendil-works.github.io/absurd/storage/) for how queues
store rows.

## Run on demand

```bash
python manage.py absurd_cleanup            # every queue
python manage.py absurd_cleanup reports    # only the named queue(s)
```

Deletes eligible rows across the configured Absurd backend and prints per-queue counts:

```
default: 12 tasks, 0 events deleted
```

The same function is importable — `cleanup_queues()` for all queues, or
`cleanup_queues(["reports", "emails"])` for specific ones — returning a list of
per-queue count dicts.

Passing an unknown queue name to `absurd_cleanup` (or `cleanup_queues([...])`) raises a
database error — the queue must exist. This is deliberate: cleanup is a maintenance
operation, so the raw error surfaces rather than being masked by a guard.

## Schedule recurring cleanup

Add `OPTIONS["CLEANUP"] = {"schedule": "<cron>"}` to run cleanup automatically on
cadence — no user code required:

```python title="settings.py"
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "OPTIONS": {
            "CLEANUP": {"schedule": "0 3 * * *"},   # 3am daily
        },
    },
}
```

This works under **either** scheduler:

- **beat** — runs cleanup in-process on the declared cadence.
- **pg_cron** — schedules Absurd's own native cleanup job (`absurd_cleanup_all`, the
  same identity `absurdctl cron` uses) alongside your other cron jobs (see
  [Cron Jobs](cron-jobs.md)). When `django_absurd.pg_cron` is installed, django-absurd
  is authoritative over this job: it schedules it from `OPTIONS["CLEANUP"]` and removes
  it otherwise — including at migrate teardown / scheduler-flip even when `CLEANUP` was
  never set — so a job created via `absurdctl cron` is reclaimed and removed. Drive
  cleanup one way only — `OPTIONS["CLEANUP"]` **or** `absurdctl cron`, not both.

`manage.py check` reports `absurd.E010` for a malformed `CLEANUP` (not a
`{"schedule": …}` map, or unknown keys); the cron grammar is checked at `check` time for
beat, and by the database at sync for pg_cron. See
[Absurd's cleanup docs](https://earendil-works.github.io/absurd/cleanup/) for the
underlying retention model.

Retention knobs (`cleanup_ttl`, `cleanup_limit`) remain per-queue policy — configure
them in `OPTIONS["QUEUES"]` (see
[Configuration — Declaring queues](configuration.md#declaring-queues)).

## Reset — drop all queues

`absurd_flush` **deletes all task history** — it removes every queue (its per-queue
tables and registry entry) along with all tasks, runs, and events in them. It does
**not** uninstall Absurd: the schema, migrations, and functions stay in place, so you
never re-`migrate` — you only re-provision the queues. It prompts for confirmation; pass
`--noinput` (alias `--no-input`) to skip the prompt in automation:

```bash
python manage.py absurd_flush            # prompts, then drops on 'yes'
python manage.py absurd_flush --noinput  # drops without prompting
```

!!! warning "Destructive"

    This permanently deletes all task history across every queue. It leaves the Absurd
    schema and migrations untouched — re-provision your declared queues afterward with
    `migrate`, `absurd_sync_queues`, or by starting a worker.

    Any existing scheduled jobs (pg_cron schedule jobs and beat schedules) survive the
    flush and will **error on each fire** until the queues exist again — re-provision
    promptly. Exception: the `absurd_cleanup_all` job (set via `OPTIONS["CLEANUP"]`)
    also survives and runs harmlessly — it finds no eligible rows until queues are
    re-provisioned.

    `absurd_flush` (via per-queue `drop_queue` → `disable_cron`) also removes that
    queue's per-queue Absurd maintenance cron jobs (`absurd_partitions_<md5>` /
    `absurd_cleanup_<md5>` / `absurd_detach_plan_<md5>`) if any were created via
    `absurdctl cron --enable <queue>`; the global `absurd_cleanup_all` job is
    unaffected (it survives).
