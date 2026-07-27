"""Entry-point regression guard: examples/web is a separate uv project that installs
django-absurd, so it loads the ``pytest11`` plugin at its REAL entry-point position
(no local conftest help). This is the only place ``install_absurd_cleanup()``'s
``TransactionTestCase._post_teardown`` patch is exercised end-to-end from the shipped
entry point.

Deterministic order (no pytest-randomly) makes the cross-test pair honest: test_1
enqueues and commits a row, test_2 asserts the previous test's row was flushed. Both
must be ``transaction=True`` — pytest-django reorders tests by transactionality class
but preserves source order WITHIN a class, so the ``test_1_``/``test_2_`` prefixes keep
this pair in order.

Do NOT add a no-DB "blocked" test here: nanodjango configures Django AFTER
pytest_configure, so pytest-django's DB-blocker is never armed at this entry-point
position — a no-DB test would not raise, which would be a false guarantee. The real
no-DB proof lives in tests/core.
"""

import typing as t

import pytest
from app import add  # app.py's own `add` task

from django_absurd.models import Queue, Task


@pytest.mark.django_db(transaction=True)
def test_1_enqueue_commits_task_row() -> None:
    add.enqueue("2", "3")
    task_model: t.Any = Task
    assert task_model.objects.filter(queue="default").count() == 1


@pytest.mark.django_db(transaction=True)
def test_2_previous_tests_absurd_state_was_flushed() -> None:
    task_model: t.Any = Task
    assert task_model.objects.filter(queue="default").count() == 0
    assert Queue.objects.filter(queue_name="default").exists()  # truncate, not drop


def test_plain_db_enqueue_rides_the_test_transaction(db: None) -> None:
    result = add.enqueue("2", "3")
    assert result.id  # rolled back by Django; hook's skip branch — nothing to flush
