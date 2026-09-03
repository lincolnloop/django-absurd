import os
import typing as t

import tests.settings
from tests.settings import *  # noqa: F403

# The harness's own queue, declared here rather than reusing the main suite's: the
# stage definitions name it, and a queue this suite provisions cannot collide with
# one another suite's --reuse-db leftovers holds.
absurd_task: dict[str, t.Any] = {
    "BACKEND": "django_absurd.backends.AbsurdBackend",
    "QUEUES": ["bench"],
}
TASKS = {"default": absurd_task}

# The parent compose server, like every other suite. The tuned db_bench instance is
# for real benchmark runs; nothing here measures a rate, so its configuration would
# buy this suite nothing. Derived from the main suite's entry rather than restated, so
# the connection cannot drift from it — a copy here kept pointing at 5432 after the
# suites moved off the default ports.
DATABASES = {
    "default": DATABASES["default"]  # noqa: F405
    | {"TEST": {"NAME": f"test_{os.environ.get('PGDATABASE', 'postgres')}_benchmarks"}},
}

# The harness's own workload app, holding the model a durable task body reads and
# writes. The `absurd_worker` children run on `benchmarks/settings.py` against THIS
# suite's database, so its table has to be migrated into this one. Named through the
# module rather than off the star import, which flake8 cannot see a name through.
# `staticfiles` is what `runserver --insecure` hands the admin its CSS from, so a
# seeded corpus is browsable rather than only readable (`benchmarks/README.md`).
INSTALLED_APPS = [
    *tests.settings.INSTALLED_APPS,
    "django.contrib.staticfiles",
    "workload",
]
STATIC_URL = "static/"

# Named hosts rather than DEBUG, which `runserver` would also accept: a changelist over
# millions of rows is exactly where Django's debug query log grows without bound.
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
