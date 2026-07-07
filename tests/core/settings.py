from tests.settings import *  # noqa: F403

# Base tests.settings is already the core config (pg_cron app absent, plain db).
# This module exists so the suite has its own DJANGO_SETTINGS_MODULE, matching
# the tests/multidb pattern.
