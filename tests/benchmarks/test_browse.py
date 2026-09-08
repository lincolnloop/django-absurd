import subprocess
import sys

import pytest

import runner

# `build_worker_env` reads the live connection to point a child at THIS suite's
# database, so these need one.
pytestmark = pytest.mark.django_db(transaction=True)


def test_admin_mounts_on_the_benchmark_settings() -> None:
    """`reverse` fails unless the admin app, `ROOT_URLCONF` and `urls.py` all hold."""
    completed = run_manage(
        "shell", "-c", "from django.urls import reverse; print(reverse('admin:index'))"
    )

    assert completed.returncode == 0, completed.stdout
    assert completed.stdout.strip().endswith("/admin/"), completed.stdout


def test_admin_checks_pass_against_the_benchmark_settings() -> None:
    """Covers `benchmarks/settings.py`'s own admin wiring, which no suite exercises."""
    completed = run_manage("check")

    assert completed.returncode == 0, completed.stdout
    assert "no issues" in completed.stdout, completed.stdout


def run_manage(*argv: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [sys.executable, runner.MANAGE_PY, *argv],
        capture_output=True,
        # The assertions read the exit code, so a failure has to reach them.
        check=False,
        cwd=runner.REPO_ROOT,
        env=runner.build_worker_env(),
        text=True,
    )
