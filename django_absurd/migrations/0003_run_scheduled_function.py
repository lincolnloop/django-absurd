from django.db import migrations

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
    dependencies = [
        ("django_absurd", "0002_scheduledjob"),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE_FN, reverse_sql=DROP_FN),
    ]
