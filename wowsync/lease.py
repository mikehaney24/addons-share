"""Client for the coordinator: lease ownership and the event stream."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from .util import log

API = "/v1"


class CoordinatorUnreachable(Exception):
    """The server did not answer. Callers decide whether that is fatal."""


@dataclass
class LeaseState:
    held: bool
    machine: str | None = None
    held_for: float = 0.0
    stale: bool = False
    detail: str = ""


class LeaseClient:
    def __init__(self, url: str, token: str = "", timeout: float = 8.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._lease_token: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.url)

    # -- transport ------------------------------------------------------

    def _request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        if not self.configured:
            raise CoordinatorUnreachable("no server url configured")
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(f"{self.url}{path}", data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except json.JSONDecodeError:
                return exc.code, {}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CoordinatorUnreachable(str(exc)) from exc

    # -- lease ----------------------------------------------------------

    def status(self, profile: str) -> LeaseState:
        _, body = self._request("GET", f"{API}/status?profile={profile}")
        lease = body.get("lease")
        if not lease:
            return LeaseState(held=False)
        return LeaseState(
            held=True,
            machine=lease.get("machine"),
            held_for=float(lease.get("held_for") or 0),
            stale=bool(lease.get("stale")),
        )

    def acquire(self, profile: str, machine: str, ttl: float, steal: bool = False) -> LeaseState:
        code, body = self._request(
            "POST",
            f"{API}/lease/acquire",
            {"profile": profile, "machine": machine, "ttl": ttl, "steal": steal},
        )
        if code == 200:
            self._lease_token = body.get("token")
            return LeaseState(held=True, machine=machine, detail="acquired")
        lease = body.get("lease") or {}
        return LeaseState(
            held=False,
            machine=lease.get("machine"),
            held_for=float(lease.get("held_for") or 0),
            stale=bool(lease.get("stale")),
            detail=body.get("error", "denied"),
        )

    def heartbeat(self, profile: str) -> bool:
        if not self._lease_token:
            return False
        code, _ = self._request(
            "POST", f"{API}/lease/heartbeat", {"profile": profile, "token": self._lease_token}
        )
        if code != 200:
            log.warning("lease heartbeat rejected; another machine may have taken over")
            self._lease_token = None
            return False
        return True

    def release(self, profile: str, machine: str, force: bool = False) -> bool:
        code, _ = self._request(
            "POST",
            f"{API}/lease/release",
            {
                "profile": profile,
                "machine": machine,
                "token": self._lease_token or "",
                "force": force,
            },
        )
        self._lease_token = None
        return code == 200

    @property
    def holds_lease(self) -> bool:
        return self._lease_token is not None

    def notify(self, profile: str, machine: str, event: str = "state-changed") -> None:
        """Tell the other machine there is something new to pull."""
        try:
            self._request(
                "POST", f"{API}/notify", {"profile": profile, "machine": machine, "event": event}
            )
        except CoordinatorUnreachable:
            log.debug("could not publish %s notification", event)

    # -- events ---------------------------------------------------------

    def watch(self, on_event: Callable[[str, dict], None], stop: threading.Event) -> None:
        """Follow the server's event stream until `stop` is set.

        Runs in its own thread. Any failure just ends the attempt; the daemon's
        periodic poll is the backstop, so a dropped stream slows sync down but
        never breaks it.
        """
        request = urllib.request.Request(f"{self.url}{API}/events")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=None) as stream:
                event = "message"
                while not stop.is_set():
                    raw = stream.readline()
                    if not raw:
                        return
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    if line.startswith("event: "):
                        event = line[len("event: ") :].strip()
                    elif line.startswith("data: "):
                        try:
                            payload = json.loads(line[len("data: ") :])
                        except json.JSONDecodeError:
                            payload = {}
                        on_event(event, payload)
                        event = "message"
        except Exception as exc:  # noqa: BLE001 - a dropped stream is not fatal
            log.debug("event stream ended: %s", exc)


def follow_events(
    client: LeaseClient, on_event: Callable[[str, dict], None], stop: threading.Event
) -> threading.Thread:
    """Keep an event subscription alive, reconnecting with a backoff."""

    def loop():
        delay = 1.0
        while not stop.is_set():
            started = time.monotonic()
            client.watch(on_event, stop)
            if stop.is_set():
                return
            # A stream that survived a while is probably healthy; reset the backoff.
            delay = 1.0 if time.monotonic() - started > 30 else min(delay * 2, 60.0)
            stop.wait(delay)

    thread = threading.Thread(target=loop, name="wowsync-events", daemon=True)
    thread.start()
    return thread
