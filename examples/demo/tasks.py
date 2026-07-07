import logging

from django.tasks import task

logger = logging.getLogger("demo")


@task
def ping() -> None:
    """Scheduled every minute (see SCHEDULE in settings). pg_cron fires it from
    Postgres; the worker runs it and logs the line below to the console."""
    logger.info("pong 🏓 — fired by the pg_cron scheduler (default backend)")


@task
def tick() -> None:
    """Scheduled every minute (see SCHEDULE in settings). The beat process fires
    it from Python; the worker runs it and logs the line below to the console."""
    logger.info("tock ⏰ — fired by the beat scheduler (beat backend)")
