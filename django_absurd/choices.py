from django.db import models


class TaskState(models.TextChoices):
    """The states a task or run row can hold.

    Mirrors the CHECK on ``t_<queue>.state`` and ``r_<queue>.state``,
    ``django_absurd/migrations/0001_initial_0_5_0.sql:233`` and ``:250``. Each label
    repeats its value because a bare member would title-case it, and the admin shows
    the string Postgres stores.
    """

    PENDING = "pending", "pending"
    RUNNING = "running", "running"
    SLEEPING = "sleeping", "sleeping"
    COMPLETED = "completed", "completed"
    FAILED = "failed", "failed"
    CANCELLED = "cancelled", "cancelled"
