import os

SECRET_KEY = "nonsense-not-a-secret"  # noqa: S105

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

# The plain `db`, not the tuned `db_bench`: nothing in this suite measures a rate.
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

TIME_ZONE = "UTC"

# The harness's own queue, declared here rather than reusing the main suite's: the
# stage definitions name it, and a queue this suite provisions cannot collide with
# one another suite's --reuse-db leftovers holds.
TASKS = {
    "default": {
        "BACKEND": "django_absurd.backends.AbsurdBackend",
        "QUEUES": ["bench"],
    }
}
