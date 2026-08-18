---
icon: lucide/settings
---

# Configuration

Everything django-absurd reads lives under Django's
[`TASKS`](https://docs.djangoproject.com/en/6.0/topics/tasks/) setting.

```python title="settings.py"
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "QUEUES": ["default"],  # optional
        "OPTIONS": {  # optional
            "DATABASE": "default",
        },
    },
}
```

## Declaring queues

Declare queues in **one** place — never both.

**`QUEUES` (list)** — just the names. Use this when queues need no special policy:

```python
"QUEUES": ["default", "reports", "emails"]
```

**`OPTIONS["QUEUES"]` (map)** — names → per-queue policy
([`absurd_sdk.CreateQueueOptions`](https://earendil-works.github.io/absurd/sdks/python/)).
Use this to set [retention](https://earendil-works.github.io/absurd/storage/)
(`cleanup_ttl` / `cleanup_limit`):

```python
"OPTIONS": {"QUEUES": {
    "default": {},
    "reports": {"cleanup_ttl": "7 days"},
}}
```

Declared queues are provisioned at `migrate`, by `manage.py absurd_sync_queues`, and by
[`dj_absurd.sync_queues()`](testing.md#sync-queues) in tests — nothing else creates one.
`enqueue` and a starting [worker](workers.md) both refuse an unprovisioned queue.

- Setting both forms is a configuration error (`absurd.E002`). Undeclared queue names
  are rejected, never silently created.
- `storage_mode="partitioned"` is declarable but **experimental — not tested yet**, and
  its partition lifecycle isn't automated. Don't rely on it in production.

→ [Absurd: storage](https://earendil-works.github.io/absurd/storage/) (queue types,
partitioning, retention).

## Backend `OPTIONS`

All optional:

| Option                      | Default                          | What it does                                                                                                                                                         |
| --------------------------- | -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `DATABASE`                  | `"default"`                      | Which [`DATABASES`](https://docs.djangoproject.com/en/6.0/ref/settings/#databases) alias to use.                                                                     |
| `DEFAULT_MAX_ATTEMPTS`      | `5`                              | Retry ceiling per task; must be an integer `>= 1` (override per task/call — see [Tasks](tasks.md#retries-spawn-options)).                                            |
| `QUEUES`                    | —                                | Map of queue name → policy (above). Mutually exclusive with the top-level list.                                                                                      |
| `CLEANUP`                   | —                                | Map `{"schedule": "<cron>"}` to run cleanup on cadence (beat: in-process; pg_cron: native job). Omit to skip. See [Cleanup](cleanup.md#schedule-recurring-cleanup).  |
| `SCHEDULE`                  | —                                | Recurring task schedules (beat or pg_cron). See [Cron Jobs](cron-jobs.md).                                                                                           |
| `SYNC_SCHEDULES_ON_MIGRATE` | `True`                           | (pg_cron) Reconcile `SCHEDULE` into pg_cron on `migrate`. See [Cron Jobs](cron-jobs.md#test-databases).                                                              |
| `SYNC_SCHEDULES_ON_TEST_DB` | `False`                          | (pg_cron) Allow that migrate-time sync on a test database. See [Cron Jobs](cron-jobs.md#test-databases).                                                             |
| `PG_CRON_ON_TEST_DB`        | `False`                          | (pg_cron) Opt in to real `cron.*` writes on a test database / active test run — otherwise every such write is a no-op. See [Cron Jobs](cron-jobs.md#test-databases). |
| `ENABLE_ADMIN`              | `True`                           | Register the read-only Absurd models in the Django admin.                                                                                                            |
| `ADMIN_SITE`                | `("django.contrib.admin.site",)` | Dotted paths to the `AdminSite`(s) to register on.                                                                                                                   |

## Non-default database

```python title="settings.py"
DATABASE_ROUTERS = ["django_absurd.routers.AbsurdRouter"]
```

Only when `DATABASE` names an alias other than `"default"`. The
[router](https://docs.djangoproject.com/en/6.0/topics/db/multi-db/#using-routers) sends
django-absurd's schema and queries there.

## Validate it

```bash
python manage.py check django_absurd
```

Verifies the configuration. Fix what it reports rather than silencing it, unless a
check's own hint says otherwise:

| ID            | Means                                                                                                                                                                                         |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `absurd.E001` | Backend / database misconfiguration.                                                                                                                                                          |
| `absurd.E002` | `QUEUES` declared in both the top level and `OPTIONS`.                                                                                                                                        |
| `absurd.E003` | Invalid per-queue policy options.                                                                                                                                                             |
| `absurd.E004` | More than one Absurd backend is configured. django-absurd supports exactly one per project.                                                                                                   |
| `absurd.E005` | `AbsurdRouter` missing from `DATABASE_ROUTERS`.                                                                                                                                               |
| `absurd.E006` | `ENABLE_ADMIN` isn't a bool, or `ADMIN_SITE` doesn't resolve to `AdminSite`s.                                                                                                                 |
| `absurd.E007` | Invalid `SCHEDULE` entry (see [Cron Jobs](cron-jobs.md)).                                                                                                                                     |
| `absurd.E009` | `OPTIONS["DEFAULT_MAX_ATTEMPTS"]` is not an integer `>= 1`.                                                                                                                                   |
| `absurd.E010` | Invalid `CLEANUP` configuration (not a `{"schedule": …}` map, unknown keys, or a cron expression the configured scheduler cannot run) (see [Cleanup](cleanup.md#schedule-recurring-cleanup)). |
| `absurd.E011` | `SYNC_SCHEDULES_ON_TEST_DB` is `True` without `PG_CRON_ON_TEST_DB` (see [Cron Jobs](cron-jobs.md#test-databases)).                                                                            |
| `absurd.E012` | The central `cron.database_name` database is unreachable or missing the `pg_cron` extension — a deploy-time check (see [Cron Jobs](cron-jobs.md#operator-setup)).                             |
| `absurd.E013` | `"django_absurd.pg_cron"` is installed but no `AbsurdBackend` is configured — schedules would save and never fire (see [Cron Jobs](cron-jobs.md#postgres-side-pg_cron)).                      |
| `absurd.E014` | `OPTIONS["QUEUES"]` is not a mapping of queue name to policy options — the bare name list belongs at the top level, as `QUEUES`.                                                              |
| `absurd.W002` | (Warning) A queue's declared `storage_mode` differs from the database; `storage_mode` is immutable once the queue exists.                                                                     |
| `absurd.W003` | (Warning) `django_absurd.pg_cron` is ordered before `django_absurd` in `INSTALLED_APPS` (see [Cron Jobs](cron-jobs.md)).                                                                      |

## Exceptions

```python
from django_absurd.exceptions import DjangoAbsurdError

try:
    emit_event("warehouse.packed:42", queue="reports")
except DjangoAbsurdError:
    ...
```

Typed errors under `DjangoAbsurdError`: `QueueNotDeclaredError` (never declared) and
`QueueNotProvisionedError` (declared, no table yet — run
`manage.py absurd_sync_queues`), raised by `enqueue`, by a starting
[worker](workers.md), by [`emit_event`](workflows.md#emit-from-a-view) and by the test
fixture's [`drain()`](testing.md#drain) and [`get_result()`](testing.md#get-result); and
`SchemaNotInstalledError` (the Absurd schema itself isn't installed — run
`manage.py migrate`).

- `enqueue` raises `QueueNotDeclaredError` only when `QUEUES` is empty or unset. With
  `QUEUES` configured, a typo is rejected earlier as Django's own `InvalidTask`.
- Every `absurd_*` management command turns a fixed set of configuration failures —
  `ImproperlyConfigured`, `BackendNotConfiguredError`,
  `MultipleBackendsConfiguredError`, `SchemaNotInstalledError`, `QueueNotDeclaredError`,
  `QueueNotProvisionedError` — into a `CommandError`; `--traceback` still shows the
  original. Every other error, including any other `DjangoAbsurdError` subclass, keeps
  its own type and full traceback.
- The hierarchy isn't total — other failures still raise plain `ImproperlyConfigured` /
  `RuntimeError` / `TypeError`.
