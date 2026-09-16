"""Is the game running?

`ps -Ao pid=,args=` is the one process listing that behaves the same on macOS
and Linux, which matters because one of the two machines runs the Windows
client under Proton, where the binary shows up by its Windows name.
"""

from __future__ import annotations

import os
import re
import subprocess

# Covers the macOS app binary, the Windows executable as Wine reports it, and
# the PTR/beta variants, without matching launchers or the updater agent.
DEFAULT_PROCESS_PATTERN = r"(World of Warcraft(?! Launcher)|\bWow(Classic|T)?\.exe\b)"


def find_processes(pattern: str = "") -> list[tuple[int, str]]:
    """Every process whose command line matches, excluding this one."""
    expression = re.compile(pattern or DEFAULT_PROCESS_PATTERN, re.IGNORECASE)
    try:
        listing = subprocess.run(
            ["ps", "-Ao", "pid=,args="], capture_output=True, timeout=10
        ).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return []

    mine = os.getpid()
    matches = []
    for line in listing.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_text, _, args = line.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == mine:
            continue
        # The daemon's own command line mentions the profile and can mention the
        # pattern; matching ourselves would mean the game never appears to exit.
        if "wowsync" in args:
            continue
        if expression.search(args):
            matches.append((pid, args))
    return matches


def is_running(pattern: str = "") -> bool:
    return bool(find_processes(pattern))
