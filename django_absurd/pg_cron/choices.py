from django.db import models


class Source(models.TextChoices):
    SETTINGS = "s", "Settings"
    ADMIN = "a", "Admin"


class RetryKind(models.TextChoices):
    EXPONENTIAL = "exponential", "Exponential"
    FIXED = "fixed", "Fixed"
    NONE = "none", "None"
