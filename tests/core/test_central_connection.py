import pytest
from django.core.exceptions import ImproperlyConfigured

from django_absurd import connection


@pytest.mark.django_db
def test_resolve_cron_database_raises_when_no_pg_cron() -> None:
    with pytest.raises(ImproperlyConfigured) as excinfo:
        connection.resolve_cron_database("default")
    assert str(excinfo.value) == (
        "cron.database_name is not set — this PostgreSQL server has no pg_cron"
        " (add pg_cron to shared_preload_libraries and set cron.database_name)."
    )
