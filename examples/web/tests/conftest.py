import pytest
from app import app


@pytest.fixture(scope="session", autouse=True)
def _prepare_nanodjango() -> None:
    """Finish nanodjango's setup (admin/API routes) once per session — mirrors
    nanodjango's own (unmerged) example-app tests:
    https://github.com/radiac/nanodjango/pull/28."""
    app._prepare()
