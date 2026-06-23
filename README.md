# django-absurd

Django integration for [Absurd](https://earendil-works.github.io/absurd/), the
Postgres-native workflow engine. Wraps Absurd's SDK so it reuses Django's database
connection, ships its schema as Django migrations, and exposes its queues and tasks
through Django settings, management commands, and system checks.

> **Alpha.** APIs and behavior may change between releases.

## Requirements

- Python 3.12+
- Django 6.0+
- PostgreSQL with the **psycopg (v3)** Django backend (the Absurd SDK reuses Django's
  connection and requires psycopg3)

## Installation

```console
pip install django-absurd
```

Pre-release tags (e.g. `v0.1.0a1`) upload as PyPI pre-releases, which `pip install`
skips unless you pass `--pre`:

```console
pip install --pre django-absurd
```

## License

MIT — see [LICENSE](LICENSE).
