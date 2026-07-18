from django_absurd.models import Queue
from django_absurd.routers import AbsurdRouter


def test_router_is_noop_at_default() -> None:
    # resolve_absurd_database() returns "default" in the main suite → router
    # routes django_absurd to "default" (a no-op).
    router = AbsurdRouter()
    assert router.allow_migrate("default", "django_absurd") is True
    assert router.allow_migrate("default", "django_absurd_pg_cron") is True
    assert router.db_for_read(Queue) == "default"
    assert Queue.objects.db == "default"
