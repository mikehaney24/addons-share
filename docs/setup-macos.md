# Setting up the Mac

## 1. Install

```sh
brew install git python@3.12
git clone https://github.com/mikehaney24/addons-share
cd addons-share
./scripts/install.sh
```

Nothing is compiled. The installer puts wowsync in a private virtual
environment under `~/.local/share/wowsync/venv` and links the command to
`~/.local/bin/wowsync`. If it warns that `~/.local/bin` is not on your `PATH`,
add the line it prints to `~/.zshrc` and open a new terminal.

The private environment matters here: Homebrew's python is marked
externally-managed, so installing into it directly is refused, and a `brew
upgrade` could otherwise change the interpreter out from under a running
agent.

## 2. Find the game directory

wowsync syncs a **flavor directory** — the folder containing `WTF/` and
`Interface/`, not the install root:

```
/Applications/World of Warcraft Forever/
└── _forever_/              ← this one
    ├── Interface/
    ├── WTF/
    └── World of Warcraft Forever.app
```

The flavor folder's actual name is whatever the client ships with; retail uses
`_retail_`. `wowsync init` finds it by looking for the `WTF` + `Interface`
pair, so you do not need to know it in advance.

## 3. Configure

```sh
wowsync init \
    --remote  ssh://you@homeserver.local/Users/you/wowsync/repos/forever.git \
    --server  http://homeserver.local:7373 \
    --token   THE_TOKEN
```

If more than one install is found, it lists them; pick one with `--game-dir`.

## 4. Check before trusting it

```sh
wowsync doctor
```

Fix anything marked `FAIL`. Warnings are usually fine — "game is not running"
just means the process pattern is unverified. Launch the game once and re-run
it to confirm the agent will see your sessions.

## 5. First sync

Do this with the game closed.

- **If the Mac has the setup you want to keep:** `wowsync sync`. This seeds the
  server.
- **If the other machine already seeded it:** `wowsync pull`. Your local
  addons and settings are committed first and parked on a
  `preexisting/<machine>/<timestamp>` branch, then the server's state is
  applied. Nothing is destroyed; `wowsync log --machine <name>` still reaches it.

## 6. Run the agent

```sh
./scripts/install.sh --service
```

Confirm:

```sh
launchctl print gui/$(id -u)/com.wowsync.agent | head
tail -f ~/Library/Logs/wowsync-agent.log
```

Now just play. Quit the game, walk to the other machine, play there.

## macOS specifics

**Case-insensitive filesystem.** APFS is case-insensitive by default; ext4 on
the Linux box is not. Two addon folders differing only in case can coexist on
Linux but not here. `wowsync doctor` checks for this — if it reports a
collision, remove one copy in your addon manager before syncing.

**Unicode in folder names.** APFS returns decomposed (NFD) filenames while
Linux stores what it was given. wowsync sets `core.precomposeunicode` so addons
with accented names match on both. `doctor` lists any it finds so you can spot
a mismatch early.

**Notifications.** The agent uses the standard notification centre for the
things that need your attention — a conflict, or launching while the other
machine still holds the lease. If you see nothing, check System Settings →
Notifications for `osascript`.

**Full Disk Access.** Not needed for a normal install under `/Applications` or
your home directory. If the game lives on an external volume and the agent
reports permission errors, grant Full Disk Access to your terminal (or to the
Python binary the agent runs as).

## Without the background agent

If you would rather stay explicit, skip `--service` and launch through wowsync:

```sh
wowsync play -- open -a "World of Warcraft Forever"
```

It takes the lease, syncs, launches, waits for the game to exit, then syncs
again and releases the lease. Leave the terminal open while you play.
