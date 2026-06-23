import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

# Top-level entries allowed in the sdist. setuptools-scm's git file-finder would
# otherwise sweep the whole repo in; MANIFEST.in prunes it back to these. Anything
# else (tests/, docs/, examples/, CLAUDE.md, .github/, dev configs) is a leak.
EXPECTED_SDIST_TOP = {
    "LICENSE",
    "MANIFEST.in",
    "PKG-INFO",
    "README.md",
    "django_absurd",
    "django_absurd.egg-info",
    "pyproject.toml",
    "setup.cfg",
}


def test_dist_ships_only_django_absurd(tmp_path):
    root = Path(__file__).resolve().parent.parent
    shutil.rmtree(root / "build", ignore_errors=True)
    shutil.rmtree(root / "django_absurd.egg-info", ignore_errors=True)
    # --no-isolation builds with the dev venv (which has build + setuptools-scm),
    # so the test reports packaging problems rather than a slow isolated install.
    subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(tmp_path)],
        check=True,
        cwd=root,
    )

    # Wheel: only the importable package (+ its .dist-info), with the data files.
    wheel_names = zipfile.ZipFile(next(tmp_path.glob("*.whl"))).namelist()
    assert any(
        n.startswith("django_absurd/migrations/") and n.endswith(".sql")
        for n in wheel_names
    )
    assert "django_absurd/py.typed" in wheel_names
    assert "django_absurd/AGENTS.md" in wheel_names
    wheel_top = {n.split("/")[0] for n in wheel_names}
    unexpected_wheel = {
        t for t in wheel_top if t != "django_absurd" and not t.endswith(".dist-info")
    }
    assert not unexpected_wheel, f"unexpected top-level in wheel: {unexpected_wheel}"

    # Sdist: walk every member, collapse to top-level entries, assert the allowlist.
    with tarfile.open(next(tmp_path.glob("*.tar.gz"))) as tf:
        members = [m.name for m in tf.getmembers()]
    pkg_root = members[0].split("/")[0]
    sdist_top = {
        m[len(pkg_root) + 1 :].split("/")[0]
        for m in members
        if m != pkg_root and "/" in m
    }
    assert sdist_top <= EXPECTED_SDIST_TOP, (
        f"unexpected files in sdist: {sdist_top - EXPECTED_SDIST_TOP}"
    )
    assert {"django_absurd", "pyproject.toml", "README.md", "LICENSE"} <= sdist_top
