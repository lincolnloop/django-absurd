# django-absurd

<!-- prettier-ignore-start -->
[![CI](https://github.com/lincolnloop/django-absurd/actions/workflows/test.yml/badge.svg?branch=main&event=push)](https://github.com/lincolnloop/django-absurd/actions/workflows/test.yml?query=branch%3Amain+event%3Apush)
[![Coverage](https://img.shields.io/codecov/c/github/lincolnloop/django-absurd.svg)](https://codecov.io/gh/lincolnloop/django-absurd)
[![PyPI](https://img.shields.io/pypi/v/django-absurd.svg)](https://pypi.org/project/django-absurd/)
[![Python versions](https://img.shields.io/pypi/pyversions/django-absurd.svg)](https://pypi.org/project/django-absurd/)
[![Django versions](https://img.shields.io/pypi/frameworkversions/django/django-absurd.svg)](https://pypi.org/project/django-absurd/)
[![License](https://img.shields.io/pypi/l/django-absurd.svg)](https://github.com/lincolnloop/django-absurd/blob/main/LICENSE)
<!-- prettier-ignore-end -->

Run background tasks and durable workflows in Django on **Postgres**. Plugs
[Absurd](https://earendil-works.github.io/absurd/), a Postgres-native workflow engine,
into Django's [Tasks](https://docs.djangoproject.com/en/6.0/topics/tasks/) framework,
reusing the database connection your project already has.

## Install

```console
uv add django-absurd
```

```console
pip install django-absurd
```

Needs Python 3.12+, Django 6.0+, and PostgreSQL on the **psycopg (v3)** driver — Absurd
reuses Django's connection, so psycopg2 won't work.

## Quickstart

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
# Installs the Absurd schema and provisions the queues you declared.
python manage.py migrate
```

```python
from django.tasks import task


@task
def add(a: int, b: int) -> int:
    return a + b


# Returns a TaskResult straight away; a worker runs the task.
result = add.enqueue(2, 3)
```

```console
python manage.py absurd_worker
```

That's the whole loop. The `"default"` queue is declared for you, so `migrate`
provisions it without any `QUEUES` setting of your own.

## Documentation

- **[Documentation](https://lincolnloop.github.io/django-absurd/)** — tasks, workflows,
  cron jobs, workers, cleanup, monitoring, testing, and configuration.
- **[Runnable examples](https://github.com/lincolnloop/django-absurd/tree/main/examples)**
  — three dockerized nanodjango demos (`web` enqueue+result, `beat`, and `pg_cron`),
  each with one `docker compose up`.

## License

MIT — see [LICENSE](https://github.com/lincolnloop/django-absurd/blob/main/LICENSE).
