# Troubleshooting

Start here:

```sh
wowsync status      # who is playing, what is pending
wowsync doctor      # what is wrong with this machine
```

Logs:

| | |
|---|---|
| macOS agent | `~/Library/Logs/wowsync-agent.log` |
| Linux agent | `journalctl --user -u wowsync -f` |
| Coordinator | `~/Library/Logs/wowsync-coordinator.log` |
| Per-profile | `<state>/wowsync/<profile>/logs/wowsync.log` |

---

## "Both machines changed the same settings"

Nothing is lost. Your side is committed and parked on a branch; the server's is
untouched.

```sh
wowsync resolve --list          # which files, and how to compare them
wowsync show <local-id>  WTF/Account/.../SavedVariables/WeakAuras.lua
wowsync show <server-id> WTF/Account/.../SavedVariables/WeakAuras.lua

wowsync resolve --take-local    # keep this machine's version
wowsync resolve --take-server   # keep the other machine's
```

Either choice records a merge commit with both sides as parents, so the version
you did not pick stays reachable in history.

**Why it happened** is usually one of: you played on both machines, the
coordinator was down when you launched, or you used `--steal`.

## The other machine thinks I am still playing

```sh
wowsync status      # "coordinator: mac-studio playing for 4h"
```

A lease that stops heartbeating goes stale after three minutes and can then be
taken normally. If it says `(stale)`, just launch — the daemon will take it.

If it is not stale but the machine is off or crashed:

```sh
wowsync play --steal -- <launch command>
```

Or clear it from the server:

```sh
curl -X POST http://homeserver.local:7373/v1/lease/release \
     -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"profile":"forever","force":true}'
```

## Settings did not follow me

Check in this order:

1. **Did the sending machine finish?** `wowsync log -n 3` there should show a
   `session end` snapshot after you quit.
2. **Did it reach the server?** `wowsync status` should say "in sync with the
   server", not "ahead".
3. **Did this machine pull?** `wowsync status`, then `wowsync sync` by hand.
4. **Is the file even in the shared set?** If it is `config-cache.wtf` or
   `Config.wtf`, it is machine-local on purpose — see
   [what gets synced](what-gets-synced.md).

## I synced while the game was running

`wowsync sync` refuses to do this. If the agent applied a sync moments after
you launched, it is fine: WoW reads SavedVariables when a **character logs in**,
not when the process starts, so anything that lands while the login screen is up
is picked up. The agent warns you if it lands later than that.

If you are already logged in and suspect stale settings: log out to the
character screen and back in. Do **not** quit the client — logging out to
character select writes your current in-memory state, which is what you want
preserved anyway.

## The agent does not notice the game

```sh
ps -Ao pid=,args= | grep -i wow
```

Set `process_match` in `wowsync.toml` to a regular expression matching that
line, then restart the agent:

```sh
launchctl kickstart -k gui/$(id -u)/com.wowsync.agent   # macOS
systemctl --user restart wowsync                        # Linux
```

`wowsync doctor` confirms the match while the game is running.

## Case collision between the two machines

`doctor` reports addon folders differing only by case. macOS cannot hold both
at once, so a checkout there breaks rather than failing cleanly.

Fix it on the Linux side, which can tell them apart:

```sh
cd "<game-dir>/Interface"
ls -d AddOns Addons        # if both exist, move the contents into AddOns
```

Then `wowsync sync` from Linux.

## "no Python 3.11 or newer found" on macOS

macOS ships Python 3.9 as `/usr/bin/python3`, and that is what a stock machine
offers. Install a current one:

```sh
brew install python@3.12
./scripts/install.sh
```

The installer looks for `python3.13`, `python3.12`, `python3.11` and the
Homebrew locations before giving up, so an interpreter you already have but
which is not first on `PATH` is found automatically. To choose one yourself:

```sh
PYTHON=/opt/homebrew/bin/python3.12 ./scripts/install.sh
```

Only one thing needs 3.11: wowsync reads its config with `tomllib`, which
entered the standard library in that release. Everything else runs on older
versions — but 3.9 reached end of life in October 2025, so it gets no security
fixes, and Apple has been signalling the removal of its bundled python for
years. Neither is a good foundation for something that runs in the background
and manages settings you cannot easily rebuild.

The interpreter wowsync installs against is the one its agent keeps using; it
lives in a private virtual environment, so upgrading or removing other pythons
later will not disturb it.

## Push is rejected or hangs

```sh
ssh you@homeserver.local true       # must return instantly with no prompt
```

The agent uses `BatchMode=yes` and will not answer a password prompt. Fix with
`ssh-copy-id`. If SSH is fine but pushes hang, the first one is simply large —
the initial addon folder can be a gigabyte. Watch it:

```sh
wowsync -v push
```

## The repository is getting big

```sh
git -C ~/wowsync/repos/forever.git count-objects -vH
~/wowsync/repos/wowsync-gc.sh
```

If it is still large, something binary is probably being tracked. Find it:

```sh
git --git-dir ~/.local/state/wowsync/forever/shared.git \
    ls-files | xargs -I{} du -h "<game-dir>/{}" 2>/dev/null | sort -rh | head -20
```

Add the offender to `exclude` and sync.

## Starting over on one machine

Safe, because the server holds everything:

```sh
rm -rf ~/.local/state/wowsync/forever     # or the macOS path
wowsync init --force --game-dir ... --remote ... --server ... --token ...
wowsync pull --adopt
```

`--adopt` tells it to replace this machine's unrelated local state with the
server's. The local state is still committed and parked on a
`preexisting/<machine>/<timestamp>` branch first.

## Recovering something you deleted months ago

```sh
wowsync log --since "6 months ago" -n 200
wowsync show <id> WTF/Account/<ACCOUNT>/SavedVariables/WeakAuras.lua > /tmp/old.lua
```

To put a whole snapshot back:

```sh
wowsync restore <id>
```

That records the old tree as a new snapshot rather than rewinding, so the
current state stays recoverable the same way.
