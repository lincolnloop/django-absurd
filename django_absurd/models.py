import typing as t

from django.db import models

from django_absurd.admin_views import ADMIN_ENTITY_SPECS, build_admin_model
from django_absurd.exceptions import QUEUE_READONLY_MSG, QueueReadOnlyError

__all__ = [
    "QUEUE_READONLY_MSG",
    "Checkpoint",
    "Event",
    "Queue",
    "QueueReadOnlyError",
    "Run",
    "ScheduledJob",
    "Task",
    "Wait",
]


class Queue(models.Model):
    class StorageMode(models.TextChoices):
        UNPARTITIONED = "unpartitioned"
        PARTITIONED = "partitioned"

    class DefaultPartition(models.TextChoices):
        ENABLED = "enabled"
        DISABLED = "disabled"

    class DetachMode(models.TextChoices):
        NONE = "none"
        EMPTY = "empty"

    queue_name = models.TextField(primary_key=True)
    created_at = models.DateTimeField()
    storage_mode = models.TextField(choices=StorageMode.choices)
    default_partition = models.TextField(choices=DefaultPartition.choices)
    partition_lookahead = models.DurationField()
    partition_lookback = models.DurationField()
    cleanup_ttl = models.DurationField()
    cleanup_limit = models.IntegerField()
    detach_mode = models.TextField(choices=DetachMode.choices)
    detach_min_age = models.DurationField()

    class Meta:
        managed = False
        db_table = 'absurd"."queues'

    def __str__(self) -> str:
        return self.queue_name

    def save(self, *args: object, **kwargs: object) -> t.NoReturn:
        raise QueueReadOnlyError(QUEUE_READONLY_MSG)

    def delete(self, *args: object, **kwargs: object) -> t.NoReturn:
        raise QueueReadOnlyError(QUEUE_READONLY_MSG)


Task: type[models.Model] = build_admin_model(
    next(s for s in ADMIN_ENTITY_SPECS if s.name == "tasks")
)
Run: type[models.Model] = build_admin_model(
    next(s for s in ADMIN_ENTITY_SPECS if s.name == "runs")
)
Checkpoint: type[models.Model] = build_admin_model(
    next(s for s in ADMIN_ENTITY_SPECS if s.name == "checkpoints")
)
Event: type[models.Model] = build_admin_model(
    next(s for s in ADMIN_ENTITY_SPECS if s.name == "events")
)
Wait: type[models.Model] = build_admin_model(
    next(s for s in ADMIN_ENTITY_SPECS if s.name == "waits")
)


class ScheduledJob(models.Model):
    class Source(models.TextChoices):
        SETTINGS = "settings"
        ADMIN = "admin"

    name = models.TextField()
    source = models.TextField(choices=Source.choices, default=Source.SETTINGS)
    alias = models.TextField()
    task = models.TextField()
    queue = models.TextField(blank=True, default="")
    params = models.JSONField(default=dict)
    options = models.JSONField(default=dict)
    cron = models.TextField()
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "django_absurd_scheduledjob"
        unique_together = (("source", "alias", "name"),)

    def __str__(self) -> str:
        return f"{self.source}:{self.alias}:{self.name}"
