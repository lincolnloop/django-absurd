import typing as t

from django.db import models

from django_absurd.admin_views import ADMIN_ENTITY_SPECS, EntitySpec, build_admin_model
from django_absurd.exceptions import QUEUE_READONLY_MSG, QueueReadOnlyError

__all__ = [
    "QUEUE_READONLY_MSG",
    "Checkpoint",
    "Event",
    "Queue",
    "QueueReadOnlyError",
    "Run",
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


def find_spec(name: str) -> EntitySpec:
    return next(s for s in ADMIN_ENTITY_SPECS if s.name == name)


Task: type[models.Model] = build_admin_model(find_spec("tasks"))
Run: type[models.Model] = build_admin_model(find_spec("runs"))
Checkpoint: type[models.Model] = build_admin_model(find_spec("checkpoints"))
Event: type[models.Model] = build_admin_model(find_spec("events"))
Wait: type[models.Model] = build_admin_model(find_spec("waits"))
