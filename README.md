# django-absurd

Run background tasks in Django on **Postgres** — no separate broker, no Redis, no
Celery. Plugs [Absurd](https://earendil-works.github.io/absurd/), a Postgres-native
workflow engine, into Django's
[Tasks](https://docs.djangoproject.com/en/6.0/topics/tasks/) framework, reusing your
existing database connection.

> **Beta.** The API is settling ahead of 1.0; behavior may still change.

## Install

```console
uv add django-absurd --prerelease allow    # or: pip install --pre django-absurd
```

Only pre-releases are published before 1.0, hence the flags. Needs Python 3.12+, Django
6.0+, and PostgreSQL on the **psycopg (v3)** driver — Absurd reuses Django's connection,
so psycopg2 won't work.

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
python manage.py migrate    # installs the Absurd schema, provisions declared queues
```

```python
from django.tasks import task


@task
def add(a: int, b: int) -> int:
    return a + b


result = add.enqueue(2, 3)   # returns a TaskResult; a worker runs it
```

```console
python manage.py absurd_worker
```

That's the whole loop. The `"default"` queue is declared for you, so `migrate`
provisions it without any `QUEUES` setting of your own.

## Documentation

- **[Documentation](https://lincolnloop.github.io/django-absurd/)** — tasks, workflows,
  cron jobs, workers, cleanup, monitoring, testing, and configuration.
- **[Runnable examples](examples/)** — three dockerized nanodjango demos (`web`
  enqueue+result, `beat`, and `pg_cron`), each with one `docker compose up`.

## License

MIT — see [LICENSE](LICENSE).
