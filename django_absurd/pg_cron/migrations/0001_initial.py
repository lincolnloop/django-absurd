from django.contrib.postgres.operations import CreateExtension
from django.core.validators import MinValueValidator
from django.db import migrations, models

import django_absurd.pg_cron.models
import django_absurd.pg_cron.validators
import django_absurd.validators

# Reads the ScheduledTask row and rebuilds the retry_strategy / cancellation jsonb from
# the typed sub-columns (omitting null keys) before PERFORM absurd.spawn_task.
CREATE_FN = """
CREATE OR REPLACE FUNCTION public.django_absurd_run_scheduled(p_source text, p_name text)
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
       AND name = p_name;

    IF NOT FOUND OR NOT v.enabled THEN
        RETURN;
    END IF;

    v_params := jsonb_build_object('args', v.args, 'kwargs', v.kwargs);

    v_options := '{}'::jsonb;
    IF v.max_attempts IS NOT NULL THEN
        v_options := v_options || jsonb_build_object('max_attempts', v.max_attempts);
    END IF;
    IF v.retry_kind != '' THEN
        v_options := v_options || jsonb_build_object('retry_strategy', jsonb_strip_nulls(jsonb_build_object('kind', v.retry_kind, 'base_seconds', v.retry_base_seconds, 'factor', v.retry_factor, 'max_seconds', v.retry_max_seconds)));
    END IF;
    IF v.headers IS NOT NULL THEN
        v_options := v_options || jsonb_build_object('headers', v.headers);
    END IF;
    IF v.cancellation_max_duration IS NOT NULL OR v.cancellation_max_delay IS NOT NULL THEN
        v_options := v_options || jsonb_build_object('cancellation', jsonb_strip_nulls(jsonb_build_object('max_duration', v.cancellation_max_duration, 'max_delay', v.cancellation_max_delay)));
    END IF;
    IF v.idempotency_key != '' THEN
        v_options := v_options || jsonb_build_object('idempotency_key', v.idempotency_key);
    END IF;

    PERFORM absurd.spawn_task(v.queue, v.task, v_params, v_options);
END;
$$;
"""

DROP_FN = "DROP FUNCTION IF EXISTS public.django_absurd_run_scheduled(text, text);"


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
                (
                    "name",
                    models.CharField(
                        validators=[
                            django_absurd.pg_cron.validators.validate_name_charset
                        ]
                    ),
                ),
                (
                    "source",
                    models.CharField(
                        choices=[("s", "Settings"), ("a", "Admin")], default="s"
                    ),
                ),
                (
                    "task",
                    models.CharField(
                        validators=[django_absurd.validators.validate_task_path]
                    ),
                ),
                (
                    "queue",
                    models.CharField(
                        choices=django_absurd.pg_cron.models.get_declared_queue_choices,
                    ),
                ),
                (
                    "args",
                    models.JSONField(
                        blank=True,
                        default=list,
                        error_messages={"invalid": "args is not JSON-serializable."},
                        validators=[django_absurd.validators.validate_args_is_list],
                    ),
                ),
                (
                    "kwargs",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        error_messages={"invalid": "kwargs is not JSON-serializable."},
                        validators=[django_absurd.validators.validate_kwargs_is_dict],
                    ),
                ),
                (
                    "max_attempts",
                    models.IntegerField(
                        blank=True,
                        default=django_absurd.pg_cron.models.get_default_max_attempts,
                        null=True,
                        validators=[MinValueValidator(1)],
                    ),
                ),
                (
                    "retry_kind",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("exponential", "Exponential"),
                            ("fixed", "Fixed"),
                            ("none", "None"),
                        ],
                        default="",
                    ),
                ),
                ("retry_base_seconds", models.FloatField(blank=True, null=True)),
                ("retry_factor", models.FloatField(blank=True, null=True)),
                ("retry_max_seconds", models.FloatField(blank=True, null=True)),
                (
                    "headers",
                    models.JSONField(
                        blank=True,
                        null=True,
                        validators=[
                            django_absurd.validators.validate_headers_is_object
                        ],
                    ),
                ),
                (
                    "cancellation_max_duration",
                    models.IntegerField(blank=True, null=True),
                ),
                ("cancellation_max_delay", models.IntegerField(blank=True, null=True)),
                ("idempotency_key", models.CharField(blank=True, default="")),
                (
                    "cron",
                    models.CharField(
                        help_text=(
                            "A 5-field cron (e.g. '0 2 * * *') or the interval form"
                            " '<n> seconds' (1-59). High-frequency schedules (a few"
                            " seconds) generate a lot of runs, so take care. See <a"
                            ' href="https://github.com/citusdata/pg_cron"'
                            ' target="_blank" rel="noopener">pg_cron</a> for the exact'
                            " schedule syntax."
                        )
                    ),
                ),
                ("enabled", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "django_absurd_scheduledtask",
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("max_attempts__isnull", True),
                            ("max_attempts__gte", 1),
                            _connector="OR",
                        ),
                        name="pg_cron_scheduledtask_max_attempts_positive",
                    )
                ],
                "unique_together": {("source", "name")},
            },
        ),
        migrations.RunSQL(sql=CREATE_FN, reverse_sql=DROP_FN),
    ]
