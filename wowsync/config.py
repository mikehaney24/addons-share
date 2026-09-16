"""Configuration loading and validation."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .paths import config_file
from .util import Fail, default_machine_id

# Paths under the game directory that are shared between machines, as glob
# patterns relative to the flavor directory. WTF's layout is:
#   WTF/Account/<ACCOUNT>/...                       account-wide
#   WTF/Account/<ACCOUNT>/<REALM>/<CHARACTER>/...   per character
DEFAULT_INCLUDE = [
    "Interface/AddOns/**",
    "WTF/Account/*/SavedVariables/**",
    "WTF/Account/*/*/*/SavedVariables/**",
    "WTF/Account/*/bindings-cache.wtf",
    "WTF/Account/*/macros-cache.txt",
    "WTF/Account/*/layout-local.txt",
    "WTF/Account/*/*/*/AddOns.txt",
    "WTF/Account/*/*/*/bindings-cache.wtf",
    "WTF/Account/*/*/*/macros-cache.txt",
    "WTF/Account/*/*/*/layout-local.txt",
]

# Noise that must never enter the repo. WoW regenerates all of it.
DEFAULT_EXCLUDE = [
    "**/.DS_Store",
    "**/._*",
    "**/Thumbs.db",
    "**/*.bak",
    "**/*.lua.bak",
    "**/desktop.ini",
]

# Files that stay on the machine that wrote them. Config.wtf holds the graphics
# and sound CVars (resolution, gxApi, display mode, audio device); config-cache
# holds the per-account and per-character copies of the same CVar store.
DEFAULT_MACHINE_LOCAL = [
    "WTF/Config.wtf",
    "WTF/Account/*/config-cache.wtf",
    "WTF/Account/*/*/*/config-cache.wtf",
]


@dataclass
class SavedVarRule:
    """Keep named globals of a SavedVariables file on this machine only."""

    file: str
    globals: list[str]


@dataclass
class Profile:
    name: str
    game_dir: Path
    process_match: str
    enabled: bool = True
    include: list[str] = field(default_factory=lambda: list(DEFAULT_INCLUDE))
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    machine_local: list[str] = field(default_factory=lambda: list(DEFAULT_MACHINE_LOCAL))
    saved_variables: list[SavedVarRule] = field(default_factory=list)
    git_remote: str = ""
    # Where a same-file collision between two machines may be resolved in
    # favour of the server instead of being reported. Addon folders qualify:
    # they are installed files that either updater can reproduce. Nothing under
    # WTF/ does, because that is the settings you would lose.
    auto_merge_prefixes: list[str] = field(default_factory=lambda: ["Interface/AddOns"])

    def validate(self) -> None:
        if not self.game_dir.is_absolute():
            raise Fail(f"profile {self.name}: game_dir must be an absolute path")
        for pattern in self.include + self.exclude + self.machine_local:
            if pattern.startswith("/") or ".." in Path(pattern).parts:
                raise Fail(
                    f"profile {self.name}: pattern {pattern!r} must be relative "
                    "to game_dir and may not contain '..'"
                )


@dataclass
class ServerConfig:
    url: str = ""
    token: str = ""
    git_remote: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.url)


@dataclass
class DaemonConfig:
    poll_seconds: float = 2.0
    remote_poll_seconds: float = 30.0
    snapshot_minutes: float = 5.0
    lease_ttl_seconds: float = 180.0
    heartbeat_seconds: float = 30.0
    settle_seconds: float = 3.0
    # How long after WoW starts we still consider it safe to write SavedVariables
    # underneath it: the client reads them at character login, not at launch, so
    # a sync that lands while the login screen is up is still picked up.
    login_grace_seconds: float = 45.0


@dataclass
class Config:
    machine_id: str
    server: ServerConfig
    daemon: DaemonConfig
    profiles: dict[str, Profile]
    source: Path | None = None

    def profile(self, name: str | None) -> Profile:
        enabled = {k: v for k, v in self.profiles.items() if v.enabled}
        if not enabled:
            raise Fail("no enabled profiles configured; run 'wowsync init'")
        if name is None:
            if len(enabled) > 1:
                raise Fail(
                    "multiple profiles configured ("
                    + ", ".join(sorted(enabled))
                    + "); pass --profile"
                )
            return next(iter(enabled.values()))
        if name not in self.profiles:
            raise Fail(f"unknown profile {name!r}; known: {', '.join(sorted(self.profiles))}")
        return self.profiles[name]


def load(path: Path | None = None) -> Config:
    path = path or config_file()
    if not path.exists():
        raise Fail(f"no config at {path}; run 'wowsync init' first")
    try:
        raw = tomllib.loads(path.read_text("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise Fail(f"{path}: invalid TOML: {exc}") from exc
    cfg = from_dict(raw)
    cfg.source = path
    return cfg


def from_dict(raw: dict) -> Config:
    server_raw = raw.get("server", {})
    server = ServerConfig(
        url=str(server_raw.get("url", "")).rstrip("/"),
        token=str(server_raw.get("token", "")),
        git_remote=str(server_raw.get("git_remote", "")),
    )

    daemon_raw = raw.get("daemon", {})
    daemon = DaemonConfig(
        **{
            key: float(daemon_raw[key])
            for key in DaemonConfig.__dataclass_fields__
            if key in daemon_raw
        }
    )

    profiles: dict[str, Profile] = {}
    for name, pr in (raw.get("profile") or {}).items():
        if not isinstance(pr, dict):
            raise Fail(f"profile {name}: expected a table")
        if "game_dir" not in pr:
            raise Fail(f"profile {name}: game_dir is required")

        ml_raw = pr.get("machine_local") or {}
        rules = [
            SavedVarRule(file=str(r["file"]), globals=[str(g) for g in r.get("globals", [])])
            for r in (ml_raw.get("saved_variables") or [])
        ]
        sync_raw = pr.get("sync") or {}

        profile = Profile(
            name=name,
            game_dir=Path(str(pr["game_dir"])).expanduser(),
            process_match=str(pr.get("process_match", "")),
            enabled=bool(pr.get("enabled", True)),
            include=_merge(DEFAULT_INCLUDE, sync_raw),
            exclude=_merge(DEFAULT_EXCLUDE, sync_raw, key="exclude"),
            machine_local=_merge(DEFAULT_MACHINE_LOCAL, ml_raw, key="files"),
            saved_variables=rules,
            git_remote=str(pr.get("git_remote", "")) or server.git_remote,
            auto_merge_prefixes=[
                str(v) for v in sync_raw.get("auto_merge_prefixes", ["Interface/AddOns"])
            ],
        )
        profile.validate()
        profiles[name] = profile

    return Config(
        machine_id=str(raw.get("machine_id") or default_machine_id()),
        server=server,
        daemon=daemon,
        profiles=profiles,
    )


def _merge(defaults: list[str], section: dict, key: str = "include") -> list[str]:
    """Defaults plus additions, unless the section replaces them outright."""
    if section.get(f"replace_{key}"):
        return [str(v) for v in section.get(key, [])]
    merged = list(defaults)
    for value in section.get(key, []):
        if str(value) not in merged:
            merged.append(str(value))
    return merged
