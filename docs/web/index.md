---
icon: lucide/rocket
---

<p align="center">
  <img src="assets/logo-full.png" alt="django-absurd — ceci n'est pas une queue" width="340">
</p>

# django-absurd

Run background tasks in Django on **Postgres** — no separate broker, no Redis, no
Celery. It plugs [Absurd](https://earendil-works.github.io/absurd/), a Postgres-native
workflow engine, into Django's built-in
[Tasks framework](https://docs.djangoproject.com/en/6.0/topics/tasks/) and reuses your
existing database connection.

!!! warning "Alpha"

    APIs and behavior may change between releases.

## Requirements

- Python **3.12+**, Django **6.0+**
- PostgreSQL with the **psycopg (v3)** driver (`django.db.backends.postgresql`). Absurd
  reuses Django's connection — psycopg2 won't work.

## Install

django-absurd is in **alpha** — only pre-releases are published, so your installer must
be allowed to pick them up.

```bash
uv add django-absurd --prerelease allow
```

Using pip:

```bash
pip install --pre django-absurd
```

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

That's the whole loop. The task runs on the [worker](how-it-works.md#workers), and the
result is stored in Postgres — [fetch it later](tasks.md#read-the-result) with
`add.get_result(result.id)`.

## Next

- **[Tasks](tasks.md)** — enqueue with retries and other options, and read results.
- **[Configuration](configuration.md)** — every setting, in one place.
- **[How it works](how-it-works.md)** — how queues, runs, checkpoints, and the admin fit
  together, with links to the Absurd and Django docs.
