import os

import dj_database_url

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "bench-only-not-secret")

INSTALLED_APPS = ["django_absurd"]

# `PGPORT_BENCH` is read by both sides — the port compose publishes and the port this
# connects to — so one value keeps them consistent, as `PGPORT` does for the suites.
# The database name is db_bench's own too: a stray run against a suite's server would
# measure an untuned Postgres and still print numbers.
DATABASES = {
    "default": dj_database_url.parse(
        os.environ.get(
            "DATABASE_URL",
            "postgres://postgres:postgres@localhost:"
            f"{os.environ.get('PGPORT_BENCH', '5460')}/absurd_bench",
        )
    )
}
# After the parse, not inside it: dj_database_url builds the alias dict from the URL
# alone and would drop a TEST key handed to it.
DATABASES["default"]["TEST"] = {"NAME": "test_absurd_bench"}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

TIME_ZONE = "UTC"

TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "QUEUES": ["bench"],
    }
}
