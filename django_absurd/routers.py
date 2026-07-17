import typing as t

from django_absurd.queues import resolve_absurd_database

if t.TYPE_CHECKING:
    from django.db.models import Model

ABSURD_APP_LABELS = frozenset({"django_absurd", "django_absurd_pg_cron"})


class AbsurdRouter:
    def db_for_read(self, model: "type[Model]", **hints: t.Any) -> str | None:
        if model._meta.app_label in ABSURD_APP_LABELS:  # noqa: SLF001
            return resolve_absurd_database()
        return None

    def db_for_write(self, model: "type[Model]", **hints: t.Any) -> str | None:
        if model._meta.app_label in ABSURD_APP_LABELS:  # noqa: SLF001
            return resolve_absurd_database()
        return None

    def allow_migrate(
        self,
        db: str,
        app_label: str,
        model_name: str | None = None,
        **hints: t.Any,
    ) -> bool | None:
        if app_label in ABSURD_APP_LABELS:
            return db == resolve_absurd_database()
        return None
