from django.db import models


class Payload(models.Model):  # noqa: DJ008 — a fixture; nothing renders it
    data = models.JSONField()

    class Meta:
        app_label = "tests"
