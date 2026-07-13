from django.db import models


class Source(models.TextChoices):
    SETTINGS = "settings"
    ADMIN = "admin"
