import typing as t

from django_absurd.queues import resolve_absurd_database


class AbsurdRouter:
    def db_for_read(self, model: t.Any, **hints: t.Any) -> str | None:
        if model._meta.app_label == "django_absurd":  # noqa: SLF001
            return resolve_absurd_database()
        return None

    def db_for_write(self, model: t.Any, **hints: t.Any) -> str | None:
        if model._meta.app_label == "django_absurd":  # noqa: SLF001
            return resolve_absurd_database()
        return None

    def allow_migrate(
        self,
        db: str,
        app_label: str,
        model_name: str | None = None,
        **hints: t.Any,
    ) -> bool | None:
        if app_label == "django_absurd":
            return db == resolve_absurd_database()
        return None
