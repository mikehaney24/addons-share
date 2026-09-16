import threading
import time

import pytest

import wowsync.daemon as daemon_module
from tests.conftest import ACCOUNT, Machine
from wowsync.daemon import IDLE, PLAYING, UNMANAGED, Worker
from wowsync.lease import LeaseClient
from wowsync.server import serve

ACCT_SV = f"WTF/Account/{ACCOUNT}/SavedVariables/Details.lua"


@pytest.fixture
def coordinator(tmp_path):
    httpd = serve("127.0.0.1", 0, tmp_path / "srv")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    yield f"http://{host}:{port}"
    httpd.shutdown()
    httpd.server_close()


class FakeGame:
    """Stands in for the WoW process for every worker in a test."""

    def __init__(self):
        self.running_on: set[str] = set()

    def install(self, monkeypatch):
        holder = {"machine": None}

        def fake_is_running(pattern=""):
            return holder["machine"] in self.running_on

        monkeypatch.setattr(daemon_module, "is_running", fake_is_running)
        return holder


@pytest.fixture
def game(monkeypatch):
    fake = FakeGame()
    fake.holder = fake.install(monkeypatch)
    return fake


def make_worker(machine: Machine, coordinator: str, game: FakeGame) -> Worker:
    client = LeaseClient(coordinator)
    worker = Worker(machine.config, machine.profile, client, paths=machine.paths)

    original_tick = worker.tick

    def tick(now=None):
        # Point the shared process fake at this worker's machine for the call.
        game.holder["machine"] = machine.name
        try:
            return original_tick(now if now is not None else time.time())
        finally:
            game.holder["machine"] = None

    worker.tick = tick
    worker.machine_name = machine.name
    return worker


def test_launch_takes_the_lease_and_exit_hands_it_back(mac: Machine, coordinator, game):
    worker = make_worker(mac, coordinator, game)
    worker.tick()  # idle: seeds the server
    assert worker.state == IDLE

    game.running_on.add("mac-studio")
    worker.tick()
    assert worker.state == PLAYING
    assert worker.client.holds_lease

    mac.write(ACCT_SV, 'DetailsDB = {\n\t["theme"] = "played",\n}\n')
    game.running_on.discard("mac-studio")
    worker.tick()

    assert worker.state == IDLE
    assert not worker.client.holds_lease
    assert LeaseClient(coordinator).status("forever").held is False


def test_the_other_machine_is_current_before_you_sit_down(
    mac: Machine, linux: Machine, coordinator, game
):
    mac_worker = make_worker(mac, coordinator, game)
    linux_worker = make_worker(linux, coordinator, game)

    mac_worker.tick()
    linux_worker.tick()

    # Play on the mac and quit.
    game.running_on.add("mac-studio")
    mac_worker.tick()
    mac.write(ACCT_SV, 'DetailsDB = {\n\t["theme"] = "from-the-mac",\n}\n')
    game.running_on.discard("mac-studio")
    mac_worker.tick()

    # The Linux box catches up on its own, before the game is ever launched there.
    linux_worker.pull_requested = True
    linux_worker.tick()
    assert "from-the-mac" in linux.read(ACCT_SV)

    # So launching there is just a confirmation.
    game.running_on.add("linux-rig")
    linux_worker.tick()
    assert linux_worker.state == PLAYING
    assert "from-the-mac" in linux.read(ACCT_SV)


def test_launching_while_the_other_machine_plays_does_not_take_over(
    mac: Machine, linux: Machine, coordinator, game
):
    mac_worker = make_worker(mac, coordinator, game)
    linux_worker = make_worker(linux, coordinator, game)
    mac_worker.tick()
    linux_worker.tick()

    game.running_on.add("mac-studio")
    mac_worker.tick()
    assert mac_worker.state == PLAYING

    game.running_on.add("linux-rig")
    linux_worker.tick()

    assert linux_worker.state == UNMANAGED
    assert not linux_worker.client.holds_lease
    # The mac keeps the lease it is actively using.
    assert LeaseClient(coordinator).status("forever").machine == "mac-studio"


