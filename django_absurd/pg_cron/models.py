from django.core.exceptions import ValidationError
from django.db import models

from django_absurd.backends import get_absurd_backends, get_declared_queues
from django_absurd.pg_cron.validators import (
    validate_alias_charset,
    validate_alias_is_pg_cron_backend,
    validate_declared_queue,
    validate_jobname_length,
    validate_name_charset,
    validate_no_cross_source_clash,
)
from django_absurd.validators import validate_task_path

__all__ = ["ScheduledTask"]


class ScheduledTask(models.Model):
    class Source(models.TextChoices):
        SETTINGS = "settings"
        ADMIN = "admin"

    name = models.TextField(validators=[validate_name_charset])
    source = models.TextField(choices=Source.choices, default=Source.SETTINGS)
    alias = models.TextField(validators=[validate_alias_charset])
    task = models.TextField(validators=[validate_task_path])
    queue = models.TextField(blank=True, default="")
    # JSONField.validate raises its own "invalid" error before run_validators, so
    # the shared serializability message is aligned via error_messages (matching the
    # check path's validate_args_serializable / validate_kwargs_serializable text).
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
    max_attempts = models.IntegerField(null=True, blank=True)
    retry_strategy = models.JSONField(null=True, blank=True)
    headers = models.JSONField(null=True, blank=True)
    cancellation = models.JSONField(null=True, blank=True)
    idempotency_key = models.TextField(blank=True, default="")
    cron = models.TextField()
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Explicit app_label so this module stays importable even when
        # django_absurd.pg_cron is not in INSTALLED_APPS.
        app_label = "django_absurd_pg_cron"
        db_table = "django_absurd_scheduledtask"
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
            validate_no_cross_source_clash(self.source, self.alias, self.name)
        except ValidationError as exc:
            errors.setdefault("name", []).extend(exc.messages)

        try:
            validate_alias_is_pg_cron_backend(self.alias)
        except ValidationError as exc:
            errors["alias"] = exc.messages
        else:
            backend = get_absurd_backends()[self.alias]
            try:
                validate_declared_queue(
                    self.queue, self.task, set(get_declared_queues(backend))
                )
            except ValidationError as exc:
                errors["queue"] = exc.messages

        if errors:
            raise ValidationError(errors)
