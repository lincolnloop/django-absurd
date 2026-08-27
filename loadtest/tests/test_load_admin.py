import json
import pathlib
import typing as t

import pytest
from django.core.management import CommandError, call_command
from django.core.signals import request_started
from django.test import Client

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("_isolate_queues"),
]


@pytest.fixture
def seeded(admin_user: object) -> None:
    call_command("load_seed", queue=["bulk"], tasks=200, truncate=True)


def run_probe(**options: object) -> dict[str, t.Any]:
    path = pathlib.Path(call_command("load_admin", **options))
    payload: dict[str, t.Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def test_admin_probe_records_one_entry_per_probed_url(seeded: None) -> None:
    payload = run_probe(entity=["tasks"], deep_page=2)

    assert [e["label"] for e in payload["entries"]] == [
        "unfiltered",
        "queue",
        "state",
        "deep-page",
    ]


def test_admin_probe_captures_both_query_plans_for_every_entry(seeded: None) -> None:
    payload = run_probe(entity=["tasks"], deep_page=2)
    results_dir = pathlib.Path(payload["results_dir"])

    for entry in payload["entries"]:
        count_plan = (results_dir / entry["count_plan_path"]).read_text(
            encoding="utf-8"
        )
        page_plan = (results_dir / entry["page_plan_path"]).read_text(encoding="utf-8")
        assert "cost=" in count_plan
        assert "cost=" in page_plan


def test_admin_probe_records_a_duration_and_query_count(seeded: None) -> None:
    payload = run_probe(entity=["tasks"], deep_page=2)

    assert all(e["elapsed_ms"] > 0 for e in payload["entries"])
    assert all(e["queries"] >= 1 for e in payload["entries"])


def test_admin_probe_records_how_many_rows_each_arm_measured(seeded: None) -> None:
    payload = run_probe(entity=["tasks"], deep_page=2)

    assert all(e["result_count"] > 0 for e in payload["entries"])


def test_admin_probe_probes_every_entity_by_default(seeded: None) -> None:
    payload = run_probe(deep_page=2)

    assert {e["entity"] for e in payload["entries"]} == {
        "tasks",
        "runs",
        "checkpoints",
        "events",
        "waits",
    }


def test_admin_probe_skips_the_state_filter_where_the_entity_has_no_state(
    seeded: None,
) -> None:
    payload = run_probe(entity=["events"], deep_page=2)

    assert "state" not in [e["label"] for e in payload["entries"]]


def test_admin_probe_skips_the_deep_page_where_the_entity_cannot_paginate(
    seeded: None,
) -> None:
    # Checkpoints, events and waits hold only the workflow templates' handful of rows —
    # far under list_per_page — and ChangeList.get_results ignores ?p= entirely unless
    # the result set paginates. A recorded deep-page arm there would be an unfiltered
    # page-1 render wearing a deep-page label.
    payload = run_probe(deep_page=2)

    deep_paged = {e["entity"] for e in payload["entries"] if e["label"] == "deep-page"}
    assert deep_paged == {"tasks", "runs"}


@pytest.mark.parametrize("entity", ["tasks", "runs"])
def test_admin_probe_filters_on_a_state_value_the_data_carries(
    seeded: None, admin_client: Client, entity: str
) -> None:
    # An arm filtered to zero rows still answers 200, and its timing and plan then
    # look symmetrical with the others while measuring nothing.
    payload = run_probe(entity=[entity], deep_page=2)
    url = next(e["url"] for e in payload["entries"] if e["label"] == "state")

    response = admin_client.get(url)
    assert response.context["cl"].result_count > 0


def test_a_later_run_does_not_reuse_an_earlier_runs_plan_paths(seeded: None) -> None:
    first = run_probe(entity=["events"], deep_page=2)
    second = run_probe(entity=["events"], deep_page=2)

    assert resolve_plan_paths(first).isdisjoint(resolve_plan_paths(second))


def resolve_plan_paths(payload: dict[str, t.Any]) -> set[pathlib.Path]:
    results_dir = pathlib.Path(payload["results_dir"])
    return {
        results_dir / entry[key]
        for entry in payload["entries"]
        for key in ("count_plan_path", "page_plan_path")
    }


def test_admin_probe_fails_loudly_when_a_page_does_not_return_200(
    seeded: None,
) -> None:
    # A deep page past the end of the data is the one non-200 the admin actually
    # produces: the changelist raises IncorrectLookupParameters and redirects to
    # ``?e=1``. Dropping ``absurd.tasks_view`` does NOT work as a trigger —
    # ``ReadOnlyAbsurdAdmin.get_queryset`` degrades to ``.none()`` and still renders
    # a 200.
    with pytest.raises(CommandError, match="tasks"):
        run_probe(entity=["tasks"], deep_page=99_999)


def test_admin_probe_repeats_each_arm_and_reports_the_median(seeded: None) -> None:
    payload = run_probe(entity=["tasks"], deep_page=2, repeat=3)

    entry = payload["entries"][0]
    assert len(entry["runs"]) == 3
    assert entry["spread"]["elapsed_ms"]["min"] <= entry["elapsed_ms"]
    assert entry["elapsed_ms"] <= entry["spread"]["elapsed_ms"]["max"]


def test_admin_probe_discards_a_warm_up_request_before_timing(seeded: None) -> None:
    """The timed requests must not include the one that pays the cold read.

    Counted rather than trusted to a flag: an arm's first request warms the table for
    the arms after it, which is what made the unfiltered arm of each entity look
    catastrophic and everything behind it look fine.
    """
    requests: list[object] = []

    def count_request(**kwargs: object) -> None:
        requests.append(kwargs)

    request_started.connect(count_request)
    try:
        payload = run_probe(entity=["tasks"], deep_page=2, repeat=2)
    finally:
        request_started.disconnect(count_request)

    warm_up_and_timed = 2 + 1
    assert len(requests) == len(payload["entries"]) * warm_up_and_timed


def test_admin_probe_records_that_its_plans_were_captured_warm(seeded: None) -> None:
    payload = run_probe(entity=["tasks"], deep_page=2, repeat=1)

    assert payload["entries"][0]["plan_regime"] == "warm"
