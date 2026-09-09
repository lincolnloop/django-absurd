"""Drive django-absurd's admin changelists and record what each page cost.

Two passes over the same URL, because the two things worth knowing cannot be taken
together: `execute_wrapper` is what captures the SQL a page ran, and instrumentation in
a timed request is exactly what `refuse_measuring_under_debug` exists to keep out. So
the first pass is clean and holds the clock, and the second re-requests the page to
collect its queries and explain the two that matter.

The real admin over Django's test client, not a hand-written query: same middleware,
same `ChangeList`, same filters a browser gets.
"""

import time
import typing as t

from django.contrib.auth import get_user_model
from django.db import connections
from django.test import Client
from django.urls import reverse

from django_absurd.queues import resolve_absurd_database

# The changelists that hold volume. Checkpoints, events and waits carry a handful of
# workflow rows each, so an arm on them measures nothing that a million tasks does.
ADMIN_ENTITIES = ("task", "run")
# Rows a changelist renders per page, which is `ModelAdmin.list_per_page`'s default and
# what turns a row count into a page count.
ADMIN_PAGE_SIZE = 100
PROBE_USERNAME = "benchmark-admin"


def probe_admin_changelists(queue: str, state: str) -> list[dict[str, t.Any]]:
    """Every arm of both changelists, timed and then explained.

    No `setup_test_environment` here, deliberately: it refuses to run twice, so a
    stage that called it would work under `python -m stages` and raise under pytest,
    which has already called it. What it was needed for instead comes from the two
    things this module does anyway — `settings.ALLOWED_HOSTS` names `testserver`, and
    the result count is read off `context_data`, a `TemplateResponse` attribute, and
    not off `.context`, which is the test-client convenience that only exists under a
    test runner.
    """
    client = Client()
    client.force_login(read_probe_user())
    return [
        measure_admin_arm(client, entity, probe, query)
        for entity in ADMIN_ENTITIES
        for probe, query in build_admin_probes(client, entity, queue, state)
    ]


def build_admin_probes(
    client: Client, entity: str, queue: str, state: str
) -> list[tuple[str, dict[str, str]]]:
    """The four probes of one changelist, the last page resolved from its row count.

    Resolved rather than assumed: `ChangeList.get_results` ignores `?p=` unless the
    table paginates, so a fixed deep page on a short table renders page 1 and answers
    200 while wearing a deep-page label. Asking for the LAST page is true at every
    size, and each arm records which page that was.
    """
    rows = read_result_count(client, entity, {})
    last_page = max(0, (rows - 1) // ADMIN_PAGE_SIZE)
    return [
        ("unfiltered", {}),
        ("queue", {"queue": queue}),
        ("state", {"state": state}),
        ("last_page", {"p": str(last_page)}),
    ]


def measure_admin_arm(
    client: Client, entity: str, probe: str, query: dict[str, str]
) -> dict[str, t.Any]:
    """One arm: a clean timed request, then a second one that captures its queries."""
    url = reverse(f"admin:django_absurd_{entity}_changelist")
    started = time.perf_counter()
    response = client.get(url, query)
    wall_ms = 1000.0 * (time.perf_counter() - started)
    captured: list[tuple[str, t.Any]] = []
    with connections[resolve_absurd_database()].execute_wrapper(
        build_capture(captured)
    ):
        client.get(url, query)
    return {
        "name": f"{'tasks' if entity == 'task' else 'runs'}_{probe}",
        "entity": entity,
        "probe": probe,
        "query": query,
        "status": response.status_code,
        "wall_ms": wall_ms,
        # What the page said it was rendering. `context_data` and not `context`: the
        # latter is a test-client convenience that only exists under a test runner.
        "result_count": read_response_result_count(response),
        "query_count": len(captured),
        "plans": explain_changelist_queries(captured),
    }


def build_capture(
    captured: list[tuple[str, t.Any]],
) -> t.Callable[..., t.Any]:
    """An `execute_wrapper` hook, which sees every statement without a debug cursor."""

    def capture(
        execute: t.Callable[..., t.Any],
        sql: str,
        params: t.Any,
        *rest: t.Any,
    ) -> t.Any:
        """Django calls this positionally with `many` and `context` after the params,
        which pass straight through — naming them here would declare a positional
        boolean this wrapper never reads."""
        captured.append((sql, params))
        return execute(sql, params, *rest)

    return capture


def explain_changelist_queries(
    captured: list[tuple[str, t.Any]],
) -> dict[str, str]:
    """`EXPLAIN (ANALYZE, BUFFERS)` for the paginator's count and the paged select.

    Those two are the page: everything else a changelist runs is session and permission
    bookkeeping against tables holding single figures.
    """
    plans = {}
    for role, statement in pick_changelist_queries(captured).items():
        sql, params = statement
        with connections[resolve_absurd_database()].cursor() as cursor:
            cursor.execute(f"explain (analyze, buffers) {sql}", params)
            plans[role] = "\n".join(row[0] for row in cursor.fetchall())
    return plans


def pick_changelist_queries(
    captured: list[tuple[str, t.Any]],
) -> dict[str, tuple[str, t.Any]]:
    """The count and the paged select, by the shape of the SQL rather than by position.

    Position would be a guess: middleware order decides how many session and auth
    queries land in front of them.
    """
    picked = {}
    for sql, params in captured:
        lowered = sql.lower()
        if "count(*)" in lowered and "absurd" in lowered:
            picked["count"] = (sql, params)
        elif " limit " in lowered and "absurd" in lowered:
            picked["page"] = (sql, params)
    return picked


def read_result_count(client: Client, entity: str, query: dict[str, str]) -> int:
    response = client.get(reverse(f"admin:django_absurd_{entity}_changelist"), query)
    return read_response_result_count(response)


def read_response_result_count(response: t.Any) -> int:
    changelist = (response.context_data or {}).get("cl")
    return 0 if changelist is None else int(changelist.result_count)


def read_probe_user() -> t.Any:
    """The superuser the probe logs in as, made once and reused across reps.

    No password: every request here is a `force_login`, and a probe account that
    cannot be logged into over HTTP is one less thing a seeded database carries.
    """
    user, _ = get_user_model().objects.get_or_create(
        username=PROBE_USERNAME,
        defaults={"is_staff": True, "is_superuser": True},
    )
    return user
