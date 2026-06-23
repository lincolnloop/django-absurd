from django.db import models


class Payload(models.Model):
    data = models.JSONField()

    class Meta:
        app_label = "tests"

    def __str__(self) -> str:
        return f"Payload({self.pk})"
