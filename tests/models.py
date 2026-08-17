from django.db import models


class Payload(models.Model):  # noqa: DJ008
    data = models.JSONField()

    class Meta:
        app_label = "tests"
