import pytest

from tests.core.test_admin.utils import register_admin


@pytest.fixture(autouse=True)
def _register_admin() -> None:
    # Register the Absurd models on the default admin site and refresh URL
    # resolution so the per-model admin URLs reverse in every test.
    register_admin()
