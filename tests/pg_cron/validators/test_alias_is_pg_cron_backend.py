from tests.pg_cron.validators.utils import (
    BACKEND,
    QUEUES,
    clean_scheduled_task,
    validate_from_model,
)


def test_unknown_alias_rejected(settings):
    # model-only: the check's alias is a real backend key, always valid
    result = validate_from_model(settings, alias="ghost")
    assert result
    assert "backend 'ghost' is not a configured pg_cron backend." in result


def test_beat_backend_alias_rejected(settings):
    # alias resolves to a real backend, but one whose scheduler is beat, not pg_cron
    settings.TASKS = {
        "default": {
            "BACKEND": BACKEND,
            "OPTIONS": {"QUEUES": QUEUES, "SCHEDULER": "pg_cron"},
        },
        "beat_db": {
            "BACKEND": BACKEND,
            "OPTIONS": {"QUEUES": QUEUES, "SCHEDULER": "beat"},
        },
    }
    result = clean_scheduled_task(alias="beat_db")
    assert result
    assert "backend 'beat_db' is not a configured pg_cron backend." in result
