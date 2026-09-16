"""Command line interface."""

from __future__ import annotations

import argparse
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, config as config_module
from .config import Config, Profile
from .daemon import Daemon
from .doctor import BAD, OK, WARN, run_checks
from .lease import CoordinatorUnreachable, LeaseClient
from .paths import ProfilePaths, config_file, find_flavor_dirs, state_home
from .procwatch import DEFAULT_PROCESS_PATTERN, is_running
from .server import serve
from .syncer import Syncer
from .util import Fail, atomic_write_text, default_machine_id, human_age, log, setup_logging

SYMBOL = {OK: "  ok  ", WARN: " warn ", BAD: " FAIL "}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wowsync",
        description="Hand a WoW addon and settings state between computers, with history.",
    )
    parser.add_argument("--version", action="version", version=f"wowsync {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every git call")
    parser.add_argument("--profile", help="which configured profile to act on")
    parser.add_argument("--config", type=Path, help="path to wowsync.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="detect the game and write a config file")
    init.add_argument("--game-dir", type=Path, help="the flavor directory to sync")
    init.add_argument("--name", default="forever", help="profile name (default: forever)")
    init.add_argument("--remote", default="", help="git remote, e.g. ssh://user@host/srv/wow.git")
    init.add_argument("--server", default="", help="coordinator url, e.g. http://host:7373")
    init.add_argument("--token", default="", help="shared coordinator token")
    init.add_argument("--machine-id", default="", help="name for this computer")
    init.add_argument("--force", action="store_true", help="overwrite an existing config")

    sub.add_parser("status", help="what is synced, who is playing, what is pending")
    sub.add_parser("doctor", help="check this machine's setup")

    pull = sub.add_parser("pull", help="take the other machine's state")
    pull.add_argument("--adopt", action="store_true",
                      help="replace unrelated local state with the server's")
    sub.add_parser("push", help="publish this machine's state")
    sub.add_parser("sync", help="pull, then push")

    snapshot = sub.add_parser("snapshot", help="record the current state without syncing")
    snapshot.add_argument("-m", "--message", default="manual snapshot")

    history = sub.add_parser("log", help="list snapshots")
    history.add_argument("-n", "--limit", type=int, default=20)
    history.add_argument("--since", help="e.g. '3 days ago'")
    history.add_argument("--machine", help="only snapshots from this machine")

    show = sub.add_parser("show", help="print a file as it was at a snapshot")
    show.add_argument("revision")
    show.add_argument("path")

    diff = sub.add_parser("diff", help="what changed between two snapshots")
    diff.add_argument("revision", nargs="?", default="HEAD~1")
    diff.add_argument("other", nargs="?", default="HEAD")
    diff.add_argument("--stat", action="store_true", help="summarise instead of full text")

    restore = sub.add_parser("restore", help="roll the shared state back to a snapshot")
    restore.add_argument("revision", help="a snapshot id, or HEAD@{2 days ago}")
    restore.add_argument("--yes", action="store_true", help="skip the confirmation")

    resolve = sub.add_parser("resolve", help="settle a conflict between the two machines")
    group = resolve.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="show the disputed files")
    group.add_argument("--take-local", action="store_true", help="keep this machine's version")
    group.add_argument("--take-server", action="store_true", help="keep the server's version")

    play = sub.add_parser(
        "play", help="sync, launch the game, then sync again when it exits"
    )
    # Not "command": the subparser already uses that dest for the subcommand name.
    play.add_argument("launch", nargs=argparse.REMAINDER, metavar="command",
                      help="how to launch the game; omit to just wait for it")
    play.add_argument("--steal", action="store_true",
                      help="take the lease even if the other machine holds it")

    sub.add_parser("daemon", help="run the background agent")

    server = sub.add_parser("serve", help="run the coordinator (on the home server)")
    server.add_argument("--host", default="0.0.0.0")
    server.add_argument("--port", type=int, default=7373)
    server.add_argument("--token", default="", help="require this bearer token")
    server.add_argument("--state-dir", type=Path, default=None)

    return parser


# -- helpers -------------------------------------------------------------


def load(args) -> Config:
    return config_module.load(args.config)


def resolve_profile(args) -> tuple[Config, Profile, Syncer]:
    config = load(args)
    profile = config.profile(args.profile)
    syncer = Syncer(config, profile, ProfilePaths(profile.name))
    syncer.require_init()
    return config, profile, syncer


