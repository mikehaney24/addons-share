# What gets synced

Three lists, all editable in `wowsync.toml`. Check what is actually tracked at
any time:

```sh
git --git-dir ~/.local/state/wowsync/forever/shared.git ls-files | head -50
```

(On macOS: `~/Library/Application Support/wowsync/forever/shared.git`.)

## Shared between machines

| Path | What it is |
|---|---|
| `Interface/AddOns/**` | The addons themselves |
| `WTF/Account/*/SavedVariables/**` | Account-wide addon settings |
| `WTF/Account/*/<Realm>/<Char>/SavedVariables/**` | Per-character addon settings |
| `WTF/Account/*/bindings-cache.wtf` | Account keybindings |
| `WTF/Account/*/<Realm>/<Char>/bindings-cache.wtf` | Character keybindings |
| `WTF/Account/*/macros-cache.txt` | Account macros |
| `WTF/Account/*/<Realm>/<Char>/macros-cache.txt` | Character macros |
| `WTF/Account/*/<Realm>/<Char>/AddOns.txt` | Which addons are enabled per character |
| `**/layout-local.txt` | Edit Mode layouts |

## Kept per machine

| Path | Why |
|---|---|
| `WTF/Config.wtf` | Resolution, display mode, graphics API, sound device, every graphics CVar |
| `WTF/Account/*/config-cache.wtf` | The account-level CVar store, which includes machine-dependent values |
| `WTF/Account/*/<Realm>/<Char>/config-cache.wtf` | The per-character copy of the same |

These are **still versioned** — they go to the `machine/<id>` branch, so you can
recover your graphics settings after a reinstall — they are just never shared.

To see their history:

```sh
wowsync --profile forever status
git --git-dir ~/.local/state/wowsync/forever/machine.git log --oneline
```

## Never stored

`Cache/`, `Logs/`, `Errors/`, `Screenshots/`, `*.bak`, `*.lua.bak`,
`.DS_Store`, `._*`, `Thumbs.db`, `desktop.ini`.

`*.bak` matters more than it looks: WoW keeps a backup copy of every
SavedVariables file, which would roughly double the repository for no benefit —
history already does that job better.

## Changing the lists

```toml
[profile.forever.sync]
include = ["Fonts/**"]              # added to the defaults
exclude = ["Interface/AddOns/BigAddonIDontWantSynced/**"]
```

To replace a default list outright rather than extend it, set
`replace_include = true` or `replace_exclude = true` in the same table.

```toml
[profile.forever.machine_local]
files = [
  "WTF/Config.wtf",
  "WTF/Account/*/config-cache.wtf",
  "WTF/Account/*/*/*/config-cache.wtf",
  "WTF/Account/*/*/*/layout-local.txt",   # if your two monitors differ enough
]
```

Note the shape of those globs: `WTF/Account/*/*/*/` is
`Account/<ACCOUNT>/<REALM>/<CHARACTER>/`. A `*` matches one path segment; `**`
matches any number.

After editing, `wowsync doctor` re-checks and the next sync applies it. A file
that was shared and is now machine-local is untracked automatically, and the
other machine stops receiving it.

## Keeping part of a SavedVariables file local

Some addons store a genuinely machine-specific setting inside a file you
otherwise want shared — a UI scale pinned to one monitor, a sound output
device. Name the saved global and only that part stays local:

```toml
[[profile.forever.machine_local.saved_variables]]
file = "WTF/Account/*/SavedVariables/ElvUI.lua"
globals = ["ElvPrivateDB"]
```

The shared repository gets the file with those globals removed; your machine
keeps them and they are appended back after every sync. The file on disk always
has every global, so the addon sees exactly what it wrote.

To find the right name, look at the top-level assignments in the file — they
are the saved globals, and they also appear in the addon's `.toc` as
`## SavedVariables:`:

```sh
grep -n '^[A-Za-z_][A-Za-z0-9_]* *=' \
    "<game-dir>/WTF/Account/<ACCOUNT>/SavedVariables/ElvUI.lua"
grep -i savedvariables "<game-dir>/Interface/AddOns/ElvUI/ElvUI_Mainline.toc"
```

`wowsync doctor` warns if a named global is not present in the file, which
catches typos before they quietly do nothing.

Use this sparingly. Whole-file exclusion is simpler and covers the graphics and
sound settings, which is what most people mean by machine-specific.

The carved-out half is kept under `<state>/wowsync/<profile>/local/sv-local/`.
If you drop a rule, or the addon stops using that global, delete the matching
file there — otherwise the old value keeps being appended back after each sync.

## Adding a second game

Each flavor is a separate profile with its own repository and its own lease:

```toml
[profile.retail]
game_dir = "/Applications/World of Warcraft/_retail_"
process_match = "World of Warcraft"
git_remote = "ssh://you@homeserver.local/Users/you/wowsync/repos/retail.git"
enabled = true
```

Run `./scripts/setup-server.sh ~/wowsync/repos retail.git` on the server first.
One agent handles every enabled profile; use `--profile` to aim a command at
one of them.
