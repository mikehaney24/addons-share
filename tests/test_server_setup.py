"""Exercise scripts/setup-server.sh for real.

These cover the home server's side of the setup, which the Python test suite
otherwise never touches -- and where a broken maintenance script fails
silently, by doing nothing, for months.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP = REPO_ROOT / "scripts" / "setup-server.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is required to run the setup script"
)


def run_setup(tmp_path: Path) -> tuple[Path, str]:
    proc = subprocess.run(
        ["bash", str(SETUP), str(tmp_path / "repos"), "forever.git"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return tmp_path / "repos" / "forever.git", proc.stdout


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def loose_objects(repo: Path) -> int:
    for line in git(repo, "count-objects", "-v").splitlines():
        if line.startswith("count:"):
            return int(line.split()[1])
    raise AssertionError("count-objects gave no count")


def test_creates_a_bare_repository(tmp_path):
    repo, _ = run_setup(tmp_path)
    assert repo.is_dir()
    assert git(repo, "rev-parse", "--is-bare-repository") == "true"


def test_is_idempotent(tmp_path):
    repo, _ = run_setup(tmp_path)
    subprocess.run(["bash", str(SETUP), str(tmp_path / "repos"), "forever.git"], check=True,
                   capture_output=True)
    assert git(repo, "rev-parse", "--is-bare-repository") == "true"


def test_disables_gc_during_pushes(tmp_path):
    """A repack firing mid-receive would stall a handoff."""
    repo, _ = run_setup(tmp_path)
    assert git(repo, "config", "gc.auto") == "0"
    assert git(repo, "config", "receive.autogc") == "false"


def test_history_is_kept_rather_than_pruned(tmp_path):
    repo, _ = run_setup(tmp_path)
    assert git(repo, "config", "gc.reflogExpire") == "never"
    assert git(repo, "config", "gc.reflogExpireUnreachable") == "never"


def test_the_maintenance_script_actually_repacks(tmp_path):
    """The regression this file exists for.

    The generated script used `gc --auto`, which honours the gc.auto=0 that
    setup deliberately sets -- so it exited 0 having done nothing, and the
    weekly cron job silently never reclaimed anything.
    """
    repo, _ = run_setup(tmp_path)
    maintenance = tmp_path / "repos" / "wowsync-gc.sh"
    assert maintenance.is_file() and maintenance.stat().st_mode & 0o111

    for i in range(40):
        subprocess.run(
            ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
            input=f"blob {i}\n", text=True, capture_output=True, check=True,
        )
    assert loose_objects(repo) == 40

    proc = subprocess.run(["bash", str(maintenance)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert loose_objects(repo) == 0, "the weekly maintenance script reclaimed nothing"
    assert "loose objects 40 -> 0" in proc.stdout


def test_the_repository_accepts_a_wowsync_push(tmp_path, monkeypatch):
    """The server's output is only useful if a client can actually push to it."""
    from tests.conftest import make_game_dir, make_config
    from wowsync.paths import ProfilePaths
    from wowsync.syncer import Syncer

    repo, stdout = run_setup(tmp_path)
    assert "Add this to the clients' config as git_remote" in stdout

    game = make_game_dir(tmp_path / "mac")
    config = make_config("mac-studio", game, repo)
    syncer = Syncer(config, config.profiles["forever"], ProfilePaths("forever", tmp_path / "st"))
    syncer.init()
    syncer.capture("initial")
    assert syncer.push().status == "pushed"

    branches = git(repo, "branch", "--format=%(refname:short)").split()
    assert "main" in branches
    assert "machine/mac-studio" in branches

    # And maintenance is safe to run against a repo holding real pushed state.
    subprocess.run(["bash", str(tmp_path / "repos" / "wowsync-gc.sh")], check=True,
                   capture_output=True)
    assert git(repo, "rev-parse", "main")