def test_an_unmanaged_session_never_buries_the_other_machines_work(
    mac: Machine, linux: Machine, coordinator, game
):
    mac_worker = make_worker(mac, coordinator, game)
    linux_worker = make_worker(linux, coordinator, game)
    mac_worker.tick()
    linux_worker.tick()

    game.running_on.add("mac-studio")
    mac_worker.tick()
    mac.write(ACCT_SV, 'DetailsDB = {\n\t["theme"] = "mac-session",\n}\n')

    # Linux is launched anyway and edits the same file.
    game.running_on.add("linux-rig")
    linux_worker.tick()
    assert linux_worker.state == UNMANAGED
    linux.write(ACCT_SV, 'DetailsDB = {\n\t["theme"] = "linux-session",\n}\n')

    game.running_on.discard("mac-studio")
    mac_worker.tick()
    game.running_on.discard("linux-rig")
    linux_worker.tick()

    # The mac's session is what the server holds; the Linux session is kept
    # locally and on a conflict branch rather than silently winning or vanishing.
    server_state = mac.syncer.shared.out("show", "refs/remotes/origin/main:" + ACCT_SV)
    assert "mac-session" in server_state
    assert "linux-session" in linux.read(ACCT_SV)
    conflict = linux_worker.syncer.load_state().get("conflict")
    assert conflict and conflict["branch"].startswith("conflict/linux-rig/")


def test_playing_with_the_server_down_still_records_the_session(mac: Machine, game):
    worker = Worker(
        mac.config, mac.profile, LeaseClient("http://127.0.0.1:1", timeout=0.3), paths=mac.paths
    )
    original = worker.tick

    def tick():
        game.holder["machine"] = "mac-studio"
        try:
            original(time.time())
        finally:
            game.holder["machine"] = None

    game.running_on.add("mac-studio")
    tick()
    assert worker.state == UNMANAGED

    mac.write(ACCT_SV, 'DetailsDB = {\n\t["theme"] = "offline",\n}\n')
    game.running_on.discard("mac-studio")
    tick()

    assert worker.state == IDLE
    history = [c.subject for c in mac.syncer.shared.log(limit=5)]
    assert any("session end" in s for s in history)


def test_in_session_snapshots_give_a_rollback_point(mac: Machine, coordinator, game):
    mac.config.daemon.snapshot_minutes = 0.0  # snapshot on every tick
    worker = make_worker(mac, coordinator, game)
    worker.tick()

    game.running_on.add("mac-studio")
    worker.tick()
    before = mac.syncer.shared.head()

    mac.write(ACCT_SV, 'DetailsDB = {\n\t["theme"] = "mid-session",\n}\n')
    worker.tick()

    assert mac.syncer.shared.head() != before
    assert any(
        "in-session snapshot" in c.subject for c in mac.syncer.shared.log(limit=5)
    )


def test_remote_events_only_wake_an_idle_machine(mac: Machine, coordinator, game):
    worker = make_worker(mac, coordinator, game)
    worker.tick()
    worker.pull_requested = False

    worker.on_remote_event("state-changed", {"machine": "linux-rig", "profile": "forever"})
    assert worker.pull_requested

    game.running_on.add("mac-studio")
    worker.tick()
    worker.pull_requested = False
    worker.on_remote_event("state-changed", {"machine": "linux-rig", "profile": "forever"})
    assert not worker.pull_requested, "a pull must never land under a running client"


def test_a_machine_ignores_its_own_events(mac: Machine, coordinator, game):
    worker = make_worker(mac, coordinator, game)
    worker.tick()
    worker.pull_requested = False
    worker.on_remote_event("state-changed", {"machine": "mac-studio", "profile": "forever"})
    assert not worker.pull_requested


def test_losing_the_lease_mid_session_stops_claiming_authority(
    mac: Machine, coordinator, game
):
    mac.config.daemon.heartbeat_seconds = 0.0
    worker = make_worker(mac, coordinator, game)
    worker.tick()

    game.running_on.add("mac-studio")
    worker.tick()
    assert worker.state == PLAYING

    LeaseClient(coordinator).acquire("forever", "linux-rig", ttl=60, steal=True)
    worker.tick()

    assert worker.state == UNMANAGED
