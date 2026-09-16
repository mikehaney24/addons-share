"""Small shared helpers: logging, subprocess, notifications, atomic writes."""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

log = logging.getLogger("wowsync")


class Fail(Exception):
    """User-facing error. The CLI prints the message and exits non-zero."""


def setup_logging(verbose: bool = False, logfile: Path | None = None) -> None:
    root = logging.getLogger("wowsync")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter("%(message)s"))
    stream.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(stream)

    if logfile is not None:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logfile)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        fh.setLevel(logging.DEBUG)
        root.addHandler(fh)


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    stdin: bytes | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Run a command, capturing output. Raises Fail on non-zero when check=True."""
    log.debug("run: %s", " ".join(args))
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env=full_env,
        input=stdin,
        capture_output=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise Fail(
            f"command failed ({proc.returncode}): {' '.join(args)}\n"
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc


def which(name: str) -> str | None:
    return shutil.which(name)


def is_macos() -> bool:
    return platform.system() == "Darwin"


def is_linux() -> bool:
    return platform.system() == "Linux"


def default_machine_id() -> str:
    host = platform.node().split(".")[0].strip()
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in host)
    return safe.strip("-").lower() or "unknown-machine"


def notify(title: str, message: str) -> None:
    """Best-effort desktop notification. Never raises."""
    try:
        if is_macos():
            script = (
                f'display notification {json.dumps(message)} '
                f'with title {json.dumps(title)}'
            )
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
        elif which("notify-send"):
            subprocess.run(
                ["notify-send", "--app-name=wowsync", title, message],
                capture_output=True,
                timeout=5,
            )
    except Exception:  # notifications are decoration, never a failure path
        log.debug("desktop notification failed", exc_info=True)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".wowsync-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, obj) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=True) + "\n")


def wait_until_quiet(paths: list[Path], quiet_seconds: float = 3.0, max_wait: float = 90.0) -> bool:
    """Block until none of `paths` have changed for `quiet_seconds`.

    WoW rewrites every SavedVariables file as it shuts down; committing mid-write
    would capture a truncated Lua file. Returns False if max_wait elapsed first.
    """
    deadline = time.monotonic() + max_wait
    last_sig = None
    stable_since = None
    while time.monotonic() < deadline:
        sig = _tree_signature(paths)
        if sig != last_sig:
            last_sig = sig
            stable_since = time.monotonic()
        elif stable_since is not None and time.monotonic() - stable_since >= quiet_seconds:
            return True
        time.sleep(0.5)
    return False


def _tree_signature(paths: list[Path]) -> tuple:
    out = []
    for root in paths:
        if root.is_file():
            try:
                st = root.stat()
                out.append((str(root), st.st_mtime_ns, st.st_size))
            except OSError:
                continue
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                fp = os.path.join(dirpath, name)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                out.append((fp, st.st_mtime_ns, st.st_size))
    return tuple(out)


def human_age(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"
