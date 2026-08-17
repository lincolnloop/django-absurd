import logging

import pytest

from django_absurd import cleanup, queues
from django_absurd.management.base import resolve_backend
from django_absurd.test import AbsurdTestRuntime

pytestmark = pytest.mark.django_db(transaction=True)


def test_provisioning_logs_what_it_created(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        result = queues.provision_backend(resolve_backend())

    # Every declared queue is already provisioned by migrate on this reused test
    # database, and settings declare no drift, so nothing is created or reconciled.
    assert result.created == []
    assert result.reconciled == []
    records = [r for r in caplog.records if r.name == "django_absurd.queues"]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert records[0].getMessage() == "queues provisioned: no changes"


def test_cleanup_logs_what_it_removed(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="django_absurd"):
        rows = cleanup.cleanup_queues()

    # No task or event rows exist on any declared queue in this test, so cleanup
    # reports nothing removed from any of them.
    assert rows
    assert all(r["tasks_deleted"] == 0 and r["events_deleted"] == 0 for r in rows)
    records = [r for r in caplog.records if r.name == "django_absurd.cleanup"]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert records[0].getMessage() == "cleanup removed nothing"


def test_the_worker_logs_when_it_stops(
    caplog: pytest.LogCaptureFixture, dj_absurd: AbsurdTestRuntime
) -> None:
    backend = resolve_backend()
    with (
        caplog.at_level(logging.INFO, logger="django_absurd"),
        dj_absurd.freeze_time(),
    ):
        drained = dj_absurd.drain()

    assert drained == []
    messages = [
        r.getMessage() for r in caplog.records if r.name == "django_absurd.worker"
    ]
    started = (
        f'worker started: alias="{backend.alias}" queue="default"'
        f' database="{backend.database}" concurrency=1'
    )
    stopped = (
        f'worker stopped: alias="{backend.alias}" queue="default"'
        f' database="{backend.database}" runs=0'
    )
    assert messages == [started, stopped]
