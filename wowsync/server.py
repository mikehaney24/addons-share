"""The coordinator: a small LAN service that decides which machine is playing.

It deliberately does not move any game data. Git over SSH already does that
well, including multi-gigabyte addon folders and resumable transfers. What git
cannot do is stop both computers from writing settings at once, so that is all
this service is for:

  * a lease -- exactly one machine may own the live state at a time,
  * a heartbeat, so a crashed client's lease expires instead of wedging,
  * a notification stream, so the idle machine pulls the moment the other
    one finishes rather than when you next sit down at it.
"""

from __future__ import annotations

import fcntl
import json
import queue
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .util import log, read_json, write_json

API = "/v1"


@dataclass
class Lease:
    profile: str
    machine: str
    token: str
    acquired_at: float
    heartbeat_at: float
    ttl: float
    note: str = ""

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) - self.heartbeat_at > self.ttl

    def public(self, now: float | None = None) -> dict:
        data = asdict(self)
        data.pop("token")
        data["held_for"] = (now or time.time()) - self.acquired_at
        data["stale"] = self.expired(now)
        return data


class LeaseStore:
    """Leases on disk, guarded by an advisory lock so concurrent requests and
    a restarted service agree on who holds what."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Lease]:
        raw = read_json(self.path, {})
        out = {}
        for profile, record in raw.items():
            try:
                out[profile] = Lease(**record)
            except TypeError:
                continue
        return out

    def _save(self, leases: dict[str, Lease]) -> None:
        write_json(self.path, {k: asdict(v) for k, v in leases.items()})

    def _with_file_lock(self, fn):
        with self._lock:
            lockfile = self.path.with_suffix(".lock")
            lockfile.parent.mkdir(parents=True, exist_ok=True)
            with open(lockfile, "w") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    return fn()
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def get(self, profile: str) -> Lease | None:
        return self._with_file_lock(lambda: self._load().get(profile))

    def all(self) -> dict[str, Lease]:
        return self._with_file_lock(self._load)

    def acquire(
        self, profile: str, machine: str, ttl: float, note: str = "", steal: bool = False
    ) -> tuple[bool, Lease]:
        def op():
            leases = self._load()
            now = time.time()
            current = leases.get(profile)
            if current and not current.expired(now) and current.machine != machine and not steal:
                return False, current
            if current and current.machine == machine and not current.expired(now):
                # Re-acquiring our own lease is a no-op, not an error: it is what
                # a client does after its own restart.
                current.heartbeat_at = now
                leases[profile] = current
                self._save(leases)
                return True, current
            lease = Lease(
                profile=profile,
                machine=machine,
                token=uuid.uuid4().hex,
                acquired_at=now,
                heartbeat_at=now,
                ttl=ttl,
                note=note,
            )
            leases[profile] = lease
            self._save(leases)
            return True, lease

        return self._with_file_lock(op)

    def heartbeat(self, profile: str, token: str) -> Lease | None:
        def op():
            leases = self._load()
            lease = leases.get(profile)
            if lease is None or lease.token != token:
                return None
            lease.heartbeat_at = time.time()
            leases[profile] = lease
            self._save(leases)
            return lease

        return self._with_file_lock(op)

    def release(self, profile: str, token: str, force: bool = False) -> bool:
        def op():
            leases = self._load()
            lease = leases.get(profile)
            if lease is None:
                return True
            if not force and lease.token != token:
                return False
            del leases[profile]
            self._save(leases)
            return True

        return self._with_file_lock(op)


class Broadcaster:
    """Fan-out for server-sent events. One queue per connected client."""

    def __init__(self) -> None:
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=64)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, event: str, payload: dict) -> None:
        message = f"event: {event}\ndata: {json.dumps(payload)}\n\n"
        with self._lock:
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put_nowait(message)
            except queue.Full:
                # A client that cannot keep up will resynchronise on its next
                # periodic poll; dropping the event is better than blocking.
                log.debug("dropping event for a slow subscriber")


class Coordinator:
    def __init__(self, state_dir: Path, token: str = ""):
        self.store = LeaseStore(state_dir / "leases.json")
        self.events = Broadcaster()
        self.token = token
        self.started_at = time.time()


class Handler(BaseHTTPRequestHandler):
    server_version = "wowsync-coordinator"
    coordinator: Coordinator  # injected by serve()

    # -- helpers --------------------------------------------------------

    def log_message(self, fmt, *args):  # quieter than the stdlib default
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _authorised(self) -> bool:
        expected = self.coordinator.token
        if not expected:
            return True
        header = self.headers.get("Authorization", "")
        return header.strip() == f"Bearer {expected}"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    # -- routing --------------------------------------------------------

    def do_GET(self):  # noqa: N802 - stdlib naming
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/":
            return self._status_page()
        if not self._authorised():
            return self._send(401, {"error": "unauthorised"})
        if url.path == f"{API}/status":
            return self._send(200, self._status(query.get("profile", [None])[0]))
        if url.path == f"{API}/events":
            return self._events()
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        if not self._authorised():
            return self._send(401, {"error": "unauthorised"})
        body = self._body()
        profile = str(body.get("profile") or "")
        machine = str(body.get("machine") or "")

        if url.path == f"{API}/lease/acquire":
            if not profile or not machine:
                return self._send(400, {"error": "profile and machine are required"})
            ok, lease = self.coordinator.store.acquire(
                profile,
                machine,
                ttl=float(body.get("ttl") or 180),
                note=str(body.get("note") or ""),
                steal=bool(body.get("steal")),
            )
            if not ok:
                return self._send(409, {"error": "held", "lease": lease.public()})
            self.coordinator.events.publish(
                "lease-acquired", {"profile": profile, "machine": machine}
            )
            return self._send(200, {"token": lease.token, "lease": lease.public()})

        if url.path == f"{API}/lease/heartbeat":
            lease = self.coordinator.store.heartbeat(profile, str(body.get("token") or ""))
            if lease is None:
                return self._send(409, {"error": "lease lost"})
            return self._send(200, {"lease": lease.public()})

        if url.path == f"{API}/lease/release":
            ok = self.coordinator.store.release(
                profile, str(body.get("token") or ""), force=bool(body.get("force"))
            )
            if not ok:
                return self._send(409, {"error": "not the lease holder"})
            self.coordinator.events.publish(
                "lease-released", {"profile": profile, "machine": machine}
            )
            return self._send(200, {"released": True})

        if url.path == f"{API}/notify":
            event = str(body.get("event") or "state-changed")
            self.coordinator.events.publish(
                event, {"profile": profile, "machine": machine, "at": time.time()}
            )
            return self._send(200, {"published": True})

        return self._send(404, {"error": "not found"})

    # -- endpoints ------------------------------------------------------

    def _status(self, profile: str | None) -> dict:
        now = time.time()
        leases = self.coordinator.store.all()
        if profile:
            lease = leases.get(profile)
            return {
                "now": now,
                "profile": profile,
                "lease": lease.public(now) if lease else None,
            }
        return {
            "now": now,
            "uptime": now - self.coordinator.started_at,
            "leases": {k: v.public(now) for k, v in leases.items()},
        }

    def _events(self):
        q = self.coordinator.events.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    message = q.get(timeout=20)
                except queue.Empty:
                    message = ": keepalive\n\n"  # keeps proxies and NAT from idling us out
                self.wfile.write(message.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.coordinator.events.unsubscribe(q)

    def _status_page(self):
        now = time.time()
        leases = self.coordinator.store.all()
        if leases:
            rows = "\n".join(
                f"<tr><td>{p}</td><td>{l.machine}</td>"
                f"<td>{int(now - l.acquired_at)}s</td>"
                f"<td>{'stale' if l.expired(now) else 'live'}</td></tr>"
                for p, l in sorted(leases.items())
            )
            table = (
                "<table><tr><th>profile</th><th>playing on</th>"
                f"<th>held</th><th>state</th></tr>{rows}</table>"
            )
        else:
            table = "<p>Nobody is playing.</p>"
        body = (
            "<!doctype html><meta charset=utf-8><title>wowsync</title>"
            "<style>body{font:14px system-ui;margin:2rem}"
            "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.4rem .8rem}"
            "</style><h1>wowsync coordinator</h1>" + table
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(host: str, port: int, state_dir: Path, token: str = "") -> ThreadingHTTPServer:
    coordinator = Coordinator(state_dir, token)
    handler = type("BoundHandler", (Handler,), {"coordinator": coordinator})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd
