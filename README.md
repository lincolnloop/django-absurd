# django-absurd

Django integration for [Absurd](https://earendil-works.github.io/absurd/), the
Postgres-native workflow engine. Runs Django's
[Tasks](https://docs.djangoproject.com/en/6.0/topics/tasks/) framework on Postgres — no
separate broker — reusing Django's own database connection.

> **Alpha.** APIs and behavior may change between releases.

## Requirements

- Python 3.12+, Django 6.0+
- PostgreSQL with the **psycopg (v3)** Django backend (the Absurd SDK reuses Django's
  connection and requires psycopg3)

## Install

```console
pip install django-absurd
```

## Quickstart

Add the app and point Django's `TASKS` setting at the backend:

```python
# settings.py
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

```console
python manage.py migrate          # create the Absurd schema
python manage.py absurd_worker    # run a worker (consumes the "default" queue)
```

Define a task with Django's Tasks API and enqueue it — the `"default"` queue is
**created automatically** on first use:

```python
from django.tasks import task


@task
def add(a: int, b: int) -> int:
    return a + b


result = add.enqueue(2, 3)  # returns a TaskResult; the worker runs it
```

## Documentation

- **[Integration guide](django_absurd/AGENTS.md)** — full configuration and `OPTIONS`,
  workers, task parameters, retrieving results, admin introspection, querying queue
  state with the ORM, deployment notes, and adopting an existing Absurd database.
  Includes
  [scheduling recurring tasks](django_absurd/AGENTS.md#scheduling-recurring-tasks) (beat
  and pg_cron schedulers) and
  [durable steps & sleep](django_absurd/AGENTS.md#durable-steps--sleep).
- **[Runnable examples](examples/)** — three dockerized nanodjango demos (`web`
  enqueue+result, `beat`, and `pg_cron`), each with one `docker compose up`.

## License

MIT — see [LICENSE](LICENSE).
