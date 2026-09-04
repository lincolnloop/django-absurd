import os

import dj_database_url

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "bench-only-not-secret")

# Off for a measurement, because DEBUG appends every query to `connection.queries`
# and never trims it; `DEBUG=1` is for browsing the seeded tables.
DEBUG = os.environ.get("DEBUG", "") == "1"

# `workload` holds the model a durable task body reads and writes; a benchmark
# database needs its table, which is what makes `migrate` cover it.
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "django_absurd",
    "workload",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

ROOT_URLCONF = "urls"

STATIC_URL = "static/"

# `PGPORT_BENCH` is read by both sides, as `PGPORT` does for the suites, and the
# database name is db_bench's own: a suite's server would print untuned numbers.
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
