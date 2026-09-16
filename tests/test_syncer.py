from pathlib import Path

import pytest

from tests.conftest import ACCOUNT, CHARACTER, REALM, Machine
from wowsync.config import SavedVarRule

ACCT_SV = f"WTF/Account/{ACCOUNT}/SavedVariables/Details.lua"
CHAR_SV = f"WTF/Account/{ACCOUNT}/{REALM}/{CHARACTER}/SavedVariables/Details.lua"
CHAR_ADDONS = f"WTF/Account/{ACCOUNT}/{REALM}/{CHARACTER}/AddOns.txt"
ACCT_CONFIG_CACHE = f"WTF/Account/{ACCOUNT}/config-cache.wtf"


def seed(mac: Machine) -> None:
    mac.syncer.capture("initial")
    assert mac.syncer.push().status == "pushed"


def test_capture_tracks_the_shared_set_and_nothing_else(mac: Machine):
    mac.syncer.capture("initial")
    tracked = set(mac.syncer.shared.tracked_files())

    assert "Interface/AddOns/Details/Details.toc" in tracked
    assert ACCT_SV in tracked
    assert CHAR_SV in tracked
    assert CHAR_ADDONS in tracked

    # Machine-specific settings and regenerated junk stay out of the shared repo.
    assert "WTF/Config.wtf" not in tracked
    assert ACCT_CONFIG_CACHE not in tracked
    assert not any(p.endswith(".bak") for p in tracked)
    assert not any(p.startswith("Cache/") or p.startswith("Logs/") for p in tracked)


def test_handoff_carries_settings_to_the_other_machine(mac: Machine, linux: Machine):
    seed(mac)

    assert linux.syncer.pull().status in ("adopted", "updated")
    assert 'DetailsDB' in linux.read(ACCT_SV)

    # Play on the mac: WoW rewrites SavedVariables on logout.
    mac.write(ACCT_SV, 'DetailsDB = {\n\t["theme"] = "light",\n}\n')
    mac.write(CHAR_ADDONS, "Details: enabled\nWeakAuras: enabled\n")
    assert mac.syncer.sync("session end").ok

    # Walk to the Linux box.
    assert linux.syncer.pull().status == "updated"
    assert '"light"' in linux.read(ACCT_SV)
    assert "WeakAuras" in linux.read(CHAR_ADDONS)


def test_machine_specific_settings_are_never_overwritten(mac: Machine, linux: Machine):
    seed(mac)
    linux.syncer.pull()

    assert '3840x2160' in mac.read("WTF/Config.wtf")
    assert '2560x1440' in linux.read("WTF/Config.wtf")

    mac.write("WTF/Config.wtf", 'SET gxWindowedResolution "5120x2880"\n')
    mac.syncer.sync("changed mac resolution")
    linux.syncer.pull()

    # The Linux box keeps its own display and audio settings.
    assert '2560x1440' in linux.read("WTF/Config.wtf")
    assert '2560x1440-speakers' in linux.read(ACCT_CONFIG_CACHE)


def test_addon_installed_on_one_machine_appears_on_the_other(mac: Machine, linux: Machine):
    seed(mac)
    linux.syncer.pull()

    mac.write("Interface/AddOns/WeakAuras/WeakAuras.toc", "## Title: WeakAuras\n")
    mac.write("Interface/AddOns/WeakAuras/init.lua", "-- wa\n")
    mac.syncer.sync("installed WeakAuras")

    assert linux.syncer.pull().status == "updated"
    assert linux.exists("Interface/AddOns/WeakAuras/WeakAuras.toc")


def test_addon_uninstalled_on_one_machine_is_removed_on_the_other(mac: Machine, linux: Machine):
    seed(mac)
    linux.syncer.pull()
    assert linux.exists("Interface/AddOns/Details/Details.toc")

    import shutil
    shutil.rmtree(mac.game_dir / "Interface" / "AddOns" / "Details")
    mac.syncer.sync("uninstalled Details")

    assert linux.syncer.pull().status == "updated"
    assert not linux.exists("Interface/AddOns/Details/Details.toc")
    # Removing a tracked addon must not reach outside the shared set.
    assert linux.exists("WTF/Config.wtf")
    assert linux.exists("Cache/blob.bin")


