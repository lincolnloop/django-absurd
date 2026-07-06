---
icon: lucide/settings
---

# Configuration

Everything django-absurd reads lives under Django's
[`TASKS`](https://docs.djangoproject.com/en/6.0/topics/tasks/) setting. A minimal setup:

```python title="settings.py"
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "QUEUES": ["default"],          # optional
        "OPTIONS": {                    # optional
            "DATABASE": "default",
        },
    },
}
```

## Declaring queues

You declare queues in **one** place — never both:

`QUEUES` (list) : Just the names. Use this when queues need no special policy.

    ```python
    "QUEUES": ["default", "reports", "emails"]
    ```

`OPTIONS["QUEUES"]` (map) : Names → per-queue policy
([`absurd_sdk.CreateQueueOptions`](https://earendil-works.github.io/absurd/sdks/python/)).
Use this to set
[storage mode, retention, partitioning](https://earendil-works.github.io/absurd/storage/).

    ```python
    "OPTIONS": {"QUEUES": {
        "default": {},
        "reports": {"storage_mode": "partitioned", "cleanup_ttl": "7 days"},
    }}
    ```

!!! note

    Setting both the top-level `QUEUES` list **and** `OPTIONS["QUEUES"]` is a
    configuration error (`absurd.E002`). Undeclared queue names are rejected, never
    silently created.

## Backend `OPTIONS`

All optional:

| Option                 | Default                          | What it does                                                                                     |
| ---------------------- | -------------------------------- | ------------------------------------------------------------------------------------------------ |
| `DATABASE`             | `"default"`                      | Which [`DATABASES`](https://docs.djangoproject.com/en/6.0/ref/settings/#databases) alias to use. |
| `DEFAULT_MAX_ATTEMPTS` | `5`                              | Retry ceiling per task (override per task/call — see [Tasks](tasks.md#retries-spawn-options)).   |
| `QUEUES`               | —                                | Map of queue name → policy (above). Mutually exclusive with the top-level list.                  |
| `ENABLE_ADMIN`         | `True`                           | Register the read-only Absurd models in the Django admin.                                        |
| `ADMIN_SITE`           | `("django.contrib.admin.site",)` | Dotted paths to the `AdminSite`(s) to register on.                                               |

## Non-default database

Only when `DATABASE` points at an alias other than `"default"`, also register the router
so django-absurd's schema and queries route there
([multi-DB routers](https://docs.djangoproject.com/en/6.0/topics/db/multi-db/#using-routers)):

```python title="settings.py"
DATABASE_ROUTERS = ["django_absurd.routers.AbsurdRouter"]
```

## Validate it

`python manage.py check django_absurd` verifies the configuration and points at anything
wrong. Fix what it reports rather than silencing it:

| ID            | Means                                                                                                                    |
| ------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `absurd.E001` | Backend / database misconfiguration.                                                                                     |
| `absurd.E002` | `QUEUES` declared in both the top level and `OPTIONS`.                                                                   |
| `absurd.E003` | Invalid per-queue policy options.                                                                                        |
| `absurd.E004` | Multiple Absurd backends targeting different databases.                                                                  |
| `absurd.E005` | `AbsurdRouter` missing from `DATABASE_ROUTERS`.                                                                          |
| `absurd.E006` | `ENABLE_ADMIN` isn't a bool, or `ADMIN_SITE` doesn't resolve to `AdminSite`s.                                            |
| `absurd.E007` | Invalid `SCHEDULE` entry (see [Cron Jobs](cron-jobs.md)).                                                                |
| `absurd.E008` | `SCHEDULER` is `pg_cron` but `django_absurd.pg_cron` is not in `INSTALLED_APPS` (see [Cron Jobs](cron-jobs.md)).         |
| `absurd.W003` | (Warning) `django_absurd.pg_cron` is ordered before `django_absurd` in `INSTALLED_APPS` (see [Cron Jobs](cron-jobs.md)). |
