import django.core.validators
from django.db import migrations, models

import django_absurd.pg_cron.models

# Rebuilds retry_strategy / cancellation jsonb from the typed sub-columns (omitting
# null keys) before PERFORM absurd.spawn_task.
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

# The wrapper as it stood before typed columns: read the retry_strategy / cancellation
# jsonb columns directly (this migration drops those columns, so the reverse restores
# both the columns and this function body).
RESTORE_FN = """
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


class Migration(migrations.Migration):
    dependencies = [
        (
            "django_absurd_pg_cron",
            "0002_alter_scheduledtask_alias_alter_scheduledtask_args_and_more",
        ),
    ]

    operations = [
        migrations.RemoveField(
            model_name="scheduledtask",
            name="cancellation",
        ),
        migrations.RemoveField(
            model_name="scheduledtask",
            name="retry_strategy",
        ),
        migrations.AddField(
            model_name="scheduledtask",
            name="cancellation_max_delay",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="scheduledtask",
            name="cancellation_max_duration",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="scheduledtask",
            name="retry_base_seconds",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="scheduledtask",
            name="retry_factor",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="scheduledtask",
            name="retry_kind",
            field=models.TextField(
                blank=True,
                choices=[
                    ("exponential", "Exponential"),
                    ("fixed", "Fixed"),
                    ("none", "None"),
                ],
                default="",
            ),
        ),
        migrations.AddField(
            model_name="scheduledtask",
            name="retry_max_seconds",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="scheduledtask",
            name="args",
            field=models.JSONField(
                blank=True,
                default=list,
                error_messages={"invalid": "args is not JSON-serializable."},
            ),
        ),
        migrations.AlterField(
            model_name="scheduledtask",
            name="kwargs",
            field=models.JSONField(
                blank=True,
                default=dict,
                error_messages={"invalid": "kwargs is not JSON-serializable."},
            ),
        ),
        migrations.AlterField(
            model_name="scheduledtask",
            name="max_attempts",
            field=models.IntegerField(
                blank=True,
                default=django_absurd.pg_cron.models.get_default_max_attempts,
                null=True,
                validators=[django.core.validators.MinValueValidator(1)],
            ),
        ),
        migrations.AlterField(
            model_name="scheduledtask",
            name="queue",
            field=models.TextField(
                blank=True,
                choices=django_absurd.pg_cron.models.get_declared_queue_choices,
                default="",
            ),
        ),
        migrations.AlterField(
            model_name="scheduledtask",
            name="source",
            field=models.TextField(
                choices=[("s", "Settings"), ("a", "Admin")], default="s"
            ),
        ),
        migrations.AddConstraint(
            model_name="scheduledtask",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("max_attempts__isnull", True),
                    ("max_attempts__gte", 1),
                    _connector="OR",
                ),
                name="pg_cron_scheduledtask_max_attempts_positive",
            ),
        ),
        migrations.RunSQL(sql=CREATE_FN, reverse_sql=RESTORE_FN),
    ]
