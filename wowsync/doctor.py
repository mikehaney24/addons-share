"""Preflight checks.

Most of what goes wrong with a setup like this goes wrong quietly: a case
collision that only one of the two filesystems can represent, a pattern that
never matches the game process so the daemon never notices a session, a
machine-local file that slipped into the shared set. These checks look for
those before they cost you a UI.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import Config, Profile
from .lease import CoordinatorUnreachable, LeaseClient
from .paths import ProfilePaths, account_dirs, is_flavor_dir
from .procwatch import find_processes
from .savedvars import global_names
from .syncer import Syncer
from .util import is_macos, run, which

OK, WARN, BAD = "ok", "warn", "fail"


@dataclass
class Check:
    level: str
    title: str
    detail: str = ""


def run_checks(config: Config, profile: Profile) -> list[Check]:
    paths = ProfilePaths(profile.name)
    checks: list[Check] = []
    checks += _tooling()
    checks += _game_dir(profile)
    checks += _case_and_unicode(profile)
    checks += _process(profile)
    checks += _repo(config, profile, paths)
    checks += _server(config, profile)
    checks += _disk(profile, paths)
    return checks


def _tooling() -> list[Check]:
    git = which("git")
    if not git:
        return [Check(BAD, "git is not installed", "wowsync uses git for transfer and history")]
    version = run(["git", "--version"], check=False).stdout.decode().split()
    numbers = version[2].split(".") if len(version) > 2 else []
    try:
        major, minor = int(numbers[0]), int(numbers[1])
    except (IndexError, ValueError):
        return [Check(WARN, "could not read the git version", " ".join(version))]
    if (major, minor) < (2, 38):
        return [
            Check(
                BAD,
                f"git {major}.{minor} is too old",
                "merging two machines' changes needs 'git merge-tree --write-tree', "
                "added in git 2.38",
            )
        ]
    return [Check(OK, f"git {major}.{minor}")]


def _game_dir(profile: Profile) -> list[Check]:
    if not profile.game_dir.exists():
        return [Check(BAD, "game directory is missing", str(profile.game_dir))]
    if not is_flavor_dir(profile.game_dir):
        return [
            Check(
                BAD,
                "game_dir does not look like a game tree",
                f"{profile.game_dir} has no WTF/ and Interface/ inside it. Point it at "
                "the flavor directory (the one next to the client binary), not the "
                "install root.",
            )
        ]
    checks = [Check(OK, f"game directory {profile.game_dir}")]

    accounts = account_dirs(profile.game_dir)
    if not accounts:
        checks.append(
            Check(
                WARN,
                "no account folder yet",
                "WTF/Account is empty, so there are no settings to sync. Log in once "
                "and run this again.",
            )
        )
    else:
        checks.append(Check(OK, f"{len(accounts)} account folder(s): "
                                f"{', '.join(a.name for a in accounts)}"))

    addons = profile.game_dir / "Interface" / "AddOns"
    count = len([d for d in addons.iterdir() if d.is_dir()]) if addons.is_dir() else 0
    checks.append(Check(OK if count else WARN, f"{count} addon folder(s) installed"))
    return checks


def _case_and_unicode(profile: Profile) -> list[Check]:
    """The two filesystems disagree about names, and git is caught in between.

    APFS is case-insensitive by default and hands back decomposed unicode; ext4
    is case-sensitive and stores whatever it was given. Two addon folders that
    differ only by case are representable on Linux and not on macOS, and that
    asymmetry corrupts a checkout rather than failing cleanly.
    """
    checks: list[Check] = []
    addons = profile.game_dir / "Interface" / "AddOns"
    if not addons.is_dir():
        return checks

    seen: dict[str, str] = {}
    collisions: list[tuple[str, str]] = []
    for entry in addons.iterdir():
        key = entry.name.lower()
        if key in seen and seen[key] != entry.name:
            collisions.append((seen[key], entry.name))
        seen[key] = entry.name

    if collisions:
        checks.append(
            Check(
                BAD,
                f"{len(collisions)} addon folder name(s) differ only by case",
                "; ".join(f"{a} vs {b}" for a, b in collisions[:3])
                + " -- macOS cannot hold both at once. Remove one in your addon "
                "manager before syncing.",
            )
        )
    else:
        checks.append(Check(OK, "no case-colliding addon folders"))

    non_ascii = [e.name for e in addons.iterdir() if not e.name.isascii()]
    if non_ascii and is_macos():
        checks.append(
            Check(
                WARN,
                f"{len(non_ascii)} addon folder(s) have non-ASCII names",
                "core.precomposeunicode is set, which handles this, but check they "
                "appear on both machines: " + ", ".join(non_ascii[:3]),
            )
        )
    return checks


def _process(profile: Profile) -> list[Check]:
    pattern = profile.process_match or "(built-in default)"
    running = find_processes(profile.process_match)
    if running:
        return [Check(OK, f"game process matches {pattern}", running[0][1][:90])]
    return [
        Check(
            WARN,
            f"game is not running, so {pattern} is unverified",
            "Launch the game and re-run 'wowsync doctor' to confirm the daemon will "
            "see your sessions. If it still reports nothing, set process_match.",
        )
    ]


def _repo(config: Config, profile: Profile, paths: ProfilePaths) -> list[Check]:
    syncer = Syncer(config, profile, paths)
    if not syncer.shared.exists:
        return [Check(WARN, "profile is not initialised", "run 'wowsync init'")]

    checks: list[Check] = []
    tracked = syncer.shared.tracked_files()
    checks.append(Check(OK, f"{len(tracked)} file(s) tracked for sync"))

    leaked = [
        path
        for pattern in profile.machine_local
        for path in tracked
        if Path(path).match(pattern)
    ]
    if leaked:
        checks.append(
            Check(
                BAD,
                f"{len(leaked)} machine-specific file(s) are being shared",
                ", ".join(leaked[:3]) + " -- the next sync will untrack them, but until "
                "then the other machine may have overwritten these settings.",
            )
        )
    else:
        checks.append(Check(OK, "no machine-specific files in the shared set"))

    for rule in profile.saved_variables:
        matches = list(profile.game_dir.glob(rule.file))
        if not matches:
            checks.append(Check(WARN, f"no file matches {rule.file}", "rule has no effect"))
            continue
        for match in matches:
            try:
                present = set(global_names(match.read_text("utf-8", errors="replace")))
            except OSError:
                continue
            missing = [g for g in rule.globals if g not in present]
            if missing:
                checks.append(
                    Check(
                        WARN,
                        f"{match.name} does not define {', '.join(missing)}",
                        "check the spelling against the addon's SavedVariables file",
                    )
                )
            else:
                checks.append(Check(OK, f"{match.name}: keeping {', '.join(rule.globals)} local"))

    conflict = syncer.pending_conflict()
    if conflict:
        checks.append(
            Check(
                BAD,
                "a conflict is waiting to be resolved",
                f"both sides changed {len(syncer.conflict_files())} file(s); "
                "run 'wowsync resolve --list'",
            )
        )
    return checks


def _server(config: Config, profile: Profile) -> list[Check]:
    checks: list[Check] = []
    if not profile.git_remote:
        checks.append(Check(BAD, "no git remote configured", "nothing can be synced"))
    else:
        probe = run(
            ["git", "ls-remote", "--exit-code", "-h", profile.git_remote, "main"],
            check=False,
            env={"GIT_SSH_COMMAND": "ssh -o ConnectTimeout=10 -o BatchMode=yes",
                 "GIT_TERMINAL_PROMPT": "0"},
            timeout=30,
        )
        if probe.returncode == 0:
            checks.append(Check(OK, f"git remote reachable: {profile.git_remote}"))
        elif probe.returncode == 2:
            checks.append(
                Check(WARN, "git remote is reachable but empty",
                      "the first push from either machine will seed it")
            )
        else:
            checks.append(
                Check(BAD, "cannot reach the git remote",
                      probe.stderr.decode("utf-8", "replace").strip()[:200])
            )

    if not config.server.url:
        checks.append(
            Check(
                WARN,
                "no coordinator configured",
                "sync still works, but nothing stops both machines from playing at "
                "once, and the idle machine will only notice new state on its poll.",
            )
        )
        return checks

    client = LeaseClient(config.server.url, config.server.token, timeout=5)
    try:
        state = client.status(profile.name)
    except CoordinatorUnreachable as exc:
        checks.append(Check(BAD, "coordinator unreachable", f"{config.server.url}: {exc}"))
        return checks

    if state.held:
        note = " (stale, will expire)" if state.stale else ""
        checks.append(Check(OK, f"coordinator up; {state.machine} is playing{note}"))
    else:
        checks.append(Check(OK, "coordinator up; nobody is playing"))
    return checks


def _disk(profile: Profile, paths: ProfilePaths) -> list[Check]:
    try:
        usage = shutil.disk_usage(profile.game_dir)
    except OSError:
        return []
    game_bytes = _tree_size(profile.game_dir / "Interface" / "AddOns")
    free_gb = usage.free / 1e9
    level = OK if free_gb > 5 else WARN
    return [
        Check(
            level,
            f"{free_gb:.1f} GB free; addons are {game_bytes / 1e9:.2f} GB",
            "History is stored compressed and deduplicated, but leave room for a few "
            "copies of the addon folder." if level == WARN else "",
        )
    ]


def _tree_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
    return total
