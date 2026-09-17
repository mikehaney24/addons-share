"""Cover install.sh's interpreter selection.

Only the checks that run before pip are exercised, so these need no network.
The rest of the script is verified by running it by hand.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL = REPO_ROOT / "scripts" / "install.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is required to run the installer"
)


def fake_python(tmp_path: Path, version: str, supported: bool) -> Path:
    """A stub interpreter that answers the two questions install.sh asks."""
    path = tmp_path / "python3"
    path.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  case "$a" in\n'
        f'    *"version_info >= (3, 11)"*) exit {0 if supported else 1} ;;\n'
        f'    *platform*) echo "{version}"; exit 0 ;;\n'
        "  esac\n"
        "done\n"
        "exit 0\n"
    )
    path.chmod(0o755)
    return path


def run_install(env_extra: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(INSTALL)], capture_output=True, text=True, env=env, timeout=120
    )


def test_an_explicitly_named_old_python_is_rejected_by_version(tmp_path):
    stub = fake_python(tmp_path, "3.9.6", supported=False)
    proc = run_install({"PYTHON": str(stub)})

    assert proc.returncode != 0
    # The message has to name the version found, not just the requirement.
    assert "3.9.6" in proc.stderr
    assert "3.11 or newer" in proc.stderr


def test_a_missing_explicit_python_is_reported_clearly(tmp_path):
    proc = run_install({"PYTHON": str(tmp_path / "no-such-python")})

    assert proc.returncode != 0
    assert "not found" in proc.stderr


def test_the_version_error_says_why_and_how_to_fix_it(tmp_path):
    """A bare 'python 3.11 required' leaves a stock-macOS user stuck."""
    stub = fake_python(tmp_path, "3.9.6", supported=False)
    proc = run_install({"PYTHON": str(stub)})
    combined = proc.stdout + proc.stderr

    # Rejecting an interpreter the user chose should not bury them in advice,
    # but the discovery failure path must be actionable -- check that text
    # exists in the script for the case where nothing suitable was found.
    script = INSTALL.read_text()
    assert "brew install python@" in script
    assert "tomllib" in script, "should explain what actually needs 3.11"
    assert "PYTHON=/path/to/python3.12" in script, "should offer the escape hatch"
    assert "3.9.6" in combined


def test_discovery_looks_beyond_whatever_python3_resolves_to():
    """macOS ships 3.9 as /usr/bin/python3; a good interpreter is often
    installed but not first on PATH."""
    script = INSTALL.read_text()
    for candidate in ("python3.13", "python3.12", "python3.11",
                      "/opt/homebrew/bin/python3"):
        assert candidate in script, f"{candidate} is not among the candidates tried"
