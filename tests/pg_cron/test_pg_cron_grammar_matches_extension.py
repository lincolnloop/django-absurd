"""Does our grammar still agree with the pg_cron actually installed?

``validators/test_schedule_grammar.py`` asserts what django-absurd accepts and rejects,
in pure Python. This module asks the real extension the same questions and asserts the
answers line up — the guard against our copy of someone else's rules going stale when
the ``postgresql-*-cron`` pin moves.

Three tables, none hand-maintained in isolation: everything we accept must be accepted
there, everything we reject must be rejected there (derived from the rule table, so a
new rejection cannot be added unilaterally), and the two expressions we refuse BECAUSE
pg_cron silently truncates them are pinned as still-accepted upstream — if that is ever
fixed, these fail and say the divergence can go.

Unlike the rule tests, this one needs the extension: it schedules and unschedules a
throwaway job per expression on the central catalog.
"""

import pytest

from tests.pg_cron import utils
from tests.pg_cron.validators.test_schedule_grammar import ACCEPTED, REJECTED

pytestmark = pytest.mark.django_db(transaction=True)

# We reject these and pg_cron does NOT: its parser reads five fields and then the
# command, so the remainder is swallowed and the job silently runs a schedule nobody
# wrote. Rejecting is the point of the rule; this pins the upstream behavior that
# justifies it. @reboot/@restart are ours too — pg_cron implements them, they just
# describe no cadence.
REJECTED_ONLY_BY_US: list[str] = [
    "0 2 * * * *",
    "0 2 * * 5#2",
    "@reboot",
    "@restart",
]

# Everything else we reject, derived from the same table the rules are asserted
# against, so a new rejection case cannot be added without pg_cron agreeing.
REJECTED_BY_BOTH: list[str] = [
    cron for cron, _ in REJECTED if cron not in REJECTED_ONLY_BY_US
]


@pytest.mark.parametrize("cron", ACCEPTED)
def test_expression_we_accept_is_accepted_by_pg_cron(cron: str) -> None:
    assert utils.pg_cron_accepts(cron)


@pytest.mark.parametrize("cron", REJECTED_BY_BOTH)
def test_expression_we_reject_is_also_rejected_by_pg_cron(cron: str) -> None:
    assert not utils.pg_cron_accepts(cron)


@pytest.mark.parametrize("cron", REJECTED_ONLY_BY_US)
def test_expression_pg_cron_silently_truncates_is_still_accepted_upstream(
    cron: str,
) -> None:
    assert utils.pg_cron_accepts(cron)
