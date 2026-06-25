from __future__ import annotations

from django.db import migrations

FORWARD_SQL = """\
DROP VIEW IF EXISTS "absurd"."tasks_view";
CREATE VIEW "absurd"."tasks_view" AS SELECT NULL::text AS queue, NULL::text AS admin_pk, NULL::uuid AS "task_id", NULL::text AS "task_name", NULL::jsonb AS "params", NULL::jsonb AS "headers", NULL::jsonb AS "retry_strategy", NULL::int AS "max_attempts", NULL::jsonb AS "cancellation", NULL::timestamptz AS "enqueue_at", NULL::timestamptz AS "first_started_at", NULL::text AS "state", NULL::int AS "attempts", NULL::uuid AS "last_attempt_run", NULL::jsonb AS "completed_payload", NULL::timestamptz AS "cancelled_at", NULL::text AS "idempotency_key" WHERE false;

DROP VIEW IF EXISTS "absurd"."runs_view";
CREATE VIEW "absurd"."runs_view" AS SELECT NULL::text AS queue, NULL::text AS admin_pk, NULL::uuid AS "run_id", NULL::uuid AS "task_id", NULL::int AS "attempt", NULL::text AS "state", NULL::text AS "claimed_by", NULL::timestamptz AS "claim_expires_at", NULL::timestamptz AS "available_at", NULL::text AS "wake_event", NULL::jsonb AS "event_payload", NULL::timestamptz AS "started_at", NULL::timestamptz AS "completed_at", NULL::timestamptz AS "failed_at", NULL::jsonb AS "result", NULL::jsonb AS "failure_reason", NULL::timestamptz AS "created_at" WHERE false;

DROP VIEW IF EXISTS "absurd"."checkpoints_view";
CREATE VIEW "absurd"."checkpoints_view" AS SELECT NULL::text AS queue, NULL::text AS admin_pk, NULL::uuid AS "task_id", NULL::text AS "checkpoint_name", NULL::jsonb AS "state", NULL::text AS "status", NULL::uuid AS "owner_run_id", NULL::timestamptz AS "updated_at" WHERE false;

DROP VIEW IF EXISTS "absurd"."events_view";
CREATE VIEW "absurd"."events_view" AS SELECT NULL::text AS queue, NULL::text AS admin_pk, NULL::text AS "event_name", NULL::jsonb AS "payload", NULL::timestamptz AS "emitted_at" WHERE false;

DROP VIEW IF EXISTS "absurd"."waits_view";
CREATE VIEW "absurd"."waits_view" AS SELECT NULL::text AS queue, NULL::text AS admin_pk, NULL::uuid AS "task_id", NULL::uuid AS "run_id", NULL::text AS "step_name", NULL::text AS "event_name", NULL::timestamptz AS "timeout_at", NULL::timestamptz AS "created_at" WHERE false;
"""


class Migration(migrations.Migration):
    dependencies = [("django_absurd", "0001_initial_0_4_0")]

    operations = [
        migrations.RunSQL(
            sql=FORWARD_SQL,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
