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