def report(result) -> int:
    messages = {
        "up-to-date": "Already current.",
        "updated": "Took the other machine's state.",
        "adopted": "Adopted the server's state.",
        "merged": "Combined both machines' changes.",
        "pushed": "Published this machine's state.",
        "captured": "Snapshot recorded.",
        "unchanged": "Nothing changed.",
        "ahead": "This machine has changes the server does not; run 'wowsync push'.",
        "nothing-to-push": "Nothing recorded yet.",
        "no-remote-state": "The server has no state yet; run 'wowsync push' to seed it.",
        "offline": "Could not reach the server.",
        "restored": "Rolled back.",
        "resolved": "Conflict resolved.",
        "nothing-to-resolve": "No conflict is pending.",
        "unrelated": "This machine's history is unrelated to the server's.",
        "diverged": "Both machines changed the same settings.",
        "error": "Failed.",
    }
    line = messages.get(result.status, result.status)
    print(f"{line} {result.detail}".rstrip())
    if result.status in ("diverged", "unrelated"):
        print("Nothing was lost. Run 'wowsync resolve --list' to see the disputed files.")
    return 0 if result.ok else 1


# -- commands ------------------------------------------------------------


def cmd_init(args) -> int:
    target = args.config or config_file()
    if target.exists() and not args.force:
        raise Fail(f"{target} already exists; pass --force to overwrite")

    game_dir = args.game_dir
    if game_dir is None:
        print("Looking for a game directory...", file=sys.stderr)
        found = find_flavor_dirs()
        if not found:
            raise Fail(
                "no game directory found. Pass --game-dir pointing at the folder that "
                "contains WTF/ and Interface/ (on Linux this is inside the Proton "
                "prefix, under drive_c)."
            )
        if len(found) > 1:
            print("Found several; pass --game-dir to choose one:", file=sys.stderr)
            for path in found:
                print(f"  {path}", file=sys.stderr)
            return 1
        game_dir = found[0]
        print(f"Found {game_dir}", file=sys.stderr)

    game_dir = game_dir.expanduser().resolve()
    body = render_config(
        machine_id=args.machine_id or default_machine_id(),
        profile=args.name,
        game_dir=game_dir,
        remote=args.remote,
        server=args.server,
        token=args.token,
    )
    atomic_write_text(target, body)
    print(f"Wrote {target}")

    config = config_module.load(target)
    profile = config.profile(args.name)
    Syncer(config, profile, ProfilePaths(profile.name)).init()
    print(f"Initialised the local repository for profile {profile.name!r}.")
    print("Next: run 'wowsync doctor', then 'wowsync sync'.")
    return 0


def render_config(*, machine_id, profile, game_dir, remote, server, token) -> str:
    return f"""# wowsync configuration -- see docs/ in the repository for the full reference.

# What this computer is called in history and in lease messages.
machine_id = {machine_id!r}

[server]
# The coordinator decides which machine currently owns the live state.
# Leave url empty to sync without one (no protection against playing on both).
url = {server!r}
token = {token!r}
# Git does the actual data transfer. SSH is used so large addon folders and
# resumed transfers behave, and so there is no web service in the data path.
git_remote = {remote!r}

[profile.{profile}]
# The flavor directory: the one containing WTF/ and Interface/.
game_dir = {str(game_dir)!r}
# How to recognise the running game. The default covers the macOS binary and
# the Windows executable as Wine reports it; override if your build differs.
process_match = {DEFAULT_PROCESS_PATTERN!r}
enabled = true

[profile.{profile}.sync]
# Extra paths to share, added to the defaults (addon folders, SavedVariables,
# keybindings, macros, per-character addon enable state, Edit Mode layouts).
include = []
# Extra paths to keep out of sync entirely.
exclude = []
# Where a same-file collision between machines may be settled automatically in
# the server's favour. Addon payloads qualify -- they are reinstallable.
# Anything under WTF/ deliberately does not.
auto_merge_prefixes = ["Interface/AddOns"]

[profile.{profile}.machine_local]
# Files that stay on the machine that wrote them. Config.wtf holds resolution,
# graphics API, display mode and sound device; config-cache.wtf holds the
# account and per-character copies of the same CVar store. These are versioned
# per machine, so they are restorable, but they are never shared.
files = [
  "WTF/Config.wtf",
  "WTF/Account/*/config-cache.wtf",
  "WTF/Account/*/*/*/config-cache.wtf",
]

# Some addons keep machine-specific settings inside an otherwise shared
# SavedVariables file. Name the saved globals that should stay local and they
# are carved out of the shared copy and put back after every sync:
#
# [[profile.{profile}.machine_local.saved_variables]]
# file = "WTF/Account/*/SavedVariables/ElvUI.lua"
# globals = ["ElvPrivateDB"]

[daemon]
poll_seconds = 2
remote_poll_seconds = 30
snapshot_minutes = 5
lease_ttl_seconds = 180
heartbeat_seconds = 30
"""


