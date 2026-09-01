import json
import pathlib

import pytest

from benchmarks import stages


@pytest.mark.django_db(transaction=True)
def test_sizes_the_producer_stage_from_the_tasks_flag(tmp_path: pathlib.Path) -> None:
    """A stage's size comes from the flag, not its hardcoded production count.

    Without this every entrypoint test enqueues the production 5000 per mode, which is
    why the harness had no entrypoint tests.
    """
    stages.main(
        ["--stage", "f", "--reps", "1", "--tasks", "10", "--results-dir", str(tmp_path)]
    )

    recorded = json.loads((tmp_path / "stage_f.json").read_text())
    assert [entry["median"]["count"] for entry in recorded["measurements"]] == [
        10,
        10,
        10,
    ]


@pytest.mark.django_db(transaction=True)
def test_sizes_a_saturation_stage_from_the_tasks_flag(tmp_path: pathlib.Path) -> None:
    stages.main(
        ["--stage", "d", "--reps", "1", "--tasks", "4", "--results-dir", str(tmp_path)]
    )

    recorded = json.loads((tmp_path / "stage_d.json").read_text())
    assert {entry["spec"]["tasks"] for entry in recorded["measurements"]} == {4}
    assert {entry["median"]["n_tasks"] for entry in recorded["measurements"]} == {4}


@pytest.mark.django_db(transaction=True)
def test_sizes_a_rate_stage_and_its_idle_probes_from_the_duration_flag(
    tmp_path: pathlib.Path,
) -> None:
    """Rate stages are sized in seconds, and the idle probes are rate work too.

    The probes have their own hardcoded duration that neither flag reaches, and they
    outlast the measurements they sit beside.
    """
    # Enough tasks that stage A's trimmed completion window is divisible: below about
    # fifty every rep reports zero throughput and stage C has nothing to calibrate on.
    size = ["--reps", "1", "--tasks", "60", "--duration", "1", "--results-dir"]
    stages.main(["--stage", "a", *size, str(tmp_path)])

    stages.main(["--stage", "c", *size, str(tmp_path)])

    recorded = json.loads((tmp_path / "stage_c.json").read_text())
    assert {entry["spec"]["duration_s"] for entry in recorded["measurements"]} == {1.0}
    assert [probe["seconds"] for probe in recorded["idle_probes"]] == [1.0, 1.0, 1.0]
