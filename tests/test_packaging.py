import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def test_wheel_ships_migration_sql_and_excludes_tests(tmp_path):
    root = Path(__file__).resolve().parent.parent
    shutil.rmtree(root / "build", ignore_errors=True)
    shutil.rmtree(root / "django_absurd.egg-info", ignore_errors=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(tmp_path),
        ],
        check=True,
        cwd=root,
    )
    names = zipfile.ZipFile(next(tmp_path.glob("*.whl"))).namelist()
    assert any(
        n.startswith("django_absurd/migrations/") and n.endswith(".sql") for n in names
    )
    assert "django_absurd/py.typed" in names
    assert "django_absurd/AGENTS.md" in names
    assert not any(n.startswith("tests/") for n in names)
    assert not any(n.startswith("examples/") for n in names)
