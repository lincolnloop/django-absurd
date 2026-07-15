from django.db import models


class Source(models.TextChoices):
    SETTINGS = "s", "Settings"
    ADMIN = "a", "Admin"
