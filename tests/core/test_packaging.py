import pydoc
import shutil
import subprocess
import sys
import tarfile
import zipfile
from importlib.resources import files
from pathlib import Path

import pytest

import django_absurd

# Distribution-build tests — version-agnostic and need the build backend
# (hatchling/hatch-vcs). Marked so the tox matrix can skip them and run them
# only in the `dev` env (see tox.ini).
pytestmark = pytest.mark.packaging

# Top-level entries allowed in the sdist. The hatchling backend uses an explicit
# allowlist ([tool.hatch.build.targets.sdist] include), so only the package plus the
# always-included packaging files ship. Anything else (tests/, docs/, .claude/,
# CLAUDE.md, dev configs) would be a leak. (.gitignore is force-added by hatchling.)
EXPECTED_SDIST_TOP = {
    ".gitignore",
    "LICENSE",
    "PKG-INFO",
    "README.md",
    "django_absurd",
    "pyproject.toml",
}


def test_dist_ships_only_django_absurd(tmp_path):
    root = Path(__file__).resolve().parent.parent.parent
    shutil.rmtree(root / "build", ignore_errors=True)
    # --no-isolation builds with the dev venv (which has build + hatchling +
    # hatch-vcs), so the test reports packaging problems rather than a slow isolated
    # install.
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


def test_agents_guide_discoverable_via_help():
    # help(django_absurd) renders the module docstring via pydoc; it must point an
    # agent at AGENTS.md...
    assert "AGENTS.md" in pydoc.render_doc(django_absurd)
    # ...and the guide the docstring promises must actually be readable from the package.
    guide = files("django_absurd").joinpath("AGENTS.md").read_text()
    assert guide.strip()
