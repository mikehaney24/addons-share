# Setting up the Linux machine (Proton)

The agent runs **natively on Linux**, not inside the Wine prefix. It only reads
and writes files, and the prefix is an ordinary directory tree, so there is
nothing to install on the Windows side.

## 1. Install

```sh
git --version        # need 2.38 or newer
python3 --version    # need 3.11 or newer

git clone https://github.com/mikehaney24/addons-share
cd addons-share
./scripts/install.sh
```

## 2. Find the game directory inside the prefix

Through Steam, the game lives under the prefix for its app ID:

```
~/.steam/steam/steamapps/compatdata/<APPID>/pfx/drive_c/
└── Program Files (x86)/World of Warcraft Forever/
    └── _forever_/          ← the flavor directory
        ├── Interface/
        └── WTF/
```

`wowsync init` scans Steam libraries (including extra ones listed in
`libraryfolders.vdf`), Lutris, Bottles and `~/.wine`, so usually:

```sh
wowsync init \
    --remote  ssh://you@homeserver.local/Users/you/wowsync/repos/forever.git \
    --server  http://homeserver.local:7373 \
    --token   THE_TOKEN
```

If it finds nothing, locate it yourself and pass `--game-dir`:

```sh
find ~/.steam ~/Games ~/.local/share -maxdepth 8 -type d -name WTF 2>/dev/null
```

The parent of that `WTF` directory is what you want. Quote it — the path
contains spaces.

## 3. Check before trusting it

```sh
wowsync doctor
```

Then launch the game and run it again, so it can confirm it recognises the
process. Under Proton the client appears as `Wow.exe`, which the default
pattern already matches. If your build differs, `doctor` tells you, and you can
set `process_match` in `~/.config/wowsync/wowsync.toml` to any regular
expression — check against:

```sh
ps -Ao pid=,args= | grep -i wow
```

## 4. First sync

With the game closed:

- **If the Mac already seeded the server:** `wowsync pull`. Your existing local
  addons are committed and parked on a `preexisting/<machine>/<timestamp>`
  branch first, then the server's state is applied.
- **If this machine has the setup you want to keep:** `wowsync sync`.

## 5. Run the agent

```sh
./scripts/install.sh --service
systemctl --user status wowsync
journalctl --user -u wowsync -f
```

The installer enables lingering (`loginctl enable-linger`), which matters here:
without it the agent is killed when you log out of your desktop session —
exactly when it would be finishing a push.

## Linux and Proton specifics

**Case sensitivity.** ext4 and btrfs are case-sensitive; APFS on the Mac is not.
Wine itself is case-insensitive, so an addon installed under `Interface/Addons`
works locally but collides with `Interface/AddOns` once it reaches the Mac.
`wowsync doctor` checks for this. If you hit it, fix it here — Linux is the
side that can represent both, so it is the side that can tell them apart:

```sh
cd "<game-dir>/Interface"
ls -d AddOns Addons 2>/dev/null       # if both exist, merge them by hand
```

**Running your addon manager.** You do not need one on this machine at all —
addon updates travel with the sync. If you do run WowUp here (native build or
inside the prefix), it fingerprints the addon folders and will identify
everything the Mac installed without being told.

**Steam launch options.** If you would rather not run the agent, you can make
the lease explicit from Steam. Set the game's launch options to:

```
/home/you/.local/bin/wowsync play -- %command%
```

wowsync takes the lease, syncs, hands off to Steam's own launch command, waits
for the client to exit, then syncs and releases. Note that Steam treats the
wrapper as the game for playtime purposes.

**Anti-cheat.** wowsync never touches the running process, injects nothing, and
does not modify the client. It only reads and writes files in `WTF/` and
`Interface/AddOns/` while the game is not running — the same thing your addon
manager does.

**Filesystem case config.** If the prefix lives on a filesystem where you have
set `chattr +F` (casefold) on a directory, tell wowsync so git agrees:

```sh
git --git-dir ~/.local/state/wowsync/forever/shared.git config core.ignoreCase true
```

`wowsync init` detects this automatically in the normal case.
