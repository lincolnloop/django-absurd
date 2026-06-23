from django.apps import AppConfig


class AbsurdConfig(AppConfig):
    name = "django_absurd"
    label = "django_absurd"

    def ready(self) -> None:
        import django_absurd.checks  # noqa: F401, PLC0415
