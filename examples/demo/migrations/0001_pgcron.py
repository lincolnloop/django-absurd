"""Install the pg_cron extension.

pg_cron is an operator-installed extension — django-absurd ships no
CREATE EXTENSION migration of its own. Installing it is the application's
responsibility because it needs superuser privileges and a
`shared_preload_libraries = pg_cron` server restart (done in compose.yaml).

CreateExtension is Django's first-class operation for this: it issues
`CREATE EXTENSION IF NOT EXISTS pg_cron` with a matching reverse — prefer it
over raw RunSQL. The demo's database container runs as a superuser with
pg_cron preloaded, so this applies cleanly on `manage.py migrate`.
"""

from django.contrib.postgres.operations import CreateExtension
from django.db import migrations


class Migration(migrations.Migration):
    dependencies: list[tuple[str, str]] = []

    operations = [
        CreateExtension("pg_cron"),
    ]
