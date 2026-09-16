"""The always-on agent.

The handoff you actually want -- stop on one machine, start on the other, with
nothing to remember -- falls out of two rules:

  1. While the game is running here, this machine owns the state. It holds a
     lease, never pulls, and pushes periodic snapshots.
  2. While the game is not running here, this machine follows. It pulls the
     moment the other one finishes, rather than when you next sit down.

Rule 2 is what makes the walk between desks free: by the time you get there the
other computer has already caught up. The launch-time check is then just a
confirmation, not a download.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .config import Config, Profile
from .lease import CoordinatorUnreachable, LeaseClient, follow_events
from .paths import ProfilePaths
from .procwatch import is_running
from .syncer import Syncer
from .util import log, notify, wait_until_quiet

IDLE = "idle"
PLAYING = "playing"
UNMANAGED = "playing-without-lease"


@dataclass
class Worker:
    """Tracks one profile's half of the state machine."""

    config: Config
    profile: Profile
    client: LeaseClient
    paths: ProfilePaths | None = None
    syncer: Syncer = field(init=False)
    state: str = IDLE
    launched_at: float = 0.0
    last_heartbeat: float = 0.0
    last_snapshot: float = 0.0
    last_remote_check: float = 0.0
    pull_requested: bool = True
    warned_conflict: bool = False

    def __post_init__(self):
        self.syncer = Syncer(
            self.config, self.profile, self.paths or ProfilePaths(self.profile.name)
        )
        self.syncer.init()

    # -- transitions ----------------------------------------------------

    def tick(self, now: float) -> None:
        running = is_running(self.profile.process_match)
        if self.state == IDLE:
            if running:
                self._on_launch(now)
            else:
                self._tick_idle(now)
        else:
            if running:
                self._tick_playing(now)
            else:
                self._on_exit(now)

    def _on_launch(self, now: float) -> None:
        self.launched_at = now
        self.last_snapshot = now
        log.info("[%s] game started", self.profile.name)

        try:
            state = self.client.acquire(
                self.profile.name, self.config.machine_id, self.config.daemon.lease_ttl_seconds
            )
        except CoordinatorUnreachable as exc:
            log.warning("[%s] coordinator unreachable (%s)", self.profile.name, exc)
            notify(
                "wowsync: playing offline",
                "The sync server is unreachable. Your session will be saved locally "
                "and reconciled when it comes back.",
            )
            self.state = UNMANAGED
            return

        if not state.held:
            held_by = state.machine or "another machine"
            log.warning("[%s] lease is held by %s", self.profile.name, held_by)
            notify(
                f"wowsync: {held_by} is playing",
                f"{held_by} still holds this character's settings. Anything you change "
                "here will have to be reconciled by hand. Quit there first.",
            )
            self.state = UNMANAGED
            return

        self.state = PLAYING
        self.last_heartbeat = now
        self._catch_up_at_launch(now)

    def _catch_up_at_launch(self, now: float) -> None:
        """Apply anything outstanding while the login screen is still up.

        WoW reads SavedVariables when a character logs in, not when the process
        starts, so a sync landing in the first few seconds is picked up. After
        that window, writing underneath a live client would be overwritten on
        logout, so we tell the user instead of racing it.
        """
        result = self.syncer.pull()
        if result.status in ("updated", "adopted", "merged"):
            elapsed = time.time() - self.launched_at
            if elapsed > self.config.daemon.login_grace_seconds:
                notify(
                    "wowsync: synced late",
                    "New settings arrived after the client had started. Log out to "
                    "the character screen and back in if something looks stale.",
                )
            log.info("[%s] applied before login: %s", self.profile.name, result.detail)
        elif result.status == "diverged":
            self._warn_conflict(result.detail)

    def _tick_playing(self, now: float) -> None:
        daemon = self.config.daemon
        if self.state == PLAYING and now - self.last_heartbeat >= daemon.heartbeat_seconds:
            self.last_heartbeat = now
            try:
                if not self.client.heartbeat(self.profile.name):
                    # Someone took the lease. Keep playing, but stop pretending
                    # this machine is authoritative.
                    self.state = UNMANAGED
                    notify(
                        "wowsync: lost the lease",
                        "Another machine took over this character's settings while you "
                        "were playing. This session will need reconciling.",
                    )
            except CoordinatorUnreachable:
                log.debug("[%s] heartbeat failed; server down", self.profile.name)

        if now - self.last_snapshot >= daemon.snapshot_minutes * 60:
            self.last_snapshot = now
            # Committing only reads the game directory, so it is safe mid-session
            # and gives you a rollback point for a UI change you regret.
            result = self.syncer.capture("in-session snapshot")
            if result.status == "captured" and self.state == PLAYING:
                self.syncer.push()

    def _on_exit(self, now: float) -> None:
        log.info("[%s] game exited; saving session", self.profile.name)
        watched = [self.profile.game_dir / "WTF", self.profile.game_dir / "Interface" / "AddOns"]
        if not wait_until_quiet(
            [p for p in watched if p.exists()], quiet_seconds=self.config.daemon.settle_seconds
        ):
            log.warning("[%s] files still changing; capturing anyway", self.profile.name)

        was_unmanaged = self.state == UNMANAGED
        self.syncer.capture("session end")

        if was_unmanaged:
            # This session never owned the state, so pushing it blindly could
            # bury the other machine's. Let the ordinary pull path judge it.
            result = self.syncer.pull()
            if result.status == "diverged":
                self._warn_conflict(result.detail)
            else:
                self.syncer.push()
        else:
            pushed = self.syncer.push()
            if pushed.status == "diverged":
                self._warn_conflict(pushed.detail)

        try:
            self.client.release(self.profile.name, self.config.machine_id)
            self.client.notify(self.profile.name, self.config.machine_id)
        except CoordinatorUnreachable:
            log.debug("[%s] could not release the lease", self.profile.name)

        self.state = IDLE
        self.launched_at = 0.0
        log.info("[%s] session saved; the other machine can take over", self.profile.name)

    def _tick_idle(self, now: float) -> None:
        daemon = self.config.daemon
        due = now - self.last_remote_check >= daemon.remote_poll_seconds
        if not (self.pull_requested or due):
            return
        self.pull_requested = False
        self.last_remote_check = now

        result = self.syncer.sync("changes while idle")
        if result.status == "diverged":
            self._warn_conflict(result.detail)
        elif result.status in ("updated", "merged", "adopted"):
            self.warned_conflict = False
            log.info("[%s] %s: %s", self.profile.name, result.status, result.detail)
        elif result.status == "pushed":
            self.warned_conflict = False
            try:
                self.client.notify(self.profile.name, self.config.machine_id)
            except CoordinatorUnreachable:
                pass

    def _warn_conflict(self, detail: str) -> None:
        if self.warned_conflict:
            return
        self.warned_conflict = True
        log.error("[%s] %s", self.profile.name, detail)
        notify(
            "wowsync: needs your decision",
            "Both machines changed the same settings. Nothing was lost -- run "
            "'wowsync resolve' to pick a side.",
        )

    def on_remote_event(self, event: str, payload: dict) -> None:
        if payload.get("machine") == self.config.machine_id:
            return
        if payload.get("profile") not in (None, "", self.profile.name):
            return
        if self.state == IDLE:
            log.debug("[%s] remote event %s; scheduling a pull", self.profile.name, event)
            self.pull_requested = True


