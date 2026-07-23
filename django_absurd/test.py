"""Auto Absurd state cleanup for Django's test runner.

``install_absurd_cleanup()`` monkeypatches ``TransactionTestCase._post_teardown`` so
that every DB-backed test case flushes leftover Absurd state (per-queue tables,
scheduled jobs) after it runs. Patching that hook IS the detection: ``_post_teardown``
is defined on ``TransactionTestCase`` (``TestCase`` inherits it) and fires only for DB
test cases, and it runs inside pytest-django's ``django_db_blocker.unblock()`` context,
so the flush executes while the DB is unblocked.

The project's no-monkeypatch rule governs test-code hygiene; this is library test-infra
integration (pytest-django itself patches ``BaseDatabaseWrapper.ensure_connection``),
so the patch here is deliberate.
"""

import functools

from django.test import TestCase, TransactionTestCase

from django_absurd import backends  # import-safe: touches no settings at module load

CLEANUP_MARKER = "absurd_cleanup_installed"


def install_absurd_cleanup() -> None:
    """Idempotently wrap ``TransactionTestCase._post_teardown`` with Absurd cleanup.

    Version-guards first: if Django ever stops defining ``_post_teardown`` on
    ``TransactionTestCase`` the patch would silently attach to nothing, so raise loudly
    instead. Installing more than once is a no-op — the wrapper carries a marker
    attribute the next call detects.
    """
    if "_post_teardown" not in vars(TransactionTestCase):  # pragma: no cover
        msg = (
            "django-absurd expected TransactionTestCase._post_teardown to exist so it "
            "could install automatic Absurd state cleanup, but that hook is absent on "
            "this Django version. django-absurd's pytest integration needs to be "
            "updated for this Django release."
        )
        raise RuntimeError(msg)

    original = TransactionTestCase._post_teardown  # type: ignore[attr-defined]  # noqa: SLF001 -- Django exposes no public teardown hook to wrap
    if getattr(original, CLEANUP_MARKER, False):
        return

    @functools.wraps(original)
    def _post_teardown_with_absurd_cleanup(self: TransactionTestCase) -> None:
        original(self)
        flush_absurd_after_teardown(self)

    setattr(_post_teardown_with_absurd_cleanup, CLEANUP_MARKER, True)
    TransactionTestCase._post_teardown = _post_teardown_with_absurd_cleanup  # type: ignore[attr-defined]  # noqa: SLF001 -- Django exposes no public teardown hook to wrap


def flush_absurd_after_teardown(instance: TransactionTestCase) -> None:
    """Flush Absurd state after a test case's own teardown, when it applies.

    Skips when the case is a transactional ``TestCase`` (its rollback already reverts
    everything — the SCOPED ``_databases_support_transactions()`` probes only the case's
    own aliases), when no Absurd backend is configured, or when the Absurd database is
    not among the case's declared ``databases`` (respecting the ``"__all__"`` sentinel).
    """
    if isinstance(instance, TestCase) and instance._databases_support_transactions():  # type: ignore[attr-defined]  # noqa: SLF001 -- mirrors Django's own TestCase._fixture_teardown; no public equivalent
        return

    if not backends.get_absurd_backends():
        return

    from django_absurd.queues import resolve_absurd_database  # noqa: PLC0415

    databases = instance.databases
    if databases != "__all__" and resolve_absurd_database() not in databases:
        return

    from django_absurd.flush import flush_absurd_state  # noqa: PLC0415

    flush_absurd_state()
