from django.db import models


class Source(models.TextChoices):
    # Values are single letters to keep the pg_cron jobname
    # (absurd:<source>:<alias>:<name>) short against its 63-byte budget; the labels stay
    # readable in the admin.
    SETTINGS = "s", "Settings"
    ADMIN = "a", "Admin"
