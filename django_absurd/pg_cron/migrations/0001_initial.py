from django.db import migrations, models

CREATE_FN = """
CREATE OR REPLACE FUNCTION public.django_absurd_run_scheduled(p_source text, p_alias text, p_name text)
RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    v public.django_absurd_scheduledjob%ROWTYPE;
BEGIN
    SELECT *
      INTO v
      FROM public.django_absurd_scheduledjob
     WHERE source = p_source
       AND alias = p_alias
       AND name = p_name;

    IF NOT FOUND OR NOT v.enabled THEN
        RETURN;
    END IF;

    PERFORM absurd.spawn_task(
        v.queue,
        v.task,
        v.params,
        COALESCE(v.options, '{}'::jsonb)
    );
END;
$$;
"""

DROP_FN = (
    "DROP FUNCTION IF EXISTS public.django_absurd_run_scheduled(text, text, text);"
)


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        # absurd.spawn_task (used by the wrapper function) is installed by the
        # core django_absurd schema migration.
        ("django_absurd", "0001_initial_0_4_0"),
    ]

    operations = [
        migrations.CreateModel(
            name="ScheduledJob",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.TextField()),
                (
                    "source",
                    models.TextField(
                        choices=[("settings", "Settings"), ("admin", "Admin")],
                        default="settings",
                    ),
                ),
                ("alias", models.TextField()),
                ("task", models.TextField()),
                ("queue", models.TextField(blank=True, default="")),
                ("params", models.JSONField(default=dict)),
                ("options", models.JSONField(default=dict)),
                ("cron", models.TextField()),
                ("enabled", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "django_absurd_scheduledjob",
                "unique_together": {("source", "alias", "name")},
            },
        ),
        migrations.RunSQL(sql=CREATE_FN, reverse_sql=DROP_FN),
    ]
