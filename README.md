# wowsync

Stop playing World of Warcraft on one computer, walk to the other, and start
playing with the addon setup you just left behind. Every change is versioned,
and anything that belongs to one machine — resolution, graphics API, sound
device — stays on that machine.

Built for a two-machine setup (a Mac and a Linux box running the game through
Proton) with a macOS home server holding the history. It works with retail's
directory layout, which **World of Warcraft Forever** shares.

```
  Mac ──┐                        ┌── Linux / Proton
        │                        │
        └──►  home server  ◄─────┘
              ├── forever.git      every snapshot, forever
              └── coordinator      who is playing right now
```

---

## The idea

WoW does not stream its settings to disk. It holds them in memory and rewrites
every `SavedVariables` file **in one go when you log out or quit**. That single
fact rules out the obvious approach:

> Point Dropbox, iCloud Drive, or Syncthing at the `WTF` folder.

If a file-sync tool delivers new settings while the client is running, WoW
overwrites them on exit — it has no idea anything changed underneath it. If
both machines are running, they race, and the loser's UI is silently gone. This
is the single most common way people lose a WeakAuras collection.

So wowsync does not continuously mirror a folder. It models the thing you
actually do, which is a **handoff**:

1. **Exactly one machine owns the live state at a time.** It takes a *lease*
   from the coordinator when the game launches and releases it when the game
   exits. The other machine can see it, but will not write it.
2. **The idle machine follows.** The moment you quit on the Mac, the Linux box
   is told and pulls immediately — while you are still standing up. By the time
   you sit down it is already current, so launching there is a confirmation,
   not a download.
3. **Nothing is ever overwritten silently.** Every sync starts by committing
   what is on disk. If the two machines genuinely disagree, you are told and
   both versions are kept.

## What you get

- **Handoff in both directions** — addons, SavedVariables, keybindings, macros,
  per-character addon enable/disable state, and Edit Mode layouts.
- **Machine-specific settings preserved** — `Config.wtf` and `config-cache.wtf`
  never leave the machine that wrote them, so each box keeps its own
  resolution, display mode, graphics API and audio device. They are still
  versioned, just on a private branch.
- **Continuous time-based versioning** — a snapshot on every launch, every exit,
  every five minutes while you play, and whenever your addon updater changes
  something. `wowsync log` and `wowsync restore` go back to any of them.
- **Protection against playing on both at once** — and a clear, recoverable
  conflict if you manage it anyway.

## Quick start

On the **home server**:

```sh
git clone https://github.com/mikehaney24/addons-share
cd addons-share
./scripts/setup-server.sh                 # creates the bare repo, prints next steps
wowsync serve --port 7373 --token "$(openssl rand -hex 16)"
```

On **each gaming machine**:

```sh
git clone https://github.com/mikehaney24/addons-share
cd addons-share
./scripts/install.sh

wowsync init \
    --remote  ssh://you@homeserver.local/Users/you/wowsync/repos/forever.git \
    --server  http://homeserver.local:7373 \
    --token   THE_TOKEN_FROM_ABOVE

wowsync doctor          # check this machine before trusting it with anything
wowsync sync            # first machine seeds the server; second adopts it
./scripts/install.sh --service   # run the agent in the background from now on
```

Then play. There is nothing else to do.

Full walkthroughs: [the server](docs/setup-server.md),
[macOS](docs/setup-macos.md), [Linux and Proton](docs/setup-linux-proton.md).

## Everyday use

| | |
|---|---|
| `wowsync status` | What is synced, who is playing, anything pending |
| `wowsync sync` | Pull then push, by hand |
| `wowsync log` | Every snapshot, newest first |
| `wowsync diff HEAD~1 HEAD` | What changed between two of them |
| `wowsync show <id> <path>` | Print a settings file as it was then |
| `wowsync restore <id>` | Roll the shared state back |
| `wowsync resolve --list` | Show a conflict and settle it |
| `wowsync doctor` | Re-check the setup |
| `wowsync play -- <cmd>` | Launch with a lease, without the daemon |

