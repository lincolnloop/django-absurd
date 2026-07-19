import typing as t
from contextlib import contextmanager

from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.core.validators import MinValueValidator
from django.db import DatabaseError, InternalError, connections, models, transaction

if t.TYPE_CHECKING:
    from django.db.backends.utils import CursorWrapper

    from django_absurd.backends import AbsurdBackend

from django_absurd.backends import get_declared_queues
from django_absurd.pg_cron.choices import RetryKind, Source
from django_absurd.pg_cron.validators import (
    build_jobname,
    build_jobname_prefix,
    validate_declared_queue,
    validate_jobname_length,
    validate_name_charset,
    validate_pg_cron_cron,
)
from django_absurd.queues import get_absurd_backend, resolve_absurd_database
from django_absurd.validators import (
    validate_args_is_list,
    validate_headers_is_object,
    validate_kwargs_is_dict,
    validate_task_path,
)

__all__ = ["ScheduledTask"]

PgCronJobRow = tuple[str, str, str, bool]
"""A ``(jobname, schedule, command, active)`` row from ``cron.job``."""

CRON_HELP_TEXT = (
    "A 5-field cron (e.g. '0 2 * * *') or the interval form '<n> seconds' (1-59)."
    " High-frequency schedules (a few seconds) generate a lot of runs, so take care."
    ' See <a href="https://github.com/citusdata/pg_cron" target="_blank"'
    ' rel="noopener">pg_cron</a> for the exact schedule syntax.'
)


def get_default_max_attempts() -> int:
    """The default retry ceiling for a new schedule — the configured backend's
    DEFAULT_MAX_ATTEMPTS (so it bubbles up), or Absurd's 5 when no backend resolves. The
    max_attempts field default, so an omitted value is bounded; an explicit NULL still
    means "retry forever"."""
    backend = get_absurd_backend()
    return backend.default_max_attempts if backend is not None else 5


def get_declared_queue_choices() -> list[tuple[str, str]]:
    """Declared queues for the configured Absurd backend, sorted, for use as field
    choices. Falls back to [("default", "default")] when no queues are declared.
    Called at form-render / validation / migration-state time — import-safe."""
    backend = get_absurd_backend()
    if backend is None:
        return [("default", "default")]
    queues = set(get_declared_queues(backend))
    if not queues:
        return [("default", "default")]
    return [(q, q) for q in sorted(queues)]


class PgCronManager(models.Manager["ScheduledTask"]):
    """The pg_cron catalog (``cron.job``) operations for these schedules, kept off
    ``objects`` (which queries the ScheduledTask table). Every method defaults to the
    single absurd database (``resolve_absurd_database()``); pass ``database`` only to
    reuse an already-resolved value.
    """

    def get_job(self, name: str, source: str) -> PgCronJobRow | None:
        """The ``(jobname, schedule, command, active)`` row for one schedule's job."""
        with connections[resolve_absurd_database()].cursor() as cur:
            cur.execute(
                "select jobname, schedule, command, active from cron.job "
                "where jobname = %s",
                [build_jobname(name, source)],
            )
            job: PgCronJobRow | None = cur.fetchone()
            return job

    def get_managed_jobs(self, source: str | None = None) -> list[PgCronJobRow]:
        """The ``(jobname, schedule, command, active)`` rows for every job we manage
        (all share the ``_dj:`` prefix). Pass source to narrow to one lane
        (``_dj:<source>:``)."""
        prefix = f"_dj:{source}:" if source is not None else "_dj:"
        with connections[resolve_absurd_database()].cursor() as cur:
            cur.execute(
                "select jobname, schedule, command, active from cron.job "
                "where starts_with(jobname, %s) order by jobname",
                [prefix],
            )
            jobs: list[PgCronJobRow] = cur.fetchall()
            return jobs

    def unschedule_matching(self, source: str, database: str | None = None) -> None:
        """Unschedule every pg_cron job owned by one source lane
        (``_dj:<source>:%``). Scoped to that exact prefix so tearing down one lane
        never touches another lane's jobs."""
        with open_locked_cursor(database or resolve_absurd_database()) as cur:
            cur.execute(
                "select jobid from cron.job where starts_with(jobname, %s)",
                [build_jobname_prefix(source=source)],
            )
            prune_pg_cron_jobs(cur, [jobid for (jobid,) in cur.fetchall()])

    def prune_jobs_without_rows(
        self,
        source: str,
        keep_names: list[str],
        database: str | None = None,
    ) -> None:
        """Unschedule owned jobs for a source lane (``_dj:<source>:%``) whose name
        isn't in keep_names — i.e. jobs with no backing row. Row deletion unschedules
        its own job via post_delete, but a row removed by a signal-less path (bulk
        delete, ``flush``, raw SQL) leaves its job orphaned — reconcile heals it so
        ``cron.job`` reconverges to the rows."""
        keep = {build_jobname(name, source) for name in keep_names}
        with open_locked_cursor(database or resolve_absurd_database()) as cur:
            cur.execute(
                "select jobid, jobname from cron.job where starts_with(jobname, %s)",
                [build_jobname_prefix(source=source)],
            )
            stale = [jobid for jobid, jobname in cur.fetchall() if jobname not in keep]
            prune_pg_cron_jobs(cur, stale)


