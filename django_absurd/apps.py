from django.apps import AppConfig


class AbsurdConfig(AppConfig):
    name = "django_absurd"
    label = "django_absurd"
    verbose_name = "Absurd"

    def ready(self) -> None:
        import django_absurd.checks  # noqa: F401, PLC0415
        from django_absurd.admin_views import PRIVATE_ADMIN_APPS  # noqa: PLC0415

        # The synthesized admin models live in PRIVATE_ADMIN_APPS, so their
        # _meta.app_config resolves there. Point it at this config so the admin
        # change-view breadcrumb shows the app's verbose_name instead of blank.
        PRIVATE_ADMIN_APPS.app_configs.setdefault("django_absurd", self)
