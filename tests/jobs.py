"""A task that lives OUTSIDE tasks.py — the lazy worker must still run it."""

from django.contrib.auth.models import Group
from django.tasks import task


@task
def record_from_jobs(name: str) -> str:
    Group.objects.get_or_create(name=name)
    return name
