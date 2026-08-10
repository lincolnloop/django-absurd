from __future__ import annotations

import importlib.resources

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("django_absurd", "0001_initial_0_4_0")]

    # No reverse_sql: Absurd publishes no downgrade SQL, so this delta is irreversible
    # rather than reporting a rollback that left the database at the newer schema.
    operations = [
        migrations.RunSQL(
            sql=importlib.resources.files("django_absurd.migrations")
            .joinpath("0002_absurd_0_5_0.sql")
            .read_text(encoding="utf-8"),
        ),
    ]
