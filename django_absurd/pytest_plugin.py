"""django-absurd's ``pytest11`` plugin: auto Absurd cleanup plus the ``dj_absurd``
fixture.

``pytest_configure`` installs ``django_absurd.test.install_absurd_cleanup``, which
patches ``TransactionTestCase._post_teardown`` so every DB-backed test flushes leftover
Absurd state after it runs — no per-test fixture or marker to opt into. The
``dj_absurd`` fixture yields an ``AbsurdTestRuntime`` — the read/drain/clock facade for
durable tests.

**Import-safety constraint**: this module is imported by pytest's own plugin bootstrap
(the ``pytest11`` entry point) before pytest-django configures Django, for EVERY pytest
run in ANY project with django-absurd installed — Django project or not. So every
module imported below, transitively, must be importable with Django UNCONFIGURED.
Reaching ``django_absurd.models`` (or ``.checks``/``.admin``, which do) defines model
classes and raises ``ImproperlyConfigured: Requested setting INSTALLED_APPS`` — an
``INTERNALERROR`` before collection, in every non-Django project in the venv, from one
module-level import.

That constraint has ONE choke point, not a rule spread over this file: the only
model-touching import in the whole graph reachable from here lives inside
``django_absurd.queues.reconcile_queue``'s function body (see the comment there), which
is what keeps ``queues``, ``flush``, ``events`` and ``django_absurd.test`` settings-free
and so importable at module level here. ``tests/core/test_pytest_plugin.py``'s
``test_a_pytest_run_with_no_django_settings_still_collects`` runs a real settings-less
subprocess and is the guard; in-function imports here would add no protection, since
``pytest_configure`` imports ``django_absurd.test`` on every such run regardless.
"""

import typing as t

import pytest
from django.apps import apps
from django.conf import settings
from django.db import connections

from django_absurd import backends
from django_absurd.flush import reset_fake_now
from django_absurd.queues import resolve_absurd_database
from django_absurd.test import AbsurdTestRuntime, install_absurd_cleanup


def pytest_configure(config: pytest.Config) -> None:
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

    Skips via ``resolve_provisioned_alias`` like ``_sweep_stranded_fake_now`` — here
    the unguarded blast radius is a central-catalog DELETE/unschedule scoped by the
    LIVE database name, killing real jobs that share it.
    """
    if not settings.configured or not apps.is_installed(backends.PG_CRON_APP_NAME):
        return
    alias = resolve_provisioned_alias(request)
    if alias is None:
        return
    django_db_blocker = request.getfixturevalue("django_db_blocker")

    # Layering, not import-safety: ``django_absurd.pg_cron`` is an OPTIONAL app, so core
    # never imports it at module level — the ``is_installed`` guard above decides
    # whether it is in play at all.
    from django_absurd.pg_cron import catalog  # noqa: PLC0415

    with django_db_blocker.unblock():
        catalog.flush_database_jobs(alias)


@pytest.fixture(scope="session", autouse=True)
def _sweep_stranded_fake_now(request: pytest.FixtureRequest) -> None:
    """Once per xdist worker, clear an ``absurd.fake_now`` stranded on the reused test
    database by a previous run that died mid-freeze.

    Must run BEFORE any test: the GUC outlives the process that set it and every NEW
    session inherits it, so this run's FIRST draining test can hang on a frozen-past
    wake time before any resetting code executes. Session-autouse, not hung off
    ``dj_absurd`` — a draining test need not use that fixture at all.

    Import-safe in the same shape as ``_sweep_orphaned_pg_cron_jobs``: takes only
    ``request``, guards first, pulls DB fixtures via ``getfixturevalue``. The backend
    guard also keeps a configured-but-Absurd-free project from an ``ALTER DATABASE``
    against its own ``default`` database. ``resolve_provisioned_alias`` skips a
    session that provisioned nothing — unguarded, that sweep would need OWNERSHIP of
    the LIVE database and would error a plain ``pytest tests/test_utils.py`` on
    permissions before a single test ran.
    """
    if not settings.configured or not backends.get_absurd_backends():
        return
    alias = resolve_provisioned_alias(request)
    if alias is None:
        return
    django_db_blocker = request.getfixturevalue("django_db_blocker")

    with django_db_blocker.unblock():
        reset_fake_now(alias)


@pytest.fixture
def dj_absurd() -> t.Iterator[AbsurdTestRuntime]:
    """Read/drain/clock facade for durable tests: snapshot task/run state, drain a
    queue, and freeze Absurd's durable clock with ``freeze_time``.

    Requires ``@pytest.mark.django_db(transaction=True)`` — its reads run on a
    separate connection, so an enqueue left inside an open transaction is invisible to
    it (see ``django_absurd.test.guard_against_open_transaction``).

    A ``freeze_time`` block releases both clock halves on the way out, so the runtime is
    entered for the test's duration purely as the crash net: a test that dies INSIDE the
    block still gets real time restored, function-scoped, before the next test runs. A
    test that never froze releases nothing.
    """
    with AbsurdTestRuntime(alias=resolve_absurd_database()) as runtime:
        yield runtime


def resolve_provisioned_alias(request: pytest.FixtureRequest) -> str | None:
    """The Absurd alias, or ``None`` when this session provisioned no test database
    for it — then ``NAME`` is still the developer's LIVE database, and a sweep there
    is an ``ALTER DATABASE``/catalog-DELETE against production state. pytest-django
    provisions aliases from the whole session's ``django_db`` markers, not from this
    forced setup call, so an unswapped ``NAME`` after forcing ``django_db_setup``
    means nothing was provisioned — and nothing stranded to sweep, by definition.
    """
    alias = resolve_absurd_database()
    unswapped_name = connections[alias].settings_dict["NAME"]
    request.getfixturevalue("django_db_setup")  # force test-DB setup ordering
    if connections[alias].settings_dict["NAME"] == unswapped_name:
        return None
    return alias
