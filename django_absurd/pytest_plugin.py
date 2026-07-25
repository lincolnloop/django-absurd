"""django-absurd's ``pytest11`` plugin: auto Absurd cleanup + ``absurd_drain_queue``.

``pytest_configure`` installs ``django_absurd.test.install_absurd_cleanup``, which
patches ``TransactionTestCase._post_teardown`` so every DB-backed test flushes leftover
Absurd state after it runs — no per-test fixture or marker to opt into. The
``absurd_drain_queue`` fixture burst-drains a queue synchronously in-process.

**Import-safety constraint**: this module is imported by pytest's own plugin bootstrap
(the ``pytest11`` entry point) before pytest-django configures Django, for EVERY pytest
run in ANY project with django-absurd installed — Django project or not. A top-level
``django``/``django_absurd`` import would chain into model definitions and raise
``AppRegistryNotReady``/``ImproperlyConfigured`` outright. So only ``typing`` and
``pytest`` are imported at module level; everything Django-touching is imported lazily
inside the function bodies. ``pytest_configure`` therefore imports ``django.test`` (via
``django_absurd.test``) on every pytest run in any venv with django-absurd installed —
an import-safe, verified pre-configuration step, but a small universal startup cost.
"""

import typing as t

import pytest


def pytest_configure(config: pytest.Config) -> None:
    from django_absurd.test import install_absurd_cleanup  # noqa: PLC0415

    install_absurd_cleanup()


@pytest.fixture(scope="session", autouse=True)
def _sweep_orphaned_pg_cron_jobs(request: pytest.FixtureRequest) -> None:
    """Once per xdist worker, clear crash-orphaned django-absurd ``_dj:`` jobs left on
    the reused test-DB name in the shared central catalog.

    ``--reuse-db`` keeps the test database between runs, so a run that crashed mid-test
    (before its ``_post_teardown`` flush) can leave the previous run's jobs bound to
    this DB name still scheduled. This session-autouse start-sweep clears them once,
    before any test runs.

    Import-safe: takes ONLY ``request`` so the guard runs BEFORE any Django/DB fixture
    is resolved. Pytest resolves declared fixture parameters before the body runs, so
    declaring ``django_db_setup``/``django_db_blocker`` as parameters would defeat the
    guard — in a project that has django-absurd installed but isn't a fully-configured
    Django + pytest-django stack, resolving ``django_db_setup`` either skips the whole
    session (Django unconfigured) or errors (pytest-django absent). Guarding first, then
    pulling the DB fixtures lazily via ``getfixturevalue`` keeps this a clean no-op for
    a non-Django or unconfigured project. When the pg_cron app is present but scheduling
    is inert (opt-in off), ``flush_database_jobs`` itself no-ops, so it's free too.
    """
    from django.apps import apps  # noqa: PLC0415
    from django.conf import settings  # noqa: PLC0415

    from django_absurd.backends import PG_CRON_APP_NAME  # noqa: PLC0415

    if not settings.configured or not apps.is_installed(PG_CRON_APP_NAME):
        return

    request.getfixturevalue("django_db_setup")  # force test-DB setup ordering
    django_db_blocker = request.getfixturevalue("django_db_blocker")

    from django_absurd.pg_cron import catalog  # noqa: PLC0415
    from django_absurd.queues import resolve_absurd_database  # noqa: PLC0415

    with django_db_blocker.unblock():
        catalog.flush_database_jobs(resolve_absurd_database())


@pytest.fixture
def absurd_drain_queue() -> t.Callable[..., None]:
    """Burst-drain a queue synchronously, in-process.

    Returns a callable ``drain(queue: str = "default", *, concurrency: int = 1) ->
    None`` that runs every currently-claimable task on the given queue to completion,
    then returns — no persistent worker process, no subprocess.
    """

    def drain(queue: str = "default", *, concurrency: int = 1) -> None:
        from django_absurd.worker import (  # noqa: PLC0415
            WorkerOptions,
            run_burst_worker,
        )

        run_burst_worker(queue, options=WorkerOptions(concurrency=concurrency))

    return drain
