import pytest
from django.core.management import call_command

from django_absurd.models import Queue, QueueReadOnlyError


def test_queue_is_read_only() -> None:
    # Overrides raise before any DB access, so no django_db needed.
    q = Queue(queue_name="x")
    with pytest.raises(QueueReadOnlyError):
        q.save()
    with pytest.raises(QueueReadOnlyError):
        q.delete()


def test_queue_str_is_the_queue_name() -> None:
    assert str(Queue(queue_name="reports")) == "reports"


def test_queue_table_and_choices() -> None:
    assert Queue._meta.db_table == 'absurd"."queues'
    assert Queue._meta.managed is False
    assert set(Queue.StorageMode.values) == {"unpartitioned", "partitioned"}


@pytest.mark.django_db(databases=["default", "sqlite"])
def test_no_pending_migrations_for_app() -> None:
    # CreateModel op lives in 0001 — makemigrations sees no changes.
    call_command("makemigrations", "django_absurd", check=True, dry_run=True)
