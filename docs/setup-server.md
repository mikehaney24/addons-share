# Setting up the home server (macOS)

The server does two jobs: it stores the history, and it tracks which machine is
playing. Neither is heavy — an idle Mac mini will not notice it.

## 1. Requirements

```sh
brew install git python@3.11     # macOS ships neither at a version we can rely on
git --version                    # need 2.38 or newer
python3 --version                # need 3.11 or newer
```

## 2. Turn on SSH

**System Settings → General → Sharing → Remote Login: on.**

Set "Allow access for" to your user. Note the hostname it shows, usually
`something.local` — that is what the gaming machines will use.

## 3. Create the repository

```sh
git clone https://github.com/mikehaney24/addons-share
cd addons-share
./scripts/setup-server.sh
```

This creates `~/wowsync/repos/forever.git`, tunes it for tens of thousands of
small files, and prints the remote URL to give the gaming machines.

If you want to sync retail as well, make a second one:

```sh
./scripts/setup-server.sh ~/wowsync/repos retail.git
```

One repository per game flavor. They are independent and lease independently.

## 4. Let the gaming machines in without a password

**On each gaming machine**, not here:

```sh
ssh-keygen -t ed25519 -C wowsync        # only if you have no key yet
ssh-copy-id you@homeserver.local
ssh you@homeserver.local true           # must not prompt
```

That last check matters: the agent runs with `BatchMode=yes` and will not
answer a password prompt.

## 5. Run the coordinator

```sh
./scripts/install.sh                    # gets you the wowsync command
TOKEN="$(openssl rand -hex 16)"
echo "$TOKEN"                           # both gaming machines need this
wowsync serve --port 7373 --token "$TOKEN"
```

Check it from a gaming machine:

```sh
curl http://homeserver.local:7373/
```

You should get a small status page saying nobody is playing.

### Start it at boot

```sh
sed -e "s|__WOWSYNC__|$(command -v wowsync)|g" \
    -e "s|__HOME__|$HOME|g" \
    -e "s|__TOKEN__|$TOKEN|g" \
    packaging/com.wowsync.coordinator.plist \
    > ~/Library/LaunchAgents/com.wowsync.coordinator.plist

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.wowsync.coordinator.plist
launchctl print gui/$(id -u)/com.wowsync.coordinator | head
```

A LaunchAgent runs when a user is logged in. On a headless home server, either
enable automatic login (System Settings → Users & Groups → Automatic login), or
move the plist to `/Library/LaunchDaemons/` and bootstrap it into the `system`
domain so it runs regardless.

Logs: `~/Library/Logs/wowsync-coordinator.log`.

## 6. Keep the repository compact

`setup-server.sh` writes a maintenance script. Run it weekly:

```sh
echo "0 4 * * 0 $HOME/wowsync/repos/wowsync-gc.sh" | crontab -
```

Repacking never touches anything the clients depend on — only how the history
is stored on disk.

## What this costs

The addon folder is typically 0.5–2 GB. Because git stores each unique file
once, a year of daily snapshots lands well under twice the size of a single
copy; SavedVariables files are text and delta-compress heavily.

Check it any time:

```sh
du -sh ~/wowsync/repos/forever.git
git -C ~/wowsync/repos/forever.git count-objects -vH
```

## Security

The coordinator speaks plain HTTP on your LAN and authenticates with the shared
token, which is appropriate for a home network and nothing more. Do not forward
port 7373 through your router. The actual game data travels over SSH and is
encrypted and authenticated by that.
