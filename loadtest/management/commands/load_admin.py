"""Time the Absurd admin changelists and capture the plans behind them.

Each probe drives the real admin over Django's test client — same middleware, same
``ChangeList``, same filters a browser gets — and records three things: wall clock,
how many queries the page ran, and the ``EXPLAIN (ANALYZE, BUFFERS)`` plan for the two
that matter (pagination's ``COUNT(*)`` and the paged ``SELECT``). Plans matter as much
as the clock here: ``natural_key`` is a computed expression (``'<queue>:' || id``), and
sorting on it means the planner cannot walk a per-table index and stop early at
``LIMIT 100``. Whether that is what actually happens at volume is exactly what these
dumps answer.

Two admin facts shape what to expect. ``show_full_result_count = False``, so the only
COUNT on the page is the paginator's. And ``ReadOnlyAbsurdAdmin.ordering`` is
``natural_key`` only for checkpoints, events and waits — the tasks and runs admins
override it with real datetime columns. That override does not get the expression out
of the sort, though: ``natural_key`` is the pk, and Django appends the pk to every
changelist ordering as a deterministic tiebreaker, so it lands in every ``Sort Key``
regardless. Read the dumps, not this paragraph, for what the planner then does with it.

The ``state`` probe is the entity's own state list filter, which only tasks and runs
declare (``EntitySpec.has_state``). Checkpoints, events and waits get no such arm:
https://github.com/lincolnloop/django-absurd/pull/244 dropped the checkpoint status
filter, since Absurd only ever writes one status. It filters on a value the seeded data
really carries — an arm filtered to zero rows answers 200 and looks like the others
while measuring nothing. Every entry records ``result_count`` for that reason: an arm
says how many rows it measured rather than leaving that to be inferred from the seed.

The ``deep-page`` probe is skipped on the same grounds wherever the entity is too small
to paginate. ``ChangeList.get_results`` only honours ``?p=`` when ``multi_page`` — under
``list_per_page`` rows and it renders the whole queryset and ignores the parameter — so
on checkpoints, events and waits (a handful of workflow rows each) a recorded deep-page
arm would be a page-1 render wearing a deep-page label. Timings include the
query-capture wrapper's overhead, which is microseconds per query against pages the
harness expects to measure in seconds.
"""

import functools
import pathlib
import typing as t

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from django_absurd.admin_views import ADMIN_ENTITY_SPECS, EntitySpec
from django_absurd.queues import resolve_absurd_database
from loadtest import clocks, preflight, results

if t.TYPE_CHECKING:
    from django.contrib.admin.views.main import ChangeList
    from django.core.management.base import CommandParser

RUN_NAME = "admin"
DEEP_PAGE_LABEL = "deep-page"
ENTITY_NAMES = tuple(spec.name for spec in ADMIN_ENTITY_SPECS)
DEFAULT_DEEP_PAGE = 500
PROBE_QUEUE = "bulk"
PROBE_STATE = "completed"
SLOWEST_QUERY_COUNT = 3
SQL_EXCERPT_CHARS = 300


class Arm(t.NamedTuple):
    label: str
    url: str


