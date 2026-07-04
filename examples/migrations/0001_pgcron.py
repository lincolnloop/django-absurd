"""Install the pg_cron extension.

This migration demonstrates the user-owned pattern described in the django-absurd
docs: the library ships no CREATE EXTENSION migration — pg_cron requires
shared_preload_libraries (a server restart, not a migration) and superuser
privileges, so installing it is always an operator/application responsibility.

The example database container runs as superuser with shared_preload_libraries=pg_cron
set, so this migration can create the extension cleanly.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = []

    operations = [
        migrations.RunSQL(
            sql="CREATE EXTENSION IF NOT EXISTS pg_cron",
            reverse_sql="DROP EXTENSION IF EXISTS pg_cron",
        ),
    ]
