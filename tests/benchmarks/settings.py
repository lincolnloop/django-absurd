import os
import typing as t

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
# buy this suite nothing.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("PGDATABASE", "postgres"),
        "USER": os.environ.get("PGUSER", "postgres"),
        "PASSWORD": os.environ.get("PGPASSWORD", "postgres"),
        "HOST": os.environ.get("PGHOST", "localhost"),
        "PORT": os.environ.get("PGPORT", "5432"),
        "TEST": {"NAME": f"test_{os.environ.get('PGDATABASE', 'postgres')}_benchmarks"},
    }
}
