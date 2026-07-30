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

    Same unprovisioned-session guard as ``_sweep_stranded_fake_now``, in the same
    shape: forcing ``django_db_setup`` costs nothing beyond ordering — it builds only
    the aliases the COLLECTED tests need, so a session with no DB-backed test in it
    builds nothing FOR THIS ALIAS, and ``NAME`` stays whatever the developer
    configured — the live database, never swapped to a test one. This sweep's blast
    radius there isn't an ``ALTER DATABASE`` ownership failure but a central-catalog
    ``DELETE``/unschedule scoped by ``database = <live NAME>``: sweeping in that case
    would unschedule and delete run history for real jobs that happen to share that
    live database name. So this compares ``NAME`` before and after forcing setup and
    skips silently — a session that provisions nothing for this alias has nothing
    orphaned to sweep, by definition.
    """
    if not settings.configured or not apps.is_installed(backends.PG_CRON_APP_NAME):
        return

    alias = resolve_absurd_database()
    unswapped_name = connections[alias].settings_dict["NAME"]

    request.getfixturevalue("django_db_setup")  # force test-DB setup ordering

    if connections[alias].settings_dict["NAME"] == unswapped_name:
        return  # nothing was provisioned for this alias; nothing orphaned to sweep

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

    Recovery has to happen BEFORE any test, not only after one. The per-test clock
    teardown and the post-test flush are both scheduled after a test body, so neither
    protects the first casualty of a poisoned ``--reuse-db`` database: the GUC outlives
    the process that set it, every NEW session inherits it, and so this run's first
    draining test can hang on a task whose wake time is already in the frozen past —
    before any resetting code executes.

    Session-autouse, not lazily hung off the ``dj_absurd`` fixture, and the difference
    is load-bearing: a draining test need not use that fixture at all (one that calls
    the worker directly hangs on the same stranded clock), so only a scope covering the
    whole session covers every casualty. Forcing ``django_db_setup`` costs nothing
    beyond ordering — it builds only the aliases the COLLECTED tests need, so a session
    with no DB-backed test in it builds nothing FOR THIS ALIAS: pytest-django computes
    which aliases to provision from the ``django_db`` markers across the WHOLE
    collected session, not from this fixture forcing the call, so ``NAME`` stays
    whatever the developer configured — the live database, never swapped to a test
    one. Sweeping in that case would run ``ALTER DATABASE`` against that live database,
    and ``ALTER DATABASE`` generally needs OWNERSHIP of the target — a privilege the
    application role commonly lacks on managed Postgres. Left unguarded, a plain
    ``pytest tests/test_utils.py`` in a project that merely has an Absurd backend
    configured, with no ``django_db``-marked test in the file, would error the whole
    session on a permissions failure before a single test ran. So this compares
    ``NAME`` before and after forcing setup and skips silently — a session that
    provisions nothing for this alias has nothing stranded to sweep, by definition.

    Import-safe in the same shape as ``_sweep_orphaned_pg_cron_jobs``: takes ONLY
    ``request`` so the guard runs BEFORE any Django/DB fixture is resolved. In a project
    that isn't a fully-configured Django + pytest-django stack, resolving
    ``django_db_setup`` either skips the whole session or errors; in one that is
    configured but has no Absurd backend, there is no Absurd database, and the resolver
    would fall back to ``default`` and issue an ``ALTER DATABASE`` against a project
    database that has nothing to do with us. Guarding first, then pulling the DB
    fixtures lazily via ``getfixturevalue``, keeps both cases a clean no-op.
    """
    if not settings.configured or not backends.get_absurd_backends():
        return

    alias = resolve_absurd_database()
    unswapped_name = connections[alias].settings_dict["NAME"]

    request.getfixturevalue("django_db_setup")  # force test-DB setup ordering

    if connections[alias].settings_dict["NAME"] == unswapped_name:
        return  # nothing was provisioned for this alias; nothing stranded to sweep

    django_db_blocker = request.getfixturevalue("django_db_blocker")

    with django_db_blocker.unblock():
        reset_fake_now(alias)


@pytest.fixture
def dj_absurd() -> t.Iterator[AbsurdTestRuntime]:
    """Read/drain/clock facade for durable tests: snapshot task/run state, burst-drain a
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