class Command(BaseCommand):
    help = (
        "Time each Absurd admin changelist, count its queries and dump the "
        "EXPLAIN (ANALYZE, BUFFERS) plans for its COUNT and paged SELECT."
    )

    def add_arguments(self, parser: "CommandParser") -> None:
        parser.add_argument(
            "--entity",
            action="append",
            default=None,
            choices=ENTITY_NAMES,
            help="Admin entity to probe; repeatable (default: every entity).",
        )
        results.add_repeat_argument(parser)
        parser.add_argument(
            "--deep-page",
            type=int,
            default=DEFAULT_DEEP_PAGE,
            help=(
                "Page number for the deep-pagination probe "
                f"(default: {DEFAULT_DEEP_PAGE}). On an entity whose rows do paginate "
                "that far it must exist: the admin answers an out-of-range page with a "
                "redirect, which the probe refuses to time. On one too small to "
                "paginate at all the admin ignores the parameter, and the probe drops "
                "that arm rather than record a page-1 render under a deep-page label."
            ),
        )

    def handle(self, *args: t.Any, **options: t.Any) -> str:
        preflight.require_migrated_database()
        selected = options["entity"] or list(ENTITY_NAMES)
        using = resolve_absurd_database()
        client = build_admin_client()
        run_dir = results.open_run(RUN_NAME)

        entries: list[dict[str, t.Any]] = []
        for spec in (find_entity_spec(name) for name in selected):
            for arm in build_probe_urls(spec, options["deep_page"]):
                entry = self.probe_changelist(
                    client, spec, arm, using, run_dir, options["repeat"]
                )
                if entry is not None:
                    entries.append(entry)

        self.report(entries)
        # Returned, not written: BaseCommand puts a truthy handle() result on stdout,
        # so the path lands there and in call_command's return value both.
        return str(results.write_run(RUN_NAME, {"entries": entries}, run_dir))

    def probe_changelist(
        self,
        client: Client,
        spec: EntitySpec,
        arm: Arm,
        using: str,
        run_dir: pathlib.Path,
        repeat: int,
    ) -> dict[str, t.Any] | None:
        """Time one changelist URL, or return ``None`` where it measures nothing.

        The first request is discarded. Whichever arm runs first against a table pays
        that table's cold read, and with the arms in a fixed order that cost always
        landed on the same one — reading as a property of the changelist rather than of
        the schedule. Discarding it puts every arm, and the ``EXPLAIN`` taken
        afterwards, in the same warm regime.
        """
        label, url = arm
        with CaptureQueriesContext(connections[using]) as capture:
            response = client.get(url)
        captured = capture.captured_queries

        if response.status_code != 200:
            msg = (
                f"The {spec.name} changelist answered {url} with "
                f"{response.status_code}, not 200. The probe times rendered "
                "changelists; a redirect or an error page measures nothing. A deep "
                "page past the end of the data is the usual cause — seed more rows "
                "or lower --deep-page."
            )
            raise CommandError(msg)

        changelist = take_changelist(response.context_data, spec, url)
        if label == DEEP_PAGE_LABEL and not changelist.multi_page:
            self.stdout.write(
                f"{spec.name}: dropped the deep-page arm — {changelist.result_count} "
                f"rows fit on one page, so the admin ignored ?p={changelist.page_num} "
                "and rendered page 1."
            )
            return None

        count_sql = find_count_query(captured, spec, url)
        page_sql = find_page_query(captured, spec, url)
        stem = f"{spec.name}-{label}"
        entry = results.summarize_repeats(
            functools.partial(self.time_changelist, client, url, using),
            repeat=repeat,
            rank_by="elapsed_ms",
        )
        return {
            "entity": spec.name,
            "label": label,
            "url": url,
            # Always 200: the guard above turns anything else into a CommandError.
            "status": response.status_code,
            "result_count": changelist.result_count,
            **entry,
            # The plans are taken after the timed requests and are therefore warm —
            # which is only honest because the timings are warm too.
            "plan_regime": "warm",
            "count_plan_path": results.write_plan(
                run_dir, f"{stem}-count.txt", explain_query(count_sql, using)
            ),
            "page_plan_path": results.write_plan(
                run_dir, f"{stem}-page.txt", explain_query(page_sql, using)
            ),
        }

    def time_changelist(self, client: Client, url: str, using: str) -> dict[str, t.Any]:
        """One timed render of an already-warm changelist."""
        with (
            clocks.measure_phase(f"{url}") as phase,
            CaptureQueriesContext(connections[using]) as capture,
        ):
            client.get(url)
        elapsed_ms = phase.elapsed_s * 1000
        return {
            "elapsed_ms": round(elapsed_ms, 1),
            "queries": len(capture.captured_queries),
            "slowest_queries": summarize_slowest_queries(capture.captured_queries),
        }

    def report(self, entries: list[dict[str, t.Any]]) -> None:
        self.stdout.write(
            f"{'entity':<12}{'label':<12}{'status':>7}{'rows':>10}"
            f"{'elapsed_ms':>12}{'queries':>9}{'runs':>6}{'spread':>22}  url"
        )
        for entry in entries:
            self.stdout.write(
                f"{entry['entity']:<12}{entry['label']:<12}{entry['status']:>7}"
                f"{entry['result_count']:>10}"
                f"{entry['elapsed_ms']:>12}{entry['queries']:>9}"
                f"{len(entry['runs']):>6}"
                f"{results.format_spread(entry, 'elapsed_ms'):>22}  {entry['url']}"
            )


