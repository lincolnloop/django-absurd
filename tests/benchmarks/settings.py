import os

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "test-only-not-secret")

# `workload` because the worker children run on `benchmarks/settings.py` against THIS
# suite's database, so its table has to migrate here too.
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django_absurd",
    "tests",
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

ROOT_URLCONF = "tests.urls"

# The parent compose server, like every other suite. The tuned db_bench instance is
# for real benchmark runs; nothing here measures a rate, so its configuration would
# buy this suite nothing. Every field reads the same variable the other suites read,
# which is what keeps a standalone copy from drifting off the published ports.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("PGDATABASE", "postgres"),
        "USER": os.environ.get("PGUSER", "postgres"),
        "PASSWORD": os.environ.get("PGPASSWORD", "postgres"),
        "HOST": os.environ.get("PGHOST", "localhost"),
        "PORT": os.environ.get("PGPORT", "5442"),
        "TEST": {"NAME": f"test_{os.environ.get('PGDATABASE', 'postgres')}_benchmarks"},
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

TIME_ZONE = "UTC"

DATABASE_ROUTERS = ["django_absurd.routers.AbsurdRouter"]

# The harness's own queue, declared here rather than reusing the main suite's: the
# stage definitions name it, and a queue this suite provisions cannot collide with
# one another suite's --reuse-db leftovers holds.
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "QUEUES": ["bench"],
    }
}
