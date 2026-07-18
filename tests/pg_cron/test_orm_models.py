from django.apps import apps as global_apps


def test_pg_cron_model_in_global_registry() -> None:
    pg_cron_names = {
        m.__name__
        for m in global_apps.get_models()
        if m._meta.app_label == "django_absurd_pg_cron"
    }
    assert pg_cron_names == {"ScheduledTask"}
