import pytest

from tests import tasks


@pytest.fixture(autouse=True)
def _enable_db() -> None:
    """Module-level override of the suite autouse fixture: NO db here."""


def test_absurd_access_blocked_without_db() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        tasks.add.enqueue(1, 2)
    assert str(excinfo.value) == (
        'Database access not allowed, use the "django_db" mark, or the "db" or '
        '"transactional_db" fixtures to enable it.'
    )
