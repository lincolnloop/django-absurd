import logging
import typing as t

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import connections, models

if t.TYPE_CHECKING:
    from django_absurd.backends import AbsurdBackend

from django_absurd.backends import get_declared_queues
from django_absurd.pg_cron import catalog
from django_absurd.pg_cron.choices import RetryKind, Source
from django_absurd.pg_cron.validators import (
    validate_declared_queue,
    validate_name_charset,
    validate_pg_cron_schedule,
)
from django_absurd.queues import get_absurd_backend, resolve_absurd_database
from django_absurd.validators import (
    validate_args_is_list,
    validate_headers_is_object,
    validate_kwargs_is_dict,
    validate_task_path,
)

__all__ = ["ScheduledTask"]

CRON_HELP_TEXT = (
    "A 5-field cron (e.g. '0 2 * * *') or the interval form '<n> seconds' (1-59)."
    " High-frequency schedules (a few seconds) generate a lot of runs, so take care."
    ' See <a href="https://github.com/citusdata/pg_cron" target="_blank"'
    ' rel="noopener">pg_cron</a> for the exact schedule syntax.'
)

logger = logging.getLogger(__name__)


def get_default_max_attempts() -> int:
    """The default retry ceiling for a new schedule — the configured backend's
    DEFAULT_MAX_ATTEMPTS (so it bubbles up), or Absurd's 5 when no backend resolves. The
    max_attempts field default, so an omitted value is bounded; an explicit NULL still
    means "retry forever"."""
    backend = get_absurd_backend()
    return backend.default_max_attempts if backend is not None else 5


def get_declared_queue_choices() -> list[tuple[str, str]]:
    """Declared queues for the configured Absurd backend, sorted, for use as field
    choices. Called at form-render / validation / migration-state time — import-safe.

    With no backend at all the queue is unvalidated (see ScheduledTask.clean), so offer
    'default'. A backend declaring no queues is the other way round — validation rejects
    every name — so offer nothing rather than a choice that cannot be saved.
    """
    backend = get_absurd_backend()
    if backend is None:
        return [("default", "default")]
    return [(q, q) for q in sorted(get_declared_queues(backend))]


class ScheduledTask(models.Model):
    Source = Source

    objects = models.Manager()

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
        # The grammar rule needs no backend — only the queue rule does — so a
        # misconfigured TASKS does not buy a row a free pass on its cron.
        try:
            validate_pg_cron_schedule(self.cron)
        except ValidationError as exc:
            errors["cron"] = exc.messages
        backend = get_absurd_backend()
        if backend is not None:
            errors.update(self.validate_queue_against_backend(backend))

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

    def validate_queue_against_backend(
        self, backend: "AbsurdBackend"
    ) -> dict[str, list[str]]:
        """Validate the effective queue against the single pg_cron backend. Returns
        field errors (empty if OK). The cron grammar is checked in clean(), which needs
        no backend to do it."""
        errors: dict[str, list[str]] = {}
        # Validate the effective queue (explicit override, else the task's own
        # queue_name) against the backend's declared queues.
        try:
            validate_declared_queue(
                self.queue, self.task, set(get_declared_queues(backend))
            )
        except ValidationError as exc:
            errors["queue"] = exc.messages
        return errors

    def schedule_pg_cron_job(self) -> None:
        """(Re)schedule this row's pg_cron job and arm it to its enabled state, via the
        catalog seam. Called by the post_save signal for every write; a no-op when no
        Absurd backend is configured (and, on a test DB, unless pg_cron is opted in)."""
        if get_absurd_backend() is None:
            # Best-effort by design (see signals), so this stays a return rather than a
            # raise — but say it, or the row looks scheduled and never fires. The
            # deploy-time report is absurd.E013.
            logger.warning(
                "pg_cron job not scheduled for '%s': no AbsurdBackend is configured",
                self,
            )
            return
        alias = resolve_absurd_database()
        catalog.schedule_job(
            alias,
            name=self.name,
            source=self.source,
            cron=self.cron,
            command=build_scheduled_command(alias, self.source, self.name),
            active=self.enabled,
        )

    def unschedule_pg_cron_job(self) -> None:
        """Remove this row's pg_cron job via the catalog seam, tolerating an
        already-gone job. Called by the post_delete signal for every deletion; when no
        Absurd backend is configured it warns and returns, symmetric with
        schedule_pg_cron_job — so deletes don't error on a DB without one, but a job
        left firing for a row that no longer exists is not silent."""
        if get_absurd_backend() is None:
            logger.warning(
                "pg_cron job not unscheduled for '%s': no AbsurdBackend is configured;"
                " it keeps firing until the next reconcile",
                self,
            )
            return
        catalog.unschedule_job(
            resolve_absurd_database(), name=self.name, source=self.source
        )


def build_scheduled_command(alias: str, source: str, name: str) -> str:
    """The stored pg_cron command for a schedule's row, built server-side so
    source/name are ``%L`` literal-escaped defense-in-depth — pg_cron executes this
    string as raw SQL later, so quoting matters even though both are already
    charset/enum-constrained. psycopg scans the query for ``%``, hence the ``%%L``."""
    with connections[alias].cursor() as cur:
        cur.execute(
            "select format('select public.django_absurd_run_scheduled(%%L, %%L)', "
            "%s::text, %s::text)",
            [source, name],
        )
        return t.cast("str", cur.fetchone()[0])