def cmd_status(args) -> int:
    config = load(args)
    print(f"machine: {config.machine_id}")
    for name, profile in sorted(config.profiles.items()):
        if not profile.enabled:
            print(f"\n[{name}] disabled")
            continue
        syncer = Syncer(config, profile, ProfilePaths(name))
        print(f"\n[{name}] {profile.game_dir}")
        if not syncer.shared.exists:
            print("  not initialised; run 'wowsync init'")
            continue

        head = syncer.shared.head()
        if head:
            latest = syncer.shared.log(limit=1)[0]
            age = human_age(time.time() - latest.when)
            print(f"  latest snapshot: {latest.sha[:10]}  {age} ago  {latest.subject}")
        else:
            print("  no snapshots yet")

        state = syncer.load_state()
        remote = syncer.shared.ref("refs/remotes/origin/main")
        if remote and head:
            if remote == head:
                print("  in sync with the server")
            elif syncer.shared.is_ancestor(head, remote):
                print("  behind the server; run 'wowsync pull'")
            elif syncer.shared.is_ancestor(remote, head):
                print("  ahead of the server; run 'wowsync push'")
            else:
                print("  diverged from the server; run 'wowsync resolve --list'")
        elif not state.get("synced_once"):
            print("  never synced with the server")

        conflict = syncer.pending_conflict()
        if conflict:
            print(f"  CONFLICT pending since {human_age(time.time() - conflict['at'])} ago")

        print(f"  game running here: {'yes' if is_running(profile.process_match) else 'no'}")
        _print_lease(config, name)
    return 0


def _print_lease(config: Config, profile_name: str) -> None:
    if not config.server.url:
        print("  coordinator: not configured")
        return
    client = LeaseClient(config.server.url, config.server.token, timeout=5)
    try:
        state = client.status(profile_name)
    except CoordinatorUnreachable:
        print("  coordinator: unreachable")
        return
    if not state.held:
        print("  coordinator: nobody is playing")
    else:
        stale = " (stale)" if state.stale else ""
        print(f"  coordinator: {state.machine} playing for {human_age(state.held_for)}{stale}")


def cmd_doctor(args) -> int:
    config = load(args)
    profile = config.profile(args.profile)
    checks = run_checks(config, profile)
    for check in checks:
        print(f"[{SYMBOL[check.level]}] {check.title}")
        if check.detail:
            for line in _wrap(check.detail):
                print(f"          {line}")
    failures = sum(1 for c in checks if c.level == BAD)
    warnings = sum(1 for c in checks if c.level == WARN)
    print(f"\n{failures} problem(s), {warnings} warning(s).")
    return 1 if failures else 0


def _wrap(text: str, width: int = 74) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width=width) or [""]


def cmd_pull(args) -> int:
    _, _, syncer = resolve_profile(args)
    return report(syncer.pull(adopt=args.adopt, subject="local changes before pull"))


def cmd_push(args) -> int:
    _, _, syncer = resolve_profile(args)
    syncer.capture("manual push")
    return report(syncer.push())


def cmd_sync(args) -> int:
    config, profile, syncer = resolve_profile(args)
    if is_running(profile.process_match):
        raise Fail(
            "the game is running on this machine. Quit it first -- WoW rewrites its "
            "settings on exit and would overwrite anything synced in now."
        )
    result = syncer.sync("manual sync")
    if result.status == "pushed" and config.server.url:
        try:
            LeaseClient(config.server.url, config.server.token).notify(
                profile.name, config.machine_id
            )
        except CoordinatorUnreachable:
            pass
    return report(result)


def cmd_snapshot(args) -> int:
    _, _, syncer = resolve_profile(args)
    return report(syncer.capture(args.message))


def cmd_log(args) -> int:
    _, _, syncer = resolve_profile(args)
    commits = syncer.shared.log(limit=args.limit, since=args.since)
    if args.machine:
        commits = [c for c in commits if c.machine == args.machine]
    if not commits:
        print("No snapshots yet.")
        return 0
    now = time.time()
    for commit in commits:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(commit.when))
        print(f"{commit.sha[:10]}  {when}  ({human_age(now - commit.when)} ago)  {commit.subject}")
    return 0


def cmd_show(args) -> int:
    _, _, syncer = resolve_profile(args)
    sys.stdout.write(syncer.shared.out("show", f"{args.revision}:{args.path}"))
    return 0


def cmd_diff(args) -> int:
    _, _, syncer = resolve_profile(args)
    flag = "--stat" if args.stat else "--patch"
    print(syncer.shared.out("diff", flag, args.revision, args.other, check=False))
    return 0


