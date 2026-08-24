---
icon: lucide/rocket
---

<p align="center">
  <img src="assets/logo-full.png" alt="django-absurd — ceci n'est pas une queue" width="340">
</p>

# django-absurd

Run background tasks and durable workflows in Django on **Postgres**. It plugs
[Absurd](https://earendil-works.github.io/absurd/), a Postgres-native workflow engine,
into Django's built-in
[Tasks framework](https://docs.djangoproject.com/en/6.0/topics/tasks/) and reuses the
database connection your project already has.

## Install

=== "uv"

    ```bash
    uv add django-absurd
    ```

=== "pip"

    ```bash
    pip install django-absurd
    ```

Needs Python **3.12+**, Django **6.0+**, and PostgreSQL on the **psycopg (v3)** driver —
Absurd reuses Django's connection, so psycopg2 won't work.

## Quickstart

**1. Add the app and point Django's `TASKS` setting at the backend:**

```python title="settings.py"
INSTALLED_APPS = [
    # ...
    "django_absurd",
]

TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
    },
}
```

**2. Migrate.** This installs Absurd's schema and provisions your declared queues:

```bash
python manage.py migrate
```

**3. Write a task** with Django's `@task` decorator — anywhere importable:

```python
from django.tasks import task


@task
def add(a: int, b: int) -> int:
    return a + b
```

**4. Enqueue it.** Returns a `TaskResult`; a worker runs it:

```python
result = add.enqueue(2, 3)
```

**5. Run a worker:**

```bash
python manage.py absurd_worker
```

That's the whole loop. The task runs on the [worker](workers.md) and its result is
stored in Postgres — [fetch it later](tasks.md#read-the-result) with
`add.get_result(result.id)`.

## Next

- **[Tasks](tasks.md)** — enqueue with retries and other options, and read results.
- **[Workflows](workflows.md)** — checkpointed steps, durable sleep, and events.
- **[Cron Jobs](cron-jobs.md)** — run tasks on a recurring cadence.
- **[Workers](workers.md)** — running them, and how runs and retries work.
- **[Monitoring](monitoring.md)** — logs, the admin, and querying queue state.
- **[Configuration](configuration.md)** — every setting, in one place.