class Daemon:
    def __init__(self, config: Config):
        self.config = config
        self.client = LeaseClient(config.server.url, config.server.token)
        self.stop = threading.Event()
        self.workers = [
            Worker(config, profile, self.client)
            for profile in config.profiles.values()
            if profile.enabled
        ]

    def run(self) -> None:
        if not self.workers:
            log.error("no enabled profiles; nothing to do")
            return
        log.info(
            "wowsync daemon starting as %s for: %s",
            self.config.machine_id,
            ", ".join(w.profile.name for w in self.workers),
        )
        if self.client.configured:
            follow_events(self.client, self._dispatch, self.stop)
        else:
            log.warning("no server url configured; falling back to periodic polling only")

        while not self.stop.is_set():
            now = time.time()
            for worker in self.workers:
                try:
                    worker.tick(now)
                except Exception:  # noqa: BLE001 - one bad profile must not stop the rest
                    log.exception("[%s] tick failed", worker.profile.name)
            self.stop.wait(self.config.daemon.poll_seconds)

        self._shutdown()

    def _dispatch(self, event: str, payload: dict) -> None:
        for worker in self.workers:
            worker.on_remote_event(event, payload)

    def _shutdown(self) -> None:
        log.info("wowsync daemon stopping")
        for worker in self.workers:
            if worker.state == PLAYING:
                # Leaving a lease behind would make the other machine wait out
                # the TTL for no reason.
                try:
                    worker.client.release(worker.profile.name, self.config.machine_id)
                except CoordinatorUnreachable:
                    pass
