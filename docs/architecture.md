# Architecture

## The constraint everything follows from

WoW keeps addon settings in memory and writes them out **all at once**, when
you log out to the character screen, `/reload`, or quit. It never re-reads a
`SavedVariables` file after a character has logged in.

Two consequences shape the whole design:

1. **You cannot deliver settings to a running client.** Anything written under
   it is ignored, and then overwritten on exit.
2. **A file-level sync tool cannot help.** Dropbox, iCloud Drive and Syncthing
   all replicate continuously and resolve conflicts by timestamp. Against a
   program that rewrites 200 files in one burst at shutdown, timestamp
   resolution means "whichever machine quit last wins", file by file, with no
   consistency between them. A half-merged UI is worse than either version.

So the unit of work is not a file, it is a **session**: everything that changed
between launching the game and quitting it.

## Ownership: the lease

Exactly one machine may own the live state at a time.

```
   idle ──── game starts ────► acquire lease ────► playing
    ▲                                                 │
    │                                            game exits
    │                                                 │
    └──── pull ◄── notify ◄── release ◄── push ◄──────┘
```

The coordinator on the home server holds one lease per profile. While a machine
holds it:

- it never pulls, so nothing is written under the running client;
- it heartbeats every 30 seconds;
- it takes a snapshot every five minutes, which only *reads* the game directory
  and is therefore safe mid-session.

If the client crashes or the machine loses power, the heartbeat stops and the
lease goes stale after its TTL (three minutes by default). The other machine
can then take it without anyone clearing anything by hand.

If you launch the game on the second machine anyway, it starts in a
*playing-without-lease* state: it warns you, still records the session locally,
and on exit goes through the ordinary conflict path instead of pushing over the
machine that did own the state.

## Why the walk between desks is free

The naive version of this syncs when you launch. That means sitting through a
download, and racing the client's startup.

Instead, the idle machine **follows continuously**. When the Mac finishes a
session it pushes and notifies the coordinator, which pushes an event down the
subscribed connection to the Linux box, which pulls straight away — while you
are still standing up. Launching later just confirms it is current.

The event stream is an optimisation, not a dependency: every agent also polls
every 30 seconds, so a dropped connection slows the handoff down rather than
breaking it.

## Storage: one git repository

Git turned out to fit this problem unusually well:

- **Deduplication.** Addon folders are tens of thousands of small text files
  that barely change between snapshots. An unchanged file across a thousand
  snapshots costs one copy.
- **Atomicity.** A session is one commit. You never restore half of one.
- **Transport.** `git fetch` and `git push` over SSH already handle
  multi-gigabyte transfers, resumption and integrity checking.
- **History, for free.** "Continuous versioning" and "sync" become the same
  mechanism rather than two systems that can disagree.
- **Conflict *detection*.** Which is the part that actually matters — see below.

The repository is bare, on the home server. Each client keeps its git directory
**outside** the game folder, with the game folder attached as a work tree, so
nothing ever appears inside the WoW install that the game or WowUp could trip
over.

### Two repositories, one game directory

| | work tree | branch | holds |
|---|---|---|---|
| `shared.git` | the game directory | `main` | addons, shared `WTF` |
| `machine.git` | a small staging dir | `machine/<id>` | `Config.wtf`, `config-cache.wtf`, per-machine SavedVariables fragments |

Both are versioned and pushed to the same server. Only the first is shared
between machines. That is how "my graphics settings are backed up" and "my
graphics settings are not on the other machine" are both true.

### Staying inside the lines

The work tree is a live game install: the client binary, `Cache/`, `Logs/`, and
files wowsync must never touch sit right next to files it owns. Three
mechanisms keep it confined:

- every mutating git call is scoped with an explicit `:(glob)` pathspec
  allowlist;
- `git clean` is never used, and nothing but a tracked file is ever removed;
- before any operation that rewrites the work tree, `assert_index_scoped()`
  verifies that every tracked path still lives under `Interface/AddOns` or
  `WTF/Account`, and refuses if not.

## Conflicts

A conflict means both machines made snapshots since they last agreed. With the
lease in place this is rare, but it happens if you play offline, steal a lease,
or let both addon updaters run.

Resolution is decided by **what the file is**, never by which is newer:

| Where | Both changed different files | Both changed the same file |
|---|---|---|
| `Interface/AddOns/**` | merged | server's copy wins, automatically |
| `WTF/**` | merged | **stops and asks you** |

The asymmetry is the point. An addon payload is a download — either copy is
fine, and the discarded one stays in history. A settings file is the thing you
spent months building, and no automatic rule is worth the risk.

When it stops, nothing is lost: your side is committed and parked on a
`conflict/<machine>/<timestamp>` branch, the disputed files are listed by
`wowsync resolve --list`, and either version of any file can be printed with
`wowsync show`. Choosing a side records a merge commit with **both** parents, so
the losing side stays reachable and the server still fast-forwards.

The merge itself is done with `git merge-tree --write-tree`, entirely in the
object database. Merging in the work tree would mean writing conflict markers
into Lua files the game is expected to parse; doing it as plumbing means we can
inspect the outcome and decide before anything reaches disk.

## Machine-local settings

Two layers, because there are two kinds of machine-specific setting.

**Whole files.** `WTF/Config.wtf` holds resolution, display mode, graphics API
and sound device; `config-cache.wtf` holds the account and per-character copies
of the same CVar store. These are excluded from the shared repository and
tracked in the machine repository instead.

**Parts of files.** Occasionally an addon keeps a machine-specific setting
inside a SavedVariables file you otherwise want shared. Naming the saved global
in the config splits it out:

- on capture, the global's statements are carved out and the *stripped* version
  is what gets recorded — using `git hash-object` so the file on disk keeps
  every global and WoW loads normally;
- the carved-out part is stored in the machine repository;
- after any sync, it is appended back.

This works because a SavedVariables file is machine-generated in a narrow
shape: a flat sequence of top-level assignments. Splitting it needs only brace
depth plus string and comment state, not a Lua interpreter — see
`wowsync/savedvars.py`.

## The pieces

| Module | Responsibility |
|---|---|
| `syncer.py` | capture, apply, merge, restore, resolve |
| `gitrepo.py` | scope-safe git wrapper |
| `savedvars.py` | splitting SavedVariables at global boundaries |
| `daemon.py` | the idle / playing / playing-without-lease state machine |
| `server.py` | leases, heartbeats, the event stream |
| `lease.py` | the client side of the above |
| `procwatch.py` | noticing the game across macOS and Proton |
| `doctor.py` | preflight checks |

## Things deliberately not done

- **No continuous file watching.** Nothing useful changes mid-session, and
  watching invites writing at the wrong moment.
- **No line-level merging of Lua.** `.gitattributes` marks every file
  `-merge`, so git cannot splice two versions of a settings file together.
- **No syncing of WowUp's database.** Addon folders are the truth; WowUp
  rebuilds its view from them by fingerprint.
- **No HTTP in the data path.** Git over SSH already does large transfers well.
  The coordinator only moves small JSON messages.