Rolling back never rewinds history: `restore` records the old tree as a *new*
snapshot, so the state you rolled back from is still there if you change your
mind.

## Working with WowUp / CurseForge

Keep using WowUp exactly as you do now, on either machine. wowsync syncs the
addon folders themselves rather than WowUp's database, which is deliberate:

- `Interface/AddOns/` is what the game actually loads, so it is the truth.
- WowUp fingerprints addon folders to identify them, so after a sync the other
  machine's WowUp recognises every addon and version without being told.
- WowUp's own database is per-machine and would carry install paths across.

Practical notes:

- Update addons on **whichever machine you like** — the update travels with the
  next sync.
- If both machines' updaters happen to pull a different version of the same
  addon while neither is playing, wowsync merges them and keeps the server's
  copy of any file they disagree on. Addon payloads are reinstallable; your
  settings are not, which is why nothing under `WTF/` is ever auto-resolved.
- You do not need WowUp installed on both machines. One is enough.

## What is and is not shared

**Shared** — `Interface/AddOns/**`, account and per-character
`SavedVariables/`, `bindings-cache.wtf`, `macros-cache.txt`, `AddOns.txt`,
`layout-local.txt`.

**Per machine** — `WTF/Config.wtf`, account and per-character
`config-cache.wtf`. Versioned privately, never shared.

**Ignored** — `Cache/`, `Logs/`, `Errors/`, `Screenshots/`, `*.bak`,
`.DS_Store`.

All three lists are editable in `wowsync.toml`. If an addon keeps a
machine-specific setting inside a shared SavedVariables file, you can name that
saved global and keep just that part local — see
[what gets synced](docs/what-gets-synced.md).

## Requirements

- Python 3.11 or newer, and git 2.38 or newer, on all three machines.
  macOS ships 3.9, so `brew install python@3.12` there. Only the config
  reader needs 3.11 (`tomllib`), but 3.9 has been end-of-life since October
  2025 and is not a runtime to hand a background agent. The installer finds a
  suitable interpreter even when it is not first on `PATH`; override with
  `PYTHON=/path/to/python3.12 ./scripts/install.sh`.
- SSH from each gaming machine to the home server.
- Both gaming machines on the same network as the server.

No database, no cloud account, nothing listening on the public internet.

### How it installs

Nothing is compiled — wowsync is Python, and `wowsync` is a small launcher
script generated at install time. `scripts/install.sh` puts it in a private
virtual environment and links the command into place:

```
~/.local/share/wowsync/venv/     the interpreter and the code
~/.local/bin/wowsync             a symlink to the launcher  ← put this on your PATH
```

The venv is not decoration. Homebrew's python and most distribution pythons
are marked externally-managed, where installing into them is refused outright;
and an isolated environment means a `brew upgrade` or a distribution update
cannot change the interpreter under a running agent. You never have to activate
it — the launcher points at the right python itself.

You need `PATH` only for typing `wowsync` yourself. The background agent is
registered with the full path, so it works either way. The installer prints the
exact line to add if the directory is not already there.

Re-running `./scripts/install.sh` upgrades in place. The code is copied into
the venv rather than linked, so you can move or delete the clone afterwards.
To remove it entirely: `rm -rf ~/.local/share/wowsync ~/.local/bin/wowsync`
(after `launchctl bootout` / `systemctl --user disable --now wowsync`).

## How it is put together

| | |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Why a handoff, what the lease guarantees, how conflicts resolve |
| [docs/what-gets-synced.md](docs/what-gets-synced.md) | The file-by-file rules and how to change them |
| [docs/troubleshooting.md](docs/troubleshooting.md) | When something looks wrong |

## Tests

```sh
python3 -m pip install -e '.[dev]'
python3 -m pytest
```

The suite drives two simulated machines, a real git server and a real
coordinator through handoffs, conflicts, crashes and rollbacks.

## Licence

MIT — see [LICENSE](LICENSE).
