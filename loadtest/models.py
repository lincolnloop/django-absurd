import typing as t

from django.db import models


class ExecutionLog(models.Model):
    """One row per workload-task execution.

    Deliberately unconstrained: a duplicate ``task_id`` means a task ran twice, which
    is exactly what the harness is here to measure. A unique constraint would turn the
    finding into an error at the wrong layer and hide it.
    """

    task_id = models.UUIDField()
    pid = models.IntegerField()
    logged_at = models.DateTimeField(auto_now_add=True)

    # Declared explicitly, not for Django's sake — it would add this manager anyway.
    # django-stubs resolves models through `[tool.django-stubs] django_settings_module`
    # (tests.pg_cron.settings), which does not install `loadtest`, so the plugin adds no
    # implicit `objects` here. Spelling it out is true at both layers.
    objects: t.ClassVar[models.Manager["ExecutionLog"]] = models.Manager()

    class Meta:
        app_label = "loadtest"

    def __str__(self) -> str:
        return f"ExecutionLog({self.pk})"


class OccupancyLog(models.Model):
    """One row per execution, carrying the interval a worker slot was occupied.

    A table of its own rather than two nullable columns on ``ExecutionLog``: the
    ``burn_*`` tasks ``load_drain`` counts could never fill them, and this harness has
    already dropped one permanently-null column from that model. Keeping them apart
    also lets each probe truncate only its own evidence.

    ``started_at`` is stamped on entry to the task and ``finished_at`` at the moment
    the row is built, so the ``INSERT`` itself falls just outside the recorded interval
    while the slot is in fact still held. That understates occupancy by one round trip
    per execution — the same round trip in every arm, so it cancels in a comparison.

    Both stamps come from the executing process's own clock. Workers are children of
    the probe on the one host, so the arms share a clock and the intervals compose
    into a single timeline. Unconstrained for the same reason ``ExecutionLog`` is: a
    repeated ``task_id`` is a redelivery, which is a finding rather than an error.
    """

    task_id = models.UUIDField()
    pid = models.IntegerField()
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField()

    objects: t.ClassVar[models.Manager["OccupancyLog"]] = models.Manager()

    class Meta:
        app_label = "loadtest"

    def __str__(self) -> str:
        return f"OccupancyLog({self.pk})"
