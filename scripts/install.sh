#!/usr/bin/env bash
# Install wowsync for the current user and, optionally, register the background
# agent so handoffs happen without you thinking about them.
#
#   ./scripts/install.sh              # install the command only
#   ./scripts/install.sh --service    # also install and start the agent
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WITH_SERVICE=0
[ "${1:-}" = "--service" ] && WITH_SERVICE=1

say() { printf '\n==> %s\n' "$*"; }

PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null || { echo "python3 is required" >&2; exit 1; }
command -v git >/dev/null || { echo "git is required" >&2; exit 1; }

VERSION="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
"$PYTHON" - <<'PYEOF' || { echo "python 3.11 or newer is required (found $VERSION)" >&2; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
PYEOF

say "Installing wowsync from $HERE"
"$PYTHON" -m pip install --user --upgrade "$HERE"

BIN="$("$PYTHON" -c 'import site,os; print(os.path.join(site.USER_BASE, "bin"))')"
export PATH="$BIN:$PATH"
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "Note: add $BIN to your PATH." ;;
esac

WOWSYNC="$BIN/wowsync"
[ -x "$WOWSYNC" ] || WOWSYNC="$(command -v wowsync)"
say "Installed: $WOWSYNC"
"$WOWSYNC" --version

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
    echo "Unsupported platform for --service; run '$WOWSYNC daemon' yourself." >&2
    exit 1
    ;;
esac
