import subprocess
from pathlib import Path

import pytest

from wowsync.config import Config, DaemonConfig, Profile, ServerConfig
from wowsync.paths import ProfilePaths
from wowsync.syncer import Syncer

ACCOUNT = "12345678#1"
REALM = "Nagrand"
CHARACTER = "Tankadin"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def make_game_dir(root: Path, *, resolution: str = "1920x1080") -> Path:
    """A minimal but structurally faithful WoW flavor directory."""
    game = root / "_forever_"
    account = game / "WTF" / "Account" / ACCOUNT
    char = account / REALM / CHARACTER

    write(game / "Interface" / "AddOns" / "Details" / "Details.toc", "## Title: Details\n")
    write(game / "Interface" / "AddOns" / "Details" / "core.lua", "-- details core\n")
    write(account / "SavedVariables" / "Details.lua", 'DetailsDB = {\n\t["theme"] = "dark",\n}\n')
    write(char / "SavedVariables" / "Details.lua", 'DetailsCharDB = {\n\t["spec"] = 1,\n}\n')
    write(char / "AddOns.txt", "Details: enabled\n")
    write(account / "bindings-cache.wtf", 'bind "F1" "TARGETSELF"\n')
    write(char / "macros-cache.txt", "MACRO hi\n")

    # Machine-specific: never leaves this machine.
    write(game / "WTF" / "Config.wtf", f'SET gxWindowedResolution "{resolution}"\n')
    write(account / "config-cache.wtf", f'SET Sound_OutputDriverName "{resolution}-speakers"\n')

    # Regenerated junk that must stay out of the repo.
    write(account / "SavedVariables" / "Details.lua.bak", "-- stale backup\n")
    write(game / "Cache" / "blob.bin", "cache\n")
    write(game / "Logs" / "Client.log", "log\n")
    return game


def make_config(machine_id: str, game_dir: Path, remote: Path, rules=None) -> Config:
    profile = Profile(
        name="forever",
        game_dir=game_dir,
        process_match="World of Warcraft Forever",
        git_remote=str(remote),
        saved_variables=rules or [],
    )
    profile.validate()
    return Config(
        machine_id=machine_id,
        server=ServerConfig(git_remote=str(remote)),
        daemon=DaemonConfig(),
        profiles={"forever": profile},
    )


class Machine:
    """A simulated computer: its own game directory and its own state dir."""

    def __init__(self, name: str, tmp: Path, remote: Path, *, resolution: str, rules=None):
        self.name = name
        self.game_dir = make_game_dir(tmp / name, resolution=resolution)
        self.config = make_config(name, self.game_dir, remote, rules)
        self.profile = self.config.profiles["forever"]
        self.paths = ProfilePaths("forever", root=tmp / name / "state")
        self.syncer = Syncer(self.config, self.profile, self.paths)
        self.syncer.init()

    def read(self, rel: str) -> str:
        return (self.game_dir / rel).read_text()

    def write(self, rel: str, text: str) -> None:
        write(self.game_dir / rel, text)

    def exists(self, rel: str) -> bool:
        return (self.game_dir / rel).exists()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    bare = tmp_path / "server" / "forever.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet", "--bare", str(bare)], check=True)
    return bare


@pytest.fixture
def mac(tmp_path: Path, remote: Path) -> Machine:
    return Machine("mac-studio", tmp_path, remote, resolution="3840x2160")


@pytest.fixture
def linux(tmp_path: Path, remote: Path) -> Machine:
    return Machine("linux-rig", tmp_path, remote, resolution="2560x1440")
