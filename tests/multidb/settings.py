import os
import typing as t

from tests.settings import *  # noqa: F403

absurd_task: dict[str, t.Any] = {
    "BACKEND": "django_absurd.backends.AbsurdBackend",
    "OPTIONS": {"DATABASE": "absurd"},
}
TASKS = {"default": absurd_task}
DATABASE_ROUTERS = ["django_absurd.routers.AbsurdRouter"]

# DATABASES is COMPLETELY redefined here (not derived from tests.settings): two
# Postgres aliases on the same compose server, each with its own _multidb-affixed
# TEST.NAME so this suite's test DBs never collide with the main suite's (--reuse-db
# leftovers). The main suite migrates django_absurd onto its default test DB; here
# "default" must stay clean of it, which the distinct test DB guarantees. No sqlite
# (this suite never uses it).
pg = {
    "ENGINE": "django.db.backends.postgresql",
    "USER": os.environ.get("PGUSER", "postgres"),
    "PASSWORD": os.environ.get("PGPASSWORD", "postgres"),
    "HOST": os.environ.get("PGHOST", "localhost"),
    "PORT": os.environ.get("PGPORT", "5432"),
    "NAME": os.environ.get("PGDATABASE", "postgres"),
}
DATABASES = {
    "default": pg | {"TEST": {"NAME": f"test_{pg['NAME']}_multidb"}},
    "absurd": pg | {"TEST": {"NAME": f"test_{pg['NAME']}_multidb_absurd"}},
}
