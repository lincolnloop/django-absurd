import typing as t

from django_absurd.exceptions import QUEUE_READONLY_MSG, QueueReadOnlyError
from django_absurd.management.base import AbsurdCommand


class Command(AbsurdCommand):
    """Test-only command: raises an out-of-tuple ``DjangoAbsurdError`` from
    ``handle`` to prove ``AbsurdCommand.execute`` leaves it untranslated.
    """

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        raise QueueReadOnlyError(QUEUE_READONLY_MSG)
