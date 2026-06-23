from __future__ import annotations

import importlib.resources
import typing as t

import psycopg
from django.core.exceptions import ImproperlyConfigured
from django.db import migrations, models

if t.TYPE_CHECKING:
    from django.apps.registry import Apps
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def require_psycopg(
    apps: Apps,
    schema_editor: BaseDatabaseSchemaEditor,
) -> None:
    # psycopg (v3) is what the SDK reuses and implies the Postgres wire protocol;
    # this rejects sqlite/mysql and psycopg2. Matches get_absurd_client's check.
    if not isinstance(schema_editor.connection.connection, psycopg.Connection):
        msg = (
            "django-absurd requires the psycopg (v3) PostgreSQL backend. "
            "See https://www.psycopg.org/psycopg3/docs/"
        )
        raise ImproperlyConfigured(msg)


class Migration(migrations.Migration):
    initial = True
    dependencies = []

    operations = [
        migrations.RunPython(require_psycopg, migrations.RunPython.noop),
        migrations.RunSQL(
            sql=importlib.resources.files("django_absurd.migrations")
            .joinpath("0001_initial_0_4_0.sql")
            .read_text(encoding="utf-8"),
            reverse_sql="DROP SCHEMA IF EXISTS absurd CASCADE;",
        ),
        migrations.CreateModel(
            name="Queue",
            fields=[
                ("queue_name", models.TextField(primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField()),
                (
                    "storage_mode",
                    models.TextField(
                        choices=[
                            ("unpartitioned", "Unpartitioned"),
                            ("partitioned", "Partitioned"),
                        ]
                    ),
                ),
                (
                    "default_partition",
                    models.TextField(
                        choices=[("enabled", "Enabled"), ("disabled", "Disabled")]
                    ),
                ),
                ("partition_lookahead", models.DurationField()),
                ("partition_lookback", models.DurationField()),
                ("cleanup_ttl", models.DurationField()),
                ("cleanup_limit", models.IntegerField()),
                (
                    "detach_mode",
                    models.TextField(choices=[("none", "None"), ("empty", "Empty")]),
                ),
                ("detach_min_age", models.DurationField()),
            ],
            options={
                "db_table": 'absurd"."queues',
                "managed": False,
            },
        ),
    ]