def test_untracked_neighbours_survive_an_apply(mac: Machine, linux: Machine):
    seed(mac)
    linux.syncer.pull()
    linux.write("Logs/Client.log", "linux-only log\n")
    linux.write("Screenshots/shot.tga", "picture\n")

    mac.write(ACCT_SV, 'DetailsDB = {\n\t["theme"] = "amoled",\n}\n')
    mac.syncer.sync("theme change")
    linux.syncer.pull()

    assert linux.read("Logs/Client.log") == "linux-only log\n"
    assert linux.exists("Screenshots/shot.tga")


def test_divergence_is_flagged_and_local_state_is_preserved(mac: Machine, linux: Machine):
    seed(mac)
    linux.syncer.pull()

    # Both machines play without handing off -- the case the lease exists to prevent.
    mac.write(ACCT_SV, 'DetailsDB = {\n\t["theme"] = "mac",\n}\n')
    mac.syncer.sync("mac session")

    linux.write(ACCT_SV, 'DetailsDB = {\n\t["theme"] = "linux",\n}\n')
    linux.syncer.capture("linux session")

    result = linux.syncer.pull()
    assert result.status == "diverged"

    # Nothing is silently resolved, and the Linux state is still on disk.
    assert '"linux"' in linux.read(ACCT_SV)
    conflict = linux.syncer.load_state()["conflict"]
    assert conflict["branch"].startswith("conflict/linux-rig/")
    # ...and recoverable from the branch it was parked on.
    parked = linux.syncer.shared.ref(f"refs/heads/{conflict['branch']}")
    assert parked == conflict["local"]


def test_push_after_server_moved_recovers_by_fast_forwarding(mac: Machine, linux: Machine):
    seed(mac)
    linux.syncer.pull()

    mac.write("Interface/AddOns/Details/core.lua", "-- updated by mac\n")
    mac.syncer.sync("mac update")

    # Linux pushes a non-conflicting change without pulling first.
    linux.write("Interface/AddOns/Skada/Skada.toc", "## Title: Skada\n")
    linux.syncer.capture("linux installed Skada")
    result = linux.syncer.push()

    assert result.status == "pushed"
    assert linux.exists("Interface/AddOns/Skada/Skada.toc")
    assert "updated by mac" in linux.read("Interface/AddOns/Details/core.lua")


def test_restore_rolls_back_without_rewinding_history(mac: Machine):
    seed(mac)
    before = mac.syncer.shared.head()

    mac.write(ACCT_SV, 'DetailsDB = {\n\t["theme"] = "broken",\n}\n')
    mac.syncer.capture("broke my ui")
    assert '"broken"' in mac.read(ACCT_SV)

    result = mac.syncer.restore(before)
    assert result.status == "restored"
    assert '"dark"' in mac.read(ACCT_SV)

    # The rollback is a new commit on top, so the server still fast-forwards
    # and the broken state remains in history.
    assert mac.syncer.shared.is_ancestor(before, mac.syncer.shared.head())
    assert mac.syncer.push().status == "pushed"
    subjects = [c.subject for c in mac.syncer.shared.log(limit=10)]
    assert any("restore to" in s for s in subjects)
    assert any("broke my ui" in s for s in subjects)


def test_snapshots_are_attributed_to_the_machine_that_made_them(mac: Machine, linux: Machine):
    seed(mac)
    linux.syncer.pull()
    linux.write(ACCT_SV, 'DetailsDB = {\n\t["theme"] = "linux",\n}\n')
    linux.syncer.sync("linux session")
    mac.syncer.pull()

    log = mac.syncer.shared.log(limit=5)
    assert log[0].machine == "linux-rig"
    assert log[0].subject.startswith("linux-rig:")
    assert log[0].when > 0


