import os

import dj_database_url

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "bench-only-not-secret")

INSTALLED_APPS = ["django_absurd"]

# The compose service name is the default because the harness only ever runs inside
# that network; there is no published port to reach db_bench from the host.
DATABASES = {
    "default": dj_database_url.parse(
        os.environ.get(
            "DATABASE_URL", "postgres://postgres:postgres@db_bench:5432/postgres"
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
