import typing as t

from django.db import models


class WorkItem(models.Model):
    """The application row a durable task body writes, reads back and clears.

    A table of the harness's own rather than one of Absurd's: a body writing into the
    queue tables would move the very columns every metric here is defined on.
    """

    payload = models.TextField()
    touches = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    # Spelled out because django-stubs' plugin only builds a default manager for a model
    # in its OWN settings module's INSTALLED_APPS, and the harness runs on its own.
    objects: t.ClassVar[models.Manager["WorkItem"]] = models.Manager()

    def __str__(self) -> str:
        return f"work item {self.pk} at touch {self.touches}"
