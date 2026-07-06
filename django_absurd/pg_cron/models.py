from django.db import models

__all__ = ["ScheduledJob"]


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
        # Explicit app_label so this module stays importable even when
        # django_absurd.pg_cron is not in INSTALLED_APPS.
        app_label = "django_absurd_pg_cron"
        db_table = "django_absurd_scheduledjob"
        unique_together = (("source", "alias", "name"),)

    def __str__(self) -> str:
        return f"{self.source}:{self.alias}:{self.name}"
