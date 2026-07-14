from django.contrib.postgres.operations import CreateExtension
from django.db import migrations, models

CREATE_FN = """
CREATE OR REPLACE FUNCTION public.django_absurd_run_scheduled(p_source text, p_alias text, p_name text)
RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
DECLARE
    v public.django_absurd_scheduledtask%ROWTYPE;
    v_params jsonb;
    v_options jsonb;
BEGIN
    SELECT *
      INTO v
      FROM public.django_absurd_scheduledtask
     WHERE source = p_source
       AND alias = p_alias
       AND name = p_name;

    IF NOT FOUND OR NOT v.enabled THEN
        RETURN;
    END IF;

    v_params := jsonb_build_object('args', v.args, 'kwargs', v.kwargs);

    v_options := '{}'::jsonb;
    IF v.max_attempts IS NOT NULL THEN
        v_options := v_options || jsonb_build_object('max_attempts', v.max_attempts);
    END IF;
    IF v.retry_strategy IS NOT NULL THEN
        v_options := v_options || jsonb_build_object('retry_strategy', v.retry_strategy);
    END IF;
    IF v.headers IS NOT NULL THEN
        v_options := v_options || jsonb_build_object('headers', v.headers);
    END IF;
    IF v.cancellation IS NOT NULL THEN
        v_options := v_options || jsonb_build_object('cancellation', v.cancellation);
    END IF;
    IF v.idempotency_key != '' THEN
        v_options := v_options || jsonb_build_object('idempotency_key', v.idempotency_key);
    END IF;

    PERFORM absurd.spawn_task(v.queue, v.task, v_params, v_options);
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
        CreateExtension("pg_cron"),
        migrations.CreateModel(
            name="ScheduledTask",
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
                ("args", models.JSONField(default=list)),
                ("kwargs", models.JSONField(default=dict)),
                ("max_attempts", models.IntegerField(blank=True, null=True)),
                ("retry_strategy", models.JSONField(blank=True, null=True)),
                ("headers", models.JSONField(blank=True, null=True)),
                ("cancellation", models.JSONField(blank=True, null=True)),
                ("idempotency_key", models.TextField(blank=True, default="")),
                ("cron", models.TextField()),
                ("enabled", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "django_absurd_scheduledtask",
                "unique_together": {("source", "alias", "name")},
            },
        ),
        migrations.RunSQL(sql=CREATE_FN, reverse_sql=DROP_FN),
    ]