def cmd_restore(args) -> int:
    config, profile, syncer = resolve_profile(args)
    if is_running(profile.process_match):
        raise Fail("quit the game before restoring; it would overwrite the result on exit.")
    if not args.yes:
        target = syncer.shared.ref(args.revision)
        if target is None:
            raise Fail(f"unknown snapshot: {args.revision}")
        print(f"This will replace the shared state with the tree from {target[:10]}.")
        print("The current state stays in history and can be restored the same way.")
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1
    result = syncer.restore(args.revision)
    report(result)
    return report(syncer.push())


def cmd_resolve(args) -> int:
    _, _, syncer = resolve_profile(args)
    conflict = syncer.pending_conflict()
    if not conflict:
        print("No conflict is pending.")
        return 0

    if args.take_local:
        return report(syncer.resolve("local"))
    if args.take_server:
        return report(syncer.resolve("server"))

    files = syncer.conflict_files()
    print(f"Both machines changed {len(files)} file(s) since they last agreed:\n")
    for path in files[:50]:
        print(f"  {path}")
    if len(files) > 50:
        print(f"  ... and {len(files) - 50} more")
    print(
        f"\nThis machine's version is kept on {conflict['branch']}."
        "\nCompare a file:   wowsync show " + conflict["local"][:10] + " <path>"
        "\n                  wowsync show " + conflict["remote"][:10] + " <path>"
        "\nThen choose:      wowsync resolve --take-local"
        "\n                  wowsync resolve --take-server"
    )
    return 0


def cmd_play(args) -> int:
    config, profile, syncer = resolve_profile(args)
    client = LeaseClient(config.server.url, config.server.token)

    if client.configured:
        try:
            state = client.acquire(
                profile.name, config.machine_id, config.daemon.lease_ttl_seconds, steal=args.steal
            )
            if not state.held:
                raise Fail(
                    f"{state.machine} is playing (for {human_age(state.held_for)}). "
                    "Quit there first, or pass --steal if that machine is gone."
                )
        except CoordinatorUnreachable as exc:
            print(f"Warning: coordinator unreachable ({exc}); playing unmanaged.", file=sys.stderr)

    result = syncer.pull()
    if result.status == "diverged":
        report(result)
        if client.holds_lease:
            client.release(profile.name, config.machine_id)
        return 1
    report(result)

    command = [a for a in args.launch if a != "--"]
    try:
        if command:
            print(f"Launching: {shlex.join(command)}")
            subprocess.Popen(command, start_new_session=True)
        else:
            print("Waiting for the game to start...")
        _wait_for_session(profile, client, config)
    except KeyboardInterrupt:
        print("\nInterrupted; saving what is on disk.", file=sys.stderr)

    syncer.capture("session end")
    outcome = syncer.push()
    if client.holds_lease:
        client.release(profile.name, config.machine_id)
        client.notify(profile.name, config.machine_id)
    return report(outcome)


def _wait_for_session(profile: Profile, client: LeaseClient, config: Config) -> None:
    deadline = time.time() + 300
    while not is_running(profile.process_match):
        if time.time() > deadline:
            print("The game never started; nothing to do.", file=sys.stderr)
            return
        time.sleep(2)
    print("Game running. Leave this open; it will sync when you quit.")
    last_beat = 0.0
    while is_running(profile.process_match):
        now = time.time()
        if client.holds_lease and now - last_beat >= config.daemon.heartbeat_seconds:
            last_beat = now
            try:
                client.heartbeat(profile.name)
            except CoordinatorUnreachable:
                pass
        time.sleep(2)
    print("Game exited; waiting for it to finish writing settings.")


def cmd_daemon(args) -> int:
    config = load(args)
    daemon = Daemon(config)

    def shutdown(signum, frame):
        daemon.stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    daemon.run()
    return 0


def cmd_serve(args) -> int:
    state_dir = args.state_dir or (state_home() / "coordinator")
    httpd = serve(args.host, args.port, state_dir, token=args.token)
    host, port = httpd.server_address[:2]
    log.info("coordinator listening on http://%s:%s (state in %s)", host, port, state_dir)
    if not args.token:
        log.warning("no --token set: anyone on the network can take the lease")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


COMMANDS = {
    "init": cmd_init,
    "status": cmd_status,
    "doctor": cmd_doctor,
    "pull": cmd_pull,
    "push": cmd_push,
    "sync": cmd_sync,
    "snapshot": cmd_snapshot,
    "log": cmd_log,
    "show": cmd_show,
    "diff": cmd_diff,
    "restore": cmd_restore,
    "resolve": cmd_resolve,
    "play": cmd_play,
    "daemon": cmd_daemon,
    "serve": cmd_serve,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logfile = None
    if args.command in ("daemon", "serve"):
        logfile = state_home() / "logs" / f"{args.command}.log"
    setup_logging(args.verbose, logfile)
    try:
        return COMMANDS[args.command](args)
    except Fail as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
