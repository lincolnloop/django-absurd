import subprocess
import sys

import pytest

import runner

# `build_worker_env` reads the live connection to point a child at THIS suite's
# database, so these need one.
pytestmark = pytest.mark.django_db(transaction=True)

MANAGE = "benchmarks/manage.py"


def test_admin_mounts_on_the_benchmark_settings() -> None:
    """The seeded tables are browsable on `benchmarks/settings.py` alone.

    `reverse` resolving is the observable that covers the whole chain at once: the
    admin app installed, `ROOT_URLCONF` set, and `urls.py` importable. A settings
    module that merely holds the right keys would still fail it.
    """
    completed = run_manage(
        "shell", "-c", "from django.urls import reverse; print(reverse('admin:index'))"
    )

    assert completed.returncode == 0, completed.stdout
    assert completed.stdout.strip().endswith("/admin/"), completed.stdout


def test_admin_checks_pass_against_the_benchmark_settings() -> None:
    """Django's own admin checks validate every `ordering` field the specs name.

    Worth a subprocess: the suites run the admin against the test settings, where a
    spec naming a column the browse configuration lacks would go unnoticed.
    """
    completed = run_manage("check")

    assert completed.returncode == 0, completed.stdout
    assert "no issues" in completed.stdout, completed.stdout


def run_manage(*argv: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [sys.executable, MANAGE, *argv],
        capture_output=True,
        # The assertions read the exit code, so a failure has to reach them.
        check=False,
        cwd=runner.REPO_ROOT,
        env=runner.build_worker_env(),
        text=True,
    )
