import pytest
from django.db import connections, transaction

import analysis
from django_absurd.queues import resolve_absurd_database

pytestmark = pytest.mark.django_db(transaction=True)


def test_the_mark_sorts_after_rows_stamped_before_it() -> None:
    """A mark taken after a row must sort after it, transaction or no transaction.

    `enqueue_at` defaults to `absurd.current_time()`, which is `clock_timestamp()` —
    real time, advancing inside a transaction. A mark reading `now()` would be that
    transaction's START, so on a connection already in one it can predate rows written
    before it and admit them to a window meant to exclude them. Read here rather than
    through an enqueue because enqueueing is deferred to commit, which would stamp the
    row after the mark and pass either way.
    """
    with transaction.atomic(using=resolve_absurd_database()):
        with connections[resolve_absurd_database()].cursor() as cursor:
            cursor.execute("select absurd.current_time()")
            stamped = cursor.fetchone()[0]
        mark = analysis.capture_database_now()

    assert mark > stamped
