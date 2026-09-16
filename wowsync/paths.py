"""Filesystem locations: config, per-profile state, and WoW install discovery."""

from __future__ import annotations

import os
from pathlib import Path

from .util import is_macos

# A WoW install root contains one directory per "flavor" (_retail_, _classic_era_,
# and whatever World of Warcraft Forever ships as). Each flavor directory is a
# self-contained game tree with Interface/ and WTF/ inside it, and that flavor
# directory is what wowsync syncs.
FLAVOR_MARKERS = ("WTF", "Interface")


def config_home() -> Path:
    if is_macos():
        return Path.home() / "Library" / "Application Support" / "wowsync"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "wowsync"


def state_home() -> Path:
    override = os.environ.get("WOWSYNC_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if is_macos():
        return Path.home() / "Library" / "Application Support" / "wowsync"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "wowsync"


def config_file() -> Path:
    override = os.environ.get("WOWSYNC_CONFIG")
    if override:
        return Path(override).expanduser()
    return config_home() / "wowsync.toml"


class ProfilePaths:
    """Where a single profile keeps its git dirs and staging area."""

    def __init__(self, name: str, root: Path | None = None):
        self.name = name
        self.root = (root or state_home()) / name

    @property
    def shared_git(self) -> Path:
        # Git dir for the shared tree. Its work tree is the live game directory.
        return self.root / "shared.git"

    @property
    def machine_git(self) -> Path:
        # Git dir for this machine's private tree. Its work tree is `local_tree`.
        return self.root / "machine.git"

    @property
    def local_tree(self) -> Path:
        # Staging copy of machine-specific files; never shared with the peer.
        return self.root / "local"

    @property
    def sv_local(self) -> Path:
        # Machine-local SavedVariables globals, stripped out of the shared copy.
        return self.local_tree / "sv-local"

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    @property
    def log_file(self) -> Path:
        return self.root / "logs" / "wowsync.log"

    def ensure(self) -> None:
        for d in (self.root, self.local_tree, self.sv_local, self.log_file.parent):
            d.mkdir(parents=True, exist_ok=True)


def is_flavor_dir(path: Path) -> bool:
    """True if `path` looks like a playable game tree (has WTF/ and Interface/)."""
    return path.is_dir() and all((path / marker).is_dir() for marker in FLAVOR_MARKERS)


def candidate_install_roots() -> list[Path]:
    """Plausible places a WoW install lives, for `wowsync init` to scan."""
    home = Path.home()
    roots: list[Path] = []

    if is_macos():
        roots += [
            Path("/Applications"),
            home / "Applications",
            Path("/Applications/World of Warcraft"),
            Path("/Applications/Battle.net"),
        ]
        for vol in _safe_iterdir(Path("/Volumes")):
            roots.append(vol)
    else:
        # Native Linux installs are rare; almost everything is a Wine/Proton prefix.
        roots += [
            home / "Games",
            home / ".wine" / "drive_c",
            home / ".local" / "share" / "lutris",
            home / ".var" / "app" / "net.lutris.Lutris",
        ]
        for steam in steam_libraries():
            roots.append(steam / "steamapps" / "common")
            roots.append(steam / "steamapps" / "compatdata")
        for mnt in (Path("/mnt"), Path("/media"), Path("/run/media")):
            for child in _safe_iterdir(mnt):
                roots.append(child)

    return [r for r in roots if r.is_dir()]


def steam_libraries() -> list[Path]:
    """Steam library roots, including extra libraries from libraryfolders.vdf."""
    home = Path.home()
    bases = [
        home / ".steam" / "steam",
        home / ".local" / "share" / "Steam",
        home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
    ]
    found: list[Path] = []
    seen: set[Path] = set()
    for base in bases:
        if not base.is_dir():
            continue
        resolved = base.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        found.append(resolved)
        vdf = resolved / "steamapps" / "libraryfolders.vdf"
        for extra in _parse_library_folders(vdf):
            if extra not in seen and extra.is_dir():
                seen.add(extra)
                found.append(extra)
    return found


def _parse_library_folders(vdf: Path) -> list[Path]:
    """Pull `"path"  "/some/dir"` entries out of Steam's libraryfolders.vdf."""
    try:
        text = vdf.read_text("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith('"path"'):
            continue
        parts = [p for p in line.split('"') if p.strip()]
        if len(parts) >= 2:
            out.append(Path(parts[-1]).resolve())
    return out


def find_flavor_dirs(roots: list[Path] | None = None, max_depth: int = 6) -> list[Path]:
    """Breadth-limited scan for game trees under the given roots."""
    roots = roots if roots is not None else candidate_install_roots()
    found: list[Path] = []
    seen: set[Path] = set()

    for root in roots:
        stack = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            try:
                resolved = current.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)

            if is_flavor_dir(current):
                found.append(current)
                continue  # don't descend into a game tree
            if depth >= max_depth:
                continue
            for child in _safe_iterdir(current):
                if child.name.startswith(".") and child.name not in (".wine",):
                    continue
                if child.is_symlink():
                    continue
                if child.is_dir():
                    stack.append((child, depth + 1))

    return sorted(found)


def _safe_iterdir(path: Path):
    try:
        return sorted(path.iterdir())
    except OSError:
        return []


def account_dirs(game_dir: Path) -> list[Path]:
    """The WTF/Account/<ACCOUNT> directories present in this install."""
    account_root = game_dir / "WTF" / "Account"
    return [
        d for d in _safe_iterdir(account_root)
        if d.is_dir() and not d.name.startswith(".") and d.name != "SavedVariables"
    ]
