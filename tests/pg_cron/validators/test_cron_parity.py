"""Parity between our Python grammar rules and the real extension.

``test_cron.py`` asserts what django-absurd accepts and rejects. This asserts that
those answers still match pg_cron itself — including the two places we deliberately
disagree, so that if a future pg_cron stops silently truncating, these fail and tell
us the divergence can be dropped.

Never surfaced to users: schedule validation stays pure Python (see the design spec).
"""

import pytest

from tests.pg_cron import utils
from tests.pg_cron.validators.test_cron import ACCEPTED, REJECTED
from tests.pg_cron.validators.utils import ValidateSubject

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


@pytest.mark.parametrize("cron", REJECTED_ONLY_BY_US)
def test_expression_pg_cron_silently_truncates_is_rejected_by_us(
    validate: ValidateSubject,
    cron: str,
) -> None:
    assert validate(cron=cron)
