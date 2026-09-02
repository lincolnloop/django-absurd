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
# buy this suite nothing. Derived from the main suite's entry rather than restated, so
# the connection cannot drift from it — a copy here kept pointing at 5432 after the
# suites moved off the default ports.
DATABASES = {
    "default": DATABASES["default"]  # noqa: F405
    | {"TEST": {"NAME": f"test_{os.environ.get('PGDATABASE', 'postgres')}_benchmarks"}},
}