class ScheduledTask(models.Model):
    Source = Source

    objects = models.Manager()
    pg_cron = PgCronManager()

    name = models.CharField(validators=[validate_name_charset])
    source = models.CharField(choices=Source.choices, default=Source.SETTINGS)
    task = models.CharField(validators=[validate_task_path])
    queue = models.CharField(choices=get_declared_queue_choices)
    # JSONField.validate raises "invalid" before run_validators, so the shared
    # serializability message is set via error_messages (matching the check path).
    args = models.JSONField(
        default=list,
        blank=True,
        validators=[validate_args_is_list],
        error_messages={"invalid": "args is not JSON-serializable."},
    )
    kwargs = models.JSONField(
        default=dict,
        blank=True,
        validators=[validate_kwargs_is_dict],
        error_messages={"invalid": "kwargs is not JSON-serializable."},
    )
    # Unset defaults to the backend's DEFAULT_MAX_ATTEMPTS (bubbles up via
    # get_default_max_attempts). Explicit NULL is allowed and means "retry forever"
    # (fail_run treats NULL as unbounded) -- a deliberate opt-in, not the default.
    # Must be >= 1 when set (Absurd rejects < 1).
    max_attempts = models.IntegerField(
        default=get_default_max_attempts,
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
    )
    retry_kind = models.CharField(choices=RetryKind.choices, blank=True, default="")
    retry_base_seconds = models.FloatField(null=True, blank=True)
    retry_factor = models.FloatField(null=True, blank=True)
    retry_max_seconds = models.FloatField(null=True, blank=True)
    headers = models.JSONField(
        null=True, blank=True, validators=[validate_headers_is_object]
    )
    cancellation_max_duration = models.IntegerField(null=True, blank=True)
    cancellation_max_delay = models.IntegerField(null=True, blank=True)
    idempotency_key = models.CharField(blank=True, default="")
    cron = models.CharField(help_text=CRON_HELP_TEXT)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Explicit app_label so this module imports even when pg_cron isn't installed.
        app_label = "django_absurd_pg_cron"
        db_table = "django_absurd_scheduledtask"
        # A settings and an admin schedule may share a name — they are distinct,
        # source-namespaced jobs — so uniqueness includes source.
        unique_together = (("source", "name"),)
        # DB-level guarantee that max_attempts is a positive integer (>= 1) when set —
        # NULL stays allowed (means "retry forever"). Absurd's spawn_task rejects < 1;
        # this catches writes that bypass full_clean (bulk_create, raw SQL).
        constraints = [
            models.CheckConstraint(
                condition=models.Q(max_attempts__isnull=True)
                | models.Q(max_attempts__gte=1),
                name="pg_cron_scheduledtask_max_attempts_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.source}:{self.name}"

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        # NON_FIELD_ERRORS, not "name": it is read-only on the change form, so a
        # field-keyed error there would raise "no field named ..." (HTTP 500). The
        # jobname length is a composite (source:name) rule anyway.
        try:
            validate_jobname_length(self.source, self.name)
        except ValidationError as exc:
            errors.setdefault(NON_FIELD_ERRORS, []).extend(exc.messages)

        backend = get_absurd_backend()
        if backend is not None:
            errors.update(self.validate_against_backend(backend))

        retry_timing_fields = (
            "retry_base_seconds",
            "retry_factor",
            "retry_max_seconds",
        )
        if not self.retry_kind and any(
            getattr(self, field) is not None for field in retry_timing_fields
        ):
            errors.setdefault("retry_kind", []).append(
                "Set a retry kind to configure retry timing."
            )

        if errors:
            raise ValidationError(errors)

    def validate_against_backend(
        self, backend: "AbsurdBackend"
    ) -> dict[str, list[str]]:
        """Validate queue + cron against the single pg_cron backend. Returns field
        errors (empty if OK)."""
        errors: dict[str, list[str]] = {}
        # Validate the effective queue (explicit override, else the task's own
        # queue_name) against the backend's declared queues.
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
        return errors

    def get_pg_cron_job(self) -> PgCronJobRow | None:
        """This row's own pg_cron job as ``(jobname, schedule, command, active)``, or
        None if it isn't scheduled. (The manager lives on the class — Django managers
        aren't accessible via instances.)"""
        return ScheduledTask.pg_cron.get_job(self.name, self.source)

    def schedule_pg_cron_job(self) -> None:
        """(Re)schedule this row's pg_cron job (``_dj:<source>:<name>``) and arm it to
        its enabled state. Called by the post_save signal for every write; a no-op when
        no Absurd backend is configured."""
        if get_absurd_backend() is None:
            return
        jobname = build_jobname(self.name, self.source)
        # cron.schedule is a pg_cron catalog function (not the Absurd SDK). psycopg
        # scans the query for %, so format()'s %L are doubled to %%L and the params
        # carry a ::text cast — building the command server-side blocks arg injection.
        with open_locked_cursor(resolve_absurd_database()) as cur:
            cur.execute(
                "select cron.schedule(%s, %s, "
                "format('select public.django_absurd_run_scheduled(%%L, %%L)', "
                "%s::text, %s::text))",
                [jobname, self.cron, self.source, self.name],
            )
            jobid = cur.fetchone()[0]
            cur.execute(
                "select cron.alter_job(%s, active := %s)", [jobid, self.enabled]
            )

    def unschedule_pg_cron_job(self) -> None:
        """Remove this row's pg_cron job, tolerating an already-gone job. Called by the
        post_delete signal for every deletion; a no-op when no Absurd backend is
        configured (symmetric with schedule_pg_cron_job) — so deletes don't error on a
        DB without one."""
        if get_absurd_backend() is None:
            return
        jobname = build_jobname(self.name, self.source)
        with open_locked_cursor(resolve_absurd_database()) as cur:
            cur.execute("select jobid from cron.job where jobname = %s", [jobname])
            prune_pg_cron_jobs(cur, [jobid for (jobid,) in cur.fetchall()])


@contextmanager
def open_locked_cursor(database: str) -> t.Iterator["CursorWrapper"]:
    """A cursor on ``database`` inside a transaction holding the shared advisory lock,
    so concurrent pg_cron job writers serialise."""
    advisory_lock_key = 0x616273_75726421  # "absurd!" as hex
    with transaction.atomic(using=database), connections[database].cursor() as cur:
        cur.execute("select pg_advisory_xact_lock(%s)", [advisory_lock_key])
        yield cur


def prune_pg_cron_jobs(cur: "CursorWrapper", stale_jobids: list[int]) -> None:
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
