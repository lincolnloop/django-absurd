import pytest


@pytest.fixture(autouse=True)
def _enable_db(db: None) -> None:
    pass
