"""absurd.E012's reachable/extension-present probe, exercised directly against this
suite's plain DB (no pg_cron preloaded) — the E-branch of the central-extension check
itself only fires outside a test environment, so it's untestable via call_command; the
probe helper it delegates to is a real-condition unit the "missing extension" branch
IS reachable through (see test_central_connection.py for the same plain-DB premise)."""

import pytest

from django_absurd.pg_cron import checks


@pytest.mark.django_db
def test_probe_central_extension_reports_missing_without_pg_cron() -> None:
    assert checks.probe_central_extension("default") is False
