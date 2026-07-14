import typing as t
from contextlib import contextmanager

from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.core.validators import MinValueValidator
from django.db import DatabaseError, InternalError, connections, models, transaction

from django_absurd.backends import get_absurd_backends, get_declared_queues
from django_absurd.pg_cron.choices import Source
from django_absurd.pg_cron.validators import (
    build_jobname,
    build_jobname_prefix,
    validate_alias_charset,
    validate_alias_is_pg_cron_backend,
    validate_declared_queue,
    validate_jobname_length,
    validate_name_charset,
    validate_pg_cron_cron,
)
from django_absurd.queues import resolve_absurd_database
from django_absurd.validators import validate_task_path

__all__ = ["ScheduledTask"]

# Advisory lock key serializing concurrent pg_cron job writers.
SYNC_CRONS_ADVISORY_LOCK = 0x616273_75726421  # "absurd!" as hex


class PgCronManager(models.Manager):
    """The pg_cron catalog (``cron.job``) operations for these schedules, kept off
    ``objects`` (which queries the ScheduledTask table). Every method defaults to the
    single absurd database (``resolve_absurd_database()``); pass ``database`` only to
    reuse an already-resolved value.
    """

    def get_job(self, alias: str, name: str, source: str) -> tuple | None:
        """The ``(jobname, schedule, command, active)`` row for one schedule's job."""
        with connections[resolve_absurd_database()].cursor() as cur:
            cur.execute(
                "select jobname, schedule, command, active from cron.job "
                "where jobname = %s",
                [build_jobname(alias, name, source)],
            )
            return cur.fetchone()

    def get_managed_jobs(self, source: str | None = None) -> list[tuple]:
        """The ``(jobname, schedule, command, active)`` rows for every job we manage
        (all share the ``absurd:`` prefix), across aliases. Pass source to narrow to one
        lane (``absurd:<source>:``)."""
        prefix = f"absurd:{source}:" if source is not None else "absurd:"
        with connections[resolve_absurd_database()].cursor() as cur:
            cur.execute(
                "select jobname, schedule, command, active from cron.job "
                "where starts_with(jobname, %s) order by jobname",
                [prefix],
            )
            return cur.fetchall()

    def unschedule_matching(
        self, alias: str, source: str, database: str | None = None
    ) -> None:
        """Unschedule every pg_cron job owned by one backend + source
        (``absurd:<source>:<alias>:%``). Scoped to that exact prefix so tearing down one
        backend's lane never touches another backend's jobs."""
        with open_locked_cursor(database or resolve_absurd_database()) as cur:
            cur.execute(
                "select jobid from cron.job where starts_with(jobname, %s)",
                [build_jobname_prefix(alias, source=source)],
            )
            prune_pg_cron_jobs(cur, [jobid for (jobid,) in cur.fetchall()])

    def prune_jobs_without_rows(
        self,
        alias: str,
        source: str,
        keep_names: list[str],
        database: str | None = None,
    ) -> None:
        """Unschedule owned jobs for a backend + source (``absurd:<source>:<alias>:%``)
        whose name isn't in keep_names — i.e. jobs with no backing row. Row deletion
        unschedules its own job via post_delete, but a row removed by a signal-less path
        (bulk delete, ``flush``, raw SQL) leaves its job orphaned — reconcile heals it
        so ``cron.job`` reconverges to the rows."""
        keep = {build_jobname(alias, name, source) for name in keep_names}
        with open_locked_cursor(database or resolve_absurd_database()) as cur:
            cur.execute(
                "select jobid, jobname from cron.job where starts_with(jobname, %s)",
                [build_jobname_prefix(alias, source=source)],
            )
            stale = [jobid for jobid, jobname in cur.fetchall() if jobname not in keep]
            prune_pg_cron_jobs(cur, stale)


