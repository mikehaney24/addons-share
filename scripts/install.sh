#!/usr/bin/env bash
# Install wowsync for the current user and, optionally, register the background
# agent so handoffs happen without you thinking about them.
#
#   ./scripts/install.sh              # install the command only
#   ./scripts/install.sh --service    # also install and start the agent
#
# Nothing is compiled. wowsync is Python, and `wowsync` is a one-line launcher
# script generated at install time. It is installed into a private virtual
# environment so it cannot collide with -- or be broken by -- Homebrew, your
# distribution's package manager, or any other Python you have.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WITH_SERVICE=0
[ "${1:-}" = "--service" ] && WITH_SERVICE=1

WOWSYNC_HOME="${WOWSYNC_HOME:-$HOME/.local/share/wowsync}"
VENV="$WOWSYNC_HOME/venv"
BIN_DIR="${WOWSYNC_BIN_DIR:-$HOME/.local/bin}"
WOWSYNC="$BIN_DIR/wowsync"

say()  { printf '\n==> %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# -- prerequisites -------------------------------------------------------

command -v git >/dev/null || die "git is required (2.38 or newer)"

version_of() { "$1" -c 'import platform; print(platform.python_version())' 2>/dev/null; }
is_supported() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }

# Look past whatever `python3` happens to resolve to. macOS still ships 3.9 as
# /usr/bin/python3, and a perfectly good Homebrew or pyenv interpreter is often
# installed but not first on PATH.
find_python() {
    local candidate resolved
    for candidate in python3.14 python3.13 python3.12 python3.11 python3 \
                     /opt/homebrew/bin/python3 /usr/local/bin/python3 \
                     /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11; do
        resolved="$(command -v "$candidate" 2>/dev/null || true)"
        [ -n "$resolved" ] || { [ -x "$candidate" ] && resolved="$candidate"; }
        [ -n "$resolved" ] || continue
        if is_supported "$resolved"; then echo "$resolved"; return 0; fi
    done
    return 1
}

if [ -n "${PYTHON:-}" ]; then
    command -v "$PYTHON" >/dev/null 2>&1 || [ -x "$PYTHON" ] || die "PYTHON=$PYTHON not found"
    is_supported "$PYTHON" || die \
        "PYTHON=$PYTHON is $(version_of "$PYTHON"); wowsync needs 3.11 or newer"
elif ! PYTHON="$(find_python)"; then
    NEWEST="$(version_of python3 || echo "none")"
    cat >&2 <<PYHELP
error: no Python 3.11 or newer found (the python3 on your PATH is $NEWEST).

  Only one thing needs it: wowsync reads its config with 'tomllib', which
  entered the standard library in 3.11. Everything else runs on older
  versions -- but 3.9, which macOS ships, went end-of-life in October 2025,
  so it is not a good host for a background agent.

  On macOS:        brew install python@3.12
  On Debian/Ubuntu: sudo apt install python3.12 python3.12-venv
  On Fedora:       sudo dnf install python3.12

  Already have a newer one somewhere? Point at it directly:
      PYTHON=/path/to/python3.12 ./scripts/install.sh
PYHELP
    exit 1
fi

say "Using $PYTHON ($(version_of "$PYTHON"))"

if ! "$PYTHON" -c 'import venv' 2>/dev/null; then
    die "python's venv module is missing. On Debian/Ubuntu: sudo apt install python3-venv"
fi

GIT_VERSION="$(git --version | awk '{print $3}')"
"$PYTHON" - "$GIT_VERSION" <<'PYEOF' || die "git 2.38 or newer is required (found $GIT_VERSION)"
import sys
parts = []
for piece in sys.argv[1].split("."):
    digits = "".join(c for c in piece if c.isdigit())
    parts.append(int(digits) if digits else 0)
sys.exit(0 if tuple(parts[:2]) >= (2, 38) else 1)
PYEOF

# -- install -------------------------------------------------------------

say "Creating the virtual environment at $VENV"
# A venv sidesteps PEP 668: Homebrew's python and most distribution pythons are
# marked externally-managed, where `pip install --user` refuses to run at all.
mkdir -p "$WOWSYNC_HOME"
"$PYTHON" -m venv --upgrade-deps "$VENV" >/dev/null 2>&1 || "$PYTHON" -m venv "$VENV"

say "Installing wowsync from $HERE"
# Not editable: the venv gets its own copy, so moving or deleting this clone
# later cannot break a running agent.
"$VENV/bin/python" -m pip install --quiet --upgrade "$HERE"

say "Linking $WOWSYNC"
mkdir -p "$BIN_DIR"
ln -sf "$VENV/bin/wowsync" "$WOWSYNC"

"$WOWSYNC" --version

# -- PATH ----------------------------------------------------------------

if ! command -v wowsync >/dev/null 2>&1 || [ "$(command -v wowsync)" != "$WOWSYNC" ]; then
    case "$(basename "${SHELL:-bash}")" in
        zsh)  PROFILE="$HOME/.zshrc" ;;
        bash) [ "$(uname -s)" = "Darwin" ] && PROFILE="$HOME/.bash_profile" || PROFILE="$HOME/.bashrc" ;;
        fish) PROFILE="$HOME/.config/fish/config.fish" ;;
        *)    PROFILE="your shell profile" ;;
    esac
    cat <<PATHEOF

  $BIN_DIR is not on your PATH yet. Add it to $PROFILE:

      export PATH="$BIN_DIR:\$PATH"

  Then open a new terminal, or run it in this one to continue now.
  (The background agent does not need this -- it is given the full path.)

PATHEOF
fi

# -- service -------------------------------------------------------------

if [ "$WITH_SERVICE" -eq 0 ]; then
    cat <<NEXT
Next:
  $WOWSYNC init --remote ssh://USER@SERVER/path/to/forever.git \\
                --server http://SERVER:7373 --token YOUR_TOKEN
  $WOWSYNC doctor
  $WOWSYNC sync

Re-run this script with --service once that looks right.
NEXT
    exit 0
fi

case "$(uname -s)" in
Darwin)
    say "Installing the LaunchAgent"
    PLIST="$HOME/Library/LaunchAgents/com.wowsync.agent.plist"
    mkdir -p "$(dirname "$PLIST")" "$HOME/Library/Logs"
    sed -e "s|__WOWSYNC__|$WOWSYNC|g" -e "s|__HOME__|$HOME|g" \
        "$HERE/packaging/com.wowsync.agent.plist" > "$PLIST"
    launchctl bootout "gui/$(id -u)/com.wowsync.agent" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    say "Agent running. Logs: $HOME/Library/Logs/wowsync-agent.log"
    ;;
Linux)
    say "Installing the systemd user unit"
    UNIT="$HOME/.config/systemd/user/wowsync.service"
    mkdir -p "$(dirname "$UNIT")"
    sed "s|%h/.local/bin/wowsync|$WOWSYNC|" "$HERE/packaging/wowsync.service" > "$UNIT"
    systemctl --user daemon-reload
    systemctl --user enable --now wowsync
    # Without lingering, the agent dies when you log out of the desktop, which
    # is exactly when you would want it to finish pushing a session.
    loginctl enable-linger "$USER" 2>/dev/null || \
        echo "Could not enable lingering; run: sudo loginctl enable-linger $USER"
    say "Agent running. Logs: journalctl --user -u wowsync -f"
    ;;
*)
    die "unsupported platform for --service; run '$WOWSYNC daemon' yourself"
    ;;
esac
