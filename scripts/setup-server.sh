#!/usr/bin/env bash
# Prepare the home server: a bare git repository for the history, and the
# coordinator service that decides which machine is playing.
#
#   ./scripts/setup-server.sh [repo-parent-dir]
#
# Run this ON the home server.
set -euo pipefail

REPO_ROOT="${1:-$HOME/wowsync/repos}"
REPO_NAME="${2:-forever.git}"
REPO_PATH="$REPO_ROOT/$REPO_NAME"

say() { printf '\n==> %s\n' "$*"; }

command -v git >/dev/null || { echo "git is required" >&2; exit 1; }

say "Creating the bare repository at $REPO_PATH"
mkdir -p "$REPO_ROOT"
if [ -d "$REPO_PATH" ]; then
    echo "Already exists; leaving it alone."
else
    git init --quiet --bare "$REPO_PATH"
fi

say "Tuning it for this workload"
# Addon folders are tens of thousands of small text files that barely change
# between snapshots, so aggressive delta compression pays for itself. Repacking
# on a schedule rather than mid-push keeps a handoff from stalling.
git -C "$REPO_PATH" config core.bigFileThreshold 8m
git -C "$REPO_PATH" config pack.window 50
git -C "$REPO_PATH" config pack.depth 50
git -C "$REPO_PATH" config receive.autogc false
git -C "$REPO_PATH" config gc.auto 0
git -C "$REPO_PATH" config transfer.unpackLimit 1
# Conflict and pre-existing branches accumulate; they are cheap, but keep their
# reflogs from expiring so an old state stays recoverable.
git -C "$REPO_PATH" config gc.reflogExpire never
git -C "$REPO_PATH" config gc.reflogExpireUnreachable never

say "Setting up scheduled maintenance"
MAINT="$REPO_ROOT/wowsync-gc.sh"
GIT_BIN="$(command -v git)"
cat > "$MAINT" <<GCEOF
#!/usr/bin/env bash
# Repack the wowsync repository. Safe to run at any time; it never touches
# working state, only how the history is stored.
#
# Deliberately NOT 'gc --auto': this repository sets gc.auto=0 so a repack can
# never fire in the middle of someone's push and stall a handoff. '--auto'
# honours that setting, so it would exit 0 having done nothing at all.
#
# --keep-largest-pack folds loose objects and small packs into one without
# rewriting the large base pack, which keeps the weekly run quick as history
# grows. Older git without that flag falls back to a full repack.
set -euo pipefail

# cron runs with a minimal PATH (/usr/bin:/bin on macOS), which does not
# include Homebrew. The git found at setup time is baked in so this works
# under cron; if it ever moves, fall back to whatever is on PATH.
GIT="$GIT_BIN"
[ -x "\$GIT" ] || GIT="\$(command -v git || true)"
[ -n "\$GIT" ] || { echo "wowsync-gc: git not found" >&2; exit 1; }

before=\$("\$GIT" -C "$REPO_PATH" count-objects -v | awk '/^count:/ {print \$2}')
"\$GIT" -C "$REPO_PATH" gc --quiet --keep-largest-pack || "\$GIT" -C "$REPO_PATH" gc --quiet
after=\$("\$GIT" -C "$REPO_PATH" count-objects -v | awk '/^count:/ {print \$2}')
echo "wowsync-gc: loose objects \$before -> \$after"
GCEOF
chmod +x "$MAINT"

# `wowsync serve` below is only typeable if the command is on PATH, and on this
# machine it may not be installed yet -- setup-server.sh deliberately runs
# before install.sh. Print whatever is actually true right now.
if WOWSYNC_BIN="$(command -v wowsync 2>/dev/null)"; then
    SERVE="$WOWSYNC_BIN"
    INSTALL_NOTE=""
elif [ -x "$HOME/.local/bin/wowsync" ]; then
    SERVE="$HOME/.local/bin/wowsync"
    INSTALL_NOTE="     (installed, but $HOME/.local/bin is not on your PATH yet)
"
else
    SERVE="$HOME/.local/bin/wowsync"
    INSTALL_NOTE="     Run ./scripts/install.sh on this machine first.
"
fi

cat <<NEXT

Repository ready: $REPO_PATH

Add this to the clients' config as git_remote:

    ssh://$(whoami)@$(hostname -s).local$REPO_PATH

Remaining steps on this machine:

  1. Allow SSH in:  System Settings > General > Sharing > Remote Login
     (on Linux: make sure sshd is running)

  2. From EACH gaming machine, install its key so pushes need no password:
         ssh-keygen -t ed25519 -C wowsync        # if you have no key yet
         ssh-copy-id $(whoami)@$(hostname -s).local

  3. Start the coordinator:
$INSTALL_NOTE         $SERVE serve --port 7373 --token "\$(openssl rand -hex 16)"
     Keep that token; both gaming machines need it in their config.
     To run it at boot, see packaging/com.wowsync.coordinator.plist.

  4. Repack weekly (optional but recommended):
         echo "0 4 * * 0 $MAINT" | crontab -

NEXT