class ScheduledTask(models.Model):
    Source = Source

    objects = models.Manager()
    pg_cron = PgCronManager()

    name = models.TextField(validators=[validate_name_charset])
    source = models.TextField(choices=Source.choices, default=Source.SETTINGS)
    alias = models.TextField(validators=[validate_alias_charset])
    task = models.TextField(validators=[validate_task_path])
    queue = models.TextField(blank=True, default="")
    # JSONField.validate raises "invalid" before run_validators, so the shared
    # serializability message is set via error_messages (matching the check path).
    args = models.JSONField(
        default=list,
        blank=True,
        error_messages={"invalid": "args is not JSON-serializable."},
    )
    kwargs = models.JSONField(
        default=dict,
        blank=True,
        error_messages={"invalid": "kwargs is not JSON-serializable."},
    )
    # Not set → 5 (mirrors Absurd's default retry ceiling). Explicit NULL is allowed and
    # means "retry forever" (Absurd's fail_run treats NULL max_attempts as unbounded) —
    # a deliberate opt-in, not the default. Must be >= 1 when set (Absurd rejects < 1).
    max_attempts = models.IntegerField(
        default=5, null=True, blank=True, validators=[MinValueValidator(1)]
    )
    retry_strategy = models.JSONField(null=True, blank=True)
    headers = models.JSONField(null=True, blank=True)
    cancellation = models.JSONField(null=True, blank=True)
    idempotency_key = models.TextField(blank=True, default="")
    cron = models.TextField()
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Explicit app_label so this module imports even when pg_cron isn't installed.
        app_label = "django_absurd_pg_cron"
        db_table = "django_absurd_scheduledtask"
        # A settings and an admin schedule may share a name — they are distinct,
        # source-namespaced jobs — so uniqueness includes source.
        unique_together = (("source", "alias", "name"),)

    def __str__(self) -> str:
        return f"{self.source}:{self.alias}:{self.name}"

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        try:
            validate_jobname_length(self.source, self.alias, self.name)
        except ValidationError as exc:
            errors["name"] = exc.messages

        try:
            validate_alias_is_pg_cron_backend(self.alias)
        except ValidationError as exc:
            # NON_FIELD_ERRORS, not "alias": alias is read-only on the change form, so
            # a field-keyed error there would raise "no field named alias" (HTTP 500).
            errors[NON_FIELD_ERRORS] = exc.messages
        else:
            backend = get_absurd_backends()[self.alias]
            try:
                validate_declared_queue(
                    self.queue, self.task, set(get_declared_queues(backend))
                )
            except ValidationError as exc:
                errors["queue"] = exc.messages
            try:
                validate_pg_cron_cron(self.cron, backend.database)
            except ValidationError as exc:
                errors["cron"] = exc.messages

        if errors:
            raise ValidationError(errors)

    def get_pg_cron_job(self) -> tuple | None:
        """This row's own pg_cron job as ``(jobname, schedule, command, active)``, or
        None if it isn't scheduled. (The manager lives on the class — Django managers
        aren't accessible via instances.)"""
        return ScheduledTask.pg_cron.get_job(self.alias, self.name, self.source)

    def schedule_pg_cron_job(self) -> None:
        """(Re)schedule this row's pg_cron job (``absurd:<source>:<alias>:<name>``) and
        arm it to its enabled state. Called by the post_save signal for every write; a
        no-op when the alias isn't a pg_cron backend."""
        if resolve_pg_cron_backend(self) is None:
            return
        jobname = build_jobname(self.alias, self.name, self.source)
        # cron.schedule is a pg_cron catalog function (not the Absurd SDK). psycopg
        # scans the query for %, so format()'s %L are doubled to %%L and the params
        # carry a ::text cast — building the command server-side blocks arg injection.
        with open_locked_cursor(resolve_absurd_database()) as cur:
            cur.execute(
                "select cron.schedule(%s, %s, "
                "format('select public.django_absurd_run_scheduled(%%L, %%L, %%L)', "
                "%s::text, %s::text, %s::text))",
                [jobname, self.cron, self.source, self.alias, self.name],
            )
            jobid = cur.fetchone()[0]
            cur.execute(
                "select cron.alter_job(%s, active := %s)", [jobid, self.enabled]
            )

    def unschedule_pg_cron_job(self) -> None:
        """Remove this row's pg_cron job, tolerating an already-gone job. Called by the
        post_delete signal for every deletion; a no-op when the alias isn't a pg_cron
        backend (symmetric with schedule_pg_cron_job) — so a non-pg_cron row never
        touches the cron catalog; deletes don't error on a DB without pg_cron."""
        if resolve_pg_cron_backend(self) is None:
            return
        jobname = build_jobname(self.alias, self.name, self.source)
        with open_locked_cursor(resolve_absurd_database()) as cur:
            cur.execute("select jobid from cron.job where jobname = %s", [jobname])
            prune_pg_cron_jobs(cur, [jobid for (jobid,) in cur.fetchall()])


def resolve_pg_cron_backend(scheduled_task: ScheduledTask) -> t.Any:
    """The pg_cron backend for a schedule's alias, or None when the alias is not a
    configured pg_cron backend — nothing to schedule for such a row."""
    backend = get_absurd_backends().get(scheduled_task.alias)
    if backend is None or backend.scheduler != "pg_cron":
        return None
    return backend


@contextmanager
def open_locked_cursor(database: str) -> t.Iterator[t.Any]:
    """A cursor on ``database`` inside a transaction holding the shared advisory lock,
    so concurrent pg_cron job writers serialise."""
    with transaction.atomic(using=database), connections[database].cursor() as cur:
        cur.execute("select pg_advisory_xact_lock(%s)", [SYNC_CRONS_ADVISORY_LOCK])
        yield cur


def prune_pg_cron_jobs(cur: t.Any, stale_jobids: list[int]) -> None:
    """Unschedule each jobid, tolerating already-removed rows.

    Each unschedule runs inside its own savepoint: if the cron.job row was removed
    out-of-band, cron.unschedule raises InternalError (SQLSTATE XX000); we roll back
    to the savepoint and continue. Matched on SQLSTATE (not message) for lc_messages.
    """
    for jobid in stale_jobids:
        cur.execute("savepoint prune_sp")
        try:
            cur.execute("select cron.unschedule(%s)", [jobid])
        except (InternalError, DatabaseError) as exc:
            if getattr(exc.__cause__, "sqlstate", None) != "XX000":
                raise
            cur.execute("rollback to savepoint prune_sp")
        else:
            cur.execute("release savepoint prune_sp")