def build_admin_client() -> Client:
    """Log a superuser straight in — the login form is not what is being measured."""
    user_model = get_user_model()
    user = user_model.objects.filter(is_superuser=True, is_active=True).first()
    if user is None:
        user = user_model.objects.create_superuser(username="load-admin")
    client = Client()
    client.force_login(user)
    return client


def find_entity_spec(name: str) -> EntitySpec:
    for spec in ADMIN_ENTITY_SPECS:
        if spec.name == name:
            return spec
    msg = f"Unknown entity {name!r}. Choose from: {', '.join(ENTITY_NAMES)}."
    raise CommandError(msg)


def build_probe_urls(spec: EntitySpec, deep_page: int) -> list[Arm]:
    url = reverse(f"admin:django_absurd_{spec.model_name.lower()}_changelist")
    probes = [Arm("unfiltered", url), Arm("queue", f"{url}?queue={PROBE_QUEUE}")]
    if spec.has_state:
        probes.append(Arm("state", f"{url}?state={PROBE_STATE}"))
    probes.append(Arm(DEEP_PAGE_LABEL, f"{url}?p={deep_page}"))
    return probes


def take_changelist(
    context: dict[str, t.Any] | None, spec: EntitySpec, url: str
) -> "ChangeList":
    """The rendered page's own ``ChangeList`` — the record of what it really showed.

    Read off ``TemplateResponse.context_data``, deliberately not off the test client's
    ``response.context``: that attribute only exists once ``setup_test_environment()``
    has instrumented template rendering, which pytest installs and a real
    ``python -m loadtest.manage load_admin`` does not. Reaching for it would make the
    harness's own tests pass while every real run died — and installing the test
    environment to get it would tax the very rendering this command is here to time.
    """
    if context is None or "cl" not in context:
        msg = (
            f"The {spec.name} changelist answered {url} with no ChangeList in its "
            "context, so there is no record of what it rendered and no arm can be "
            "judged against it. The admin's view has changed shape; update "
            "load_admin.py."
        )
        raise CommandError(msg)
    changelist: ChangeList = context["cl"]
    return changelist


def find_count_query(captured: list[dict[str, str]], spec: EntitySpec, url: str) -> str:
    """The paginator's ``SELECT COUNT(*) AS "__count" FROM "absurd"."<view>"``."""
    matches = [
        sql
        for sql in collect_view_queries(captured, spec)
        if sql.startswith("SELECT COUNT(*)")
    ]
    return take_only_query(matches, spec, url, "COUNT")


def find_page_query(captured: list[dict[str, str]], spec: EntitySpec, url: str) -> str:
    """The changelist's own paged ``SELECT`` over the entity view.

    Everything else the page runs against that view is either the COUNT or the
    ``SELECT DISTINCT`` a list filter runs to populate its choices.
    """
    matches = [
        sql
        for sql in collect_view_queries(captured, spec)
        if not sql.startswith(("SELECT COUNT(*)", "SELECT DISTINCT "))
    ]
    return take_only_query(matches, spec, url, "paged SELECT")


def collect_view_queries(captured: list[dict[str, str]], spec: EntitySpec) -> list[str]:
    """Queries reading the entity's union view, as the ORM quotes it.

    The quoted form is what separates them from the admin's own provisioning probes
    (``to_regclass('absurd.tasks_view')`` and the ``pg_rewrite`` lookup), which name
    the same view unquoted.
    """
    quoted = f'"absurd"."{spec.view_name}"'
    return [query["sql"] for query in captured if quoted in query["sql"]]


def take_only_query(matches: list[str], spec: EntitySpec, url: str, kind: str) -> str:
    if len(matches) != 1:
        msg = (
            f"Found {len(matches)} candidates for the {spec.name} changelist's {kind} "
            f"at {url}, not one, so no plan can be attributed to it. The admin's "
            "query shape has changed; update the matcher in load_admin.py."
        )
        raise CommandError(msg)
    return matches[0]


def summarize_slowest_queries(captured: list[dict[str, str]]) -> list[dict[str, t.Any]]:
    slowest = sorted(captured, key=lambda query: float(query["time"]), reverse=True)
    return [
        {
            "ms": round(float(query["time"]) * 1000, 1),
            "sql": query["sql"][:SQL_EXCERPT_CHARS],
        }
        for query in slowest[:SLOWEST_QUERY_COUNT]
    ]


def explain_query(sql: str, using: str) -> str:
    with connections[using].cursor() as cur:
        cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql)
        return "\n".join(row[0] for row in cur.fetchall())
