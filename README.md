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
pip install django-absurd          # add --pre for alpha (pre-release) versions
```

## Quickstart

Add the app, register the router, and point Django's `TASKS` setting at the backend:

```python
# settings.py
INSTALLED_APPS = [..., "django_absurd"]
DATABASE_ROUTERS = ["django_absurd.routers.AbsurdRouter"]

TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "QUEUES": ["default"],  # queue names this app enqueues to
    },
}
```

```console
python manage.py migrate          # install Absurd's schema (shipped, offline)
python manage.py absurd_worker    # run a worker
```

Define tasks with Django's Tasks API and enqueue them — the declared queue is **created
automatically** on first use, so there's no provisioning step:

```python
from django.tasks import task

@task
def send_report(user_id): ...

send_report.enqueue(42)
```

## Documentation

- **[Integration guide](django_absurd/AGENTS.md)** — full configuration and `OPTIONS`,
  workers, task parameters, retrieving results, deployment notes, and adopting an
  existing Absurd database.
- **[Runnable example](examples/)** — a dockerized Django project demonstrating the
  whole flow end to end.

## License

MIT — see [LICENSE](LICENSE).