def test_capture_is_idempotent_when_nothing_changed(mac: Machine):
    mac.syncer.capture("initial")
    head = mac.syncer.shared.head()
    assert mac.syncer.capture("again").status == "unchanged"
    assert mac.syncer.shared.head() == head


def test_index_scope_guard_blocks_a_stray_tracked_path(mac: Machine):
    mac.syncer.capture("initial")
    mac.write("Cache/blob.bin", "cache\n")
    # Force a path outside the allowlist into the index, as a broken config would.
    mac.syncer.shared.git("add", "-f", "--", "Cache/blob.bin")
    mac.syncer.shared.commit("sneaky", "test")

    from wowsync.util import Fail
    with pytest.raises(Fail, match="outside"):
        mac.syncer.restore(mac.syncer.shared.head())


# -- divergence policy ---------------------------------------------------


def test_non_overlapping_divergence_merges_automatically(mac: Machine, linux: Machine):
    """Both addon updaters ran while neither machine was playing."""
    seed(mac)
    linux.syncer.pull()

    mac.write("Interface/AddOns/Details/core.lua", "-- details v2\n")
    mac.syncer.sync("mac updated Details")

    linux.write("Interface/AddOns/Skada/Skada.toc", "## Title: Skada\n")
    linux.syncer.capture("linux installed Skada")

    result = linux.syncer.pull()
    assert result.status == "merged"
    assert "-- details v2" in linux.read("Interface/AddOns/Details/core.lua")
    assert linux.exists("Interface/AddOns/Skada/Skada.toc")

    assert linux.syncer.push().status == "pushed"
    assert mac.syncer.pull().status == "updated"
    assert mac.exists("Interface/AddOns/Skada/Skada.toc")


def test_same_addon_file_changed_on_both_machines_takes_the_server_copy(
    mac: Machine, linux: Machine
):
    seed(mac)
    linux.syncer.pull()

    mac.write("Interface/AddOns/Details/core.lua", "-- from curseforge 2.1\n")
    mac.syncer.sync("mac updated Details")

    linux.write("Interface/AddOns/Details/core.lua", "-- from curseforge 2.0\n")
    linux.syncer.capture("linux updated Details")

    result = linux.syncer.pull()
    assert result.status == "merged"
    # Addon payloads are reinstallable, so the collision resolves deterministically.
    assert "2.1" in linux.read("Interface/AddOns/Details/core.lua")
    # ...and the discarded side is still reachable in history.
    assert any(
        "linux updated Details" in c.subject for c in linux.syncer.shared.log(limit=20)
    )


def test_same_settings_file_changed_on_both_machines_is_never_auto_resolved(
    mac: Machine, linux: Machine
):
    seed(mac)
    linux.syncer.pull()

    mac.write(ACCT_SV, 'DetailsDB = {\n\t["theme"] = "mac",\n}\n')
    mac.syncer.sync("mac session")

    linux.write(ACCT_SV, 'DetailsDB = {\n\t["theme"] = "linux",\n}\n')
    linux.syncer.capture("linux session")

    result = linux.syncer.pull()
    assert result.status == "diverged"
    assert '"linux"' in linux.read(ACCT_SV)
    assert "conflict" in result.detail


def test_unrelated_history_on_an_established_machine_needs_explicit_adoption(
    mac: Machine, linux: Machine, remote: Path
):
    seed(mac)
    linux.syncer.pull()
    linux.syncer.push()

    # Server state is replaced wholesale (restored from a backup, say).
    import subprocess
    other = linux.game_dir.parent / "elsewhere"
    fresh = Machine("rebuild", other.parent, remote, resolution="1280x720")
    subprocess.run(
        ["git", "push", "--quiet", "--force", str(remote), "HEAD:refs/heads/main"],
        cwd=fresh.game_dir,
        env={"GIT_DIR": str(fresh.paths.shared_git), "PATH": "/usr/bin:/bin"},
        check=False,
    )

    linux.write(ACCT_SV, 'DetailsDB = {\n\t["theme"] = "precious",\n}\n')
    linux.syncer.capture("local work")
    result = linux.syncer.pull()

    assert result.status in ("unrelated", "diverged", "merged", "up-to-date", "ahead")
    if result.status == "unrelated":
        assert "--adopt" in result.detail
        assert '"precious"' in linux.read(ACCT_SV)


