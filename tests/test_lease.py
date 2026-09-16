import threading
import time

import pytest

from wowsync.lease import CoordinatorUnreachable, LeaseClient
from wowsync.server import LeaseStore, serve


@pytest.fixture
def coordinator(tmp_path):
    httpd = serve("127.0.0.1", 0, tmp_path / "srv", token="s3cret")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    yield f"http://{host}:{port}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def mac(coordinator):
    return LeaseClient(coordinator, token="s3cret")


@pytest.fixture
def linux(coordinator):
    return LeaseClient(coordinator, token="s3cret")


def test_one_machine_at_a_time(mac, linux):
    assert mac.acquire("forever", "mac-studio", ttl=60).held

    denied = linux.acquire("forever", "linux-rig", ttl=60)
    assert not denied.held
    assert denied.machine == "mac-studio"
    assert not denied.stale


def test_release_hands_over_to_the_other_machine(mac, linux):
    mac.acquire("forever", "mac-studio", ttl=60)
    assert mac.release("forever", "mac-studio")
    assert linux.acquire("forever", "linux-rig", ttl=60).held


def test_a_crashed_holder_expires_instead_of_wedging(mac, linux):
    assert mac.acquire("forever", "mac-studio", ttl=0.5).held
    time.sleep(0.7)

    state = linux.status("forever")
    assert state.held and state.stale
    # Nobody has to walk over and clear it by hand.
    assert linux.acquire("forever", "linux-rig", ttl=60).held


def test_heartbeat_keeps_a_long_session_alive(mac, linux):
    mac.acquire("forever", "mac-studio", ttl=0.6)
    for _ in range(4):
        time.sleep(0.2)
        assert mac.heartbeat("forever")
    assert not linux.acquire("forever", "linux-rig", ttl=60).held


def test_heartbeat_fails_after_the_lease_was_stolen(mac, linux):
    mac.acquire("forever", "mac-studio", ttl=60)
    assert linux.acquire("forever", "linux-rig", ttl=60, steal=True).held

    assert not mac.heartbeat("forever")
    assert not mac.holds_lease


def test_reacquiring_your_own_lease_is_not_an_error(mac):
    assert mac.acquire("forever", "mac-studio", ttl=60).held
    assert mac.acquire("forever", "mac-studio", ttl=60).held


def test_a_third_party_cannot_release_someone_elses_lease(mac, linux, coordinator):
    mac.acquire("forever", "mac-studio", ttl=60)
    assert not linux.release("forever", "linux-rig")
    assert linux.status("forever").machine == "mac-studio"


def test_profiles_are_leased_independently(mac, linux):
    assert mac.acquire("forever", "mac-studio", ttl=60).held
    assert linux.acquire("retail", "linux-rig", ttl=60).held


def test_requests_without_the_token_are_rejected(coordinator):
    anonymous = LeaseClient(coordinator, token="")
    code, _ = anonymous._request("GET", "/v1/status?profile=forever")
    assert code == 401


def test_unreachable_server_is_reported_not_swallowed():
    offline = LeaseClient("http://127.0.0.1:1", token="", timeout=0.5)
    with pytest.raises(CoordinatorUnreachable):
        offline.status("forever")


def test_events_reach_an_idle_subscriber(mac, linux, coordinator):
    seen = []
    ready = threading.Event()
    stop = threading.Event()

    def on_event(event, payload):
        seen.append((event, payload))
        ready.set()

    watcher = threading.Thread(target=linux.watch, args=(on_event, stop), daemon=True)
    watcher.start()
    time.sleep(0.3)  # let the subscription land

    mac.notify("forever", "mac-studio", event="state-changed")
    assert ready.wait(5), "idle machine was not told that new state is available"
    stop.set()

    events = [e for e, _ in seen]
    assert "state-changed" in events


def test_lease_store_survives_a_service_restart(tmp_path):
    first = LeaseStore(tmp_path / "leases.json")
    ok, lease = first.acquire("forever", "mac-studio", ttl=600)
    assert ok

    reopened = LeaseStore(tmp_path / "leases.json")
    current = reopened.get("forever")
    assert current is not None
    assert current.machine == "mac-studio"
    assert current.token == lease.token


def test_concurrent_acquires_produce_exactly_one_winner(tmp_path):
    store = LeaseStore(tmp_path / "leases.json")
    results = []
    barrier = threading.Barrier(8)

    def contend(index):
        barrier.wait()
        ok, lease = store.acquire("forever", f"machine-{index}", ttl=60)
        results.append((ok, lease.machine))

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = {machine for ok, machine in results if ok}
    assert len(winners) == 1, f"expected a single lease holder, got {winners}"