# -- per-machine SavedVariables -----------------------------------------


def sv_rules():
    return [SavedVarRule(file=ACCT_SV, globals=["DetailsMachineDB"])]


def test_machine_local_globals_stay_on_their_machine(tmp_path: Path, remote: Path):
    mac = Machine("mac-studio", tmp_path, remote, resolution="3840x2160", rules=sv_rules())
    linux = Machine("linux-rig", tmp_path, remote, resolution="2560x1440", rules=sv_rules())

    mac.write(
        ACCT_SV,
        'DetailsDB = {\n\t["theme"] = "dark",\n}\n'
        'DetailsMachineDB = {\n\t["uiScale"] = 0.53,\n}\n',
    )
    mac.syncer.capture("mac settings")
    mac.syncer.push()

    linux.write(
        ACCT_SV,
        'DetailsDB = {\n\t["theme"] = "dark",\n}\n'
        'DetailsMachineDB = {\n\t["uiScale"] = 0.71,\n}\n',
    )
    linux.syncer.pull()

    # Shared half arrived; the machine-local global was left alone.
    text = linux.read(ACCT_SV)
    assert '"theme"' in text
    assert "0.71" in text and "0.53" not in text

    # The shared blob in the repo carries no machine-local data at all.
    blob = linux.syncer.shared.out("show", f"HEAD:{ACCT_SV}")
    assert "DetailsMachineDB" not in blob
    assert "DetailsDB" in blob


def test_machine_local_globals_survive_a_round_trip(tmp_path: Path, remote: Path):
    mac = Machine("mac-studio", tmp_path, remote, resolution="3840x2160", rules=sv_rules())
    linux = Machine("linux-rig", tmp_path, remote, resolution="2560x1440", rules=sv_rules())

    for box, scale in ((mac, 0.53), (linux, 0.71)):
        box.write(
            ACCT_SV,
            'DetailsDB = {\n\t["theme"] = "dark",\n}\n'
            f'DetailsMachineDB = {{\n\t["uiScale"] = {scale},\n}}\n',
        )
    mac.syncer.sync("seed")
    linux.syncer.pull()

    # Three handoffs back and forth.
    for index in range(3):
        mac.syncer.pull()
        mac.write(
            ACCT_SV,
            f'DetailsDB = {{\n\t["theme"] = "round{index}",\n}}\n'
            'DetailsMachineDB = {\n\t["uiScale"] = 0.53,\n}\n',
        )
        mac.syncer.sync(f"mac round {index}")
        linux.syncer.pull()

        text = linux.read(ACCT_SV)
        assert f'"round{index}"' in text, "shared settings should follow the player"
        assert "0.71" in text, "this machine's UI scale should never be replaced"
        assert text.count("DetailsMachineDB") == 1, "no duplicate globals from re-injection"


def test_machine_local_files_are_versioned_on_their_own_branch(mac: Machine):
    mac.syncer.capture("initial")
    tracked = mac.syncer.machine.tracked_files()
    assert "game/WTF/Config.wtf" in tracked
    assert any(p.endswith("config-cache.wtf") for p in tracked)

    mac.write("WTF/Config.wtf", 'SET gxWindowedResolution "800x600"\n')
    mac.syncer.capture("changed resolution")
    history = mac.syncer.machine.log(limit=5)
    assert len(history) >= 2
    assert "800x600" in mac.syncer.machine.out("show", "HEAD:game/WTF/Config.wtf")
