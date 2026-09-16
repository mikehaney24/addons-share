from pathlib import Path

import pytest

from tests.conftest import ACCOUNT, make_game_dir
from wowsync.cli import main


@pytest.fixture
def workspace(tmp_path, monkeypatch, remote):
    game = make_game_dir(tmp_path / "mac")
    monkeypatch.setenv("WOWSYNC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("wowsync.cli.is_running", lambda pattern="": False)
    monkeypatch.setattr("wowsync.doctor.find_processes", lambda pattern="": [])
    return {
        "game": game,
        "config": tmp_path / "wowsync.toml",
        "remote": remote,
        "tmp": tmp_path,
    }


def run(workspace, *args) -> int:
    return main(["--config", str(workspace["config"]), *args])


def init(workspace, **kwargs) -> int:
    return run(
        workspace,
        "init",
        "--game-dir", str(workspace["game"]),
        "--remote", str(workspace["remote"]),
        "--machine-id", kwargs.get("machine_id", "mac-studio"),
    )


def test_init_writes_a_readable_config_and_sets_up_the_repo(workspace, capsys):
    assert init(workspace) == 0
    body = workspace["config"].read_text()

    assert "machine_id = 'mac-studio'" in body
    assert str(workspace["game"]) in body
    # The file has to explain itself; it is the thing the user edits.
    assert "Config.wtf" in body and "resolution" in body
    assert (Path(workspace["tmp"]) / "state" / "forever" / "shared.git").is_dir()


def test_init_refuses_to_clobber_an_existing_config(workspace, capsys):
    assert init(workspace) == 0
    capsys.readouterr()

    assert init(workspace) == 1
    assert "already exists" in capsys.readouterr().err


def test_init_with_force_overwrites(workspace):
    assert init(workspace) == 0
    assert run(
        workspace, "init",
        "--game-dir", str(workspace["game"]),
        "--remote", str(workspace["remote"]),
        "--machine-id", "renamed",
        "--force",
    ) == 0
    assert "renamed" in workspace["config"].read_text()


def test_sync_then_status_and_log_describe_the_state(workspace, capsys):
    init(workspace)
    capsys.readouterr()

    assert run(workspace, "sync") == 0
    assert run(workspace, "status") == 0
    status = capsys.readouterr().out
    assert "machine: mac-studio" in status
    assert "in sync with the server" in status

    assert run(workspace, "log") == 0
    history = capsys.readouterr().out
    assert "mac-studio:" in history


def test_snapshot_and_restore_round_trip(workspace, capsys):
    init(workspace)
    run(workspace, "sync")
    capsys.readouterr()

    sv = workspace["game"] / "WTF" / "Account" / ACCOUNT / "SavedVariables" / "Details.lua"
    run(workspace, "log", "-n", "1")
    first = capsys.readouterr().out.split()[0]

    sv.write_text('DetailsDB = {\n\t["theme"] = "regrettable",\n}\n')
    run(workspace, "snapshot", "-m", "bad idea")
    capsys.readouterr()

    assert run(workspace, "restore", first, "--yes") == 0
    assert '"dark"' in sv.read_text()


def test_sync_refuses_to_run_while_the_game_is_open(workspace, monkeypatch, capsys):
    init(workspace)
    capsys.readouterr()
    monkeypatch.setattr("wowsync.cli.is_running", lambda pattern="": True)

    assert run(workspace, "sync") == 1
    assert "Quit it first" in capsys.readouterr().err


def test_doctor_reports_a_machine_specific_file_that_leaked_into_the_shared_set(
    workspace, capsys
):
    init(workspace)
    run(workspace, "sync")
    capsys.readouterr()

    # Force Config.wtf into the shared index, as a hand-edited config might.
    from wowsync import config as config_module
    from wowsync.paths import ProfilePaths
    from wowsync.syncer import Syncer

    config = config_module.load(workspace["config"])
    profile = config.profile(None)
    syncer = Syncer(config, profile, ProfilePaths("forever"))
    syncer.shared.git("add", "-f", "--", "WTF/Config.wtf")
    syncer.shared.commit("leak", "test")

    assert run(workspace, "doctor") == 1
    out = capsys.readouterr().out
    assert "machine-specific file(s) are being shared" in out


def test_doctor_passes_on_a_healthy_setup(workspace, capsys):
    init(workspace)
    run(workspace, "sync")
    capsys.readouterr()

    assert run(workspace, "doctor") == 0
    out = capsys.readouterr().out
    assert "0 problem(s)" in out
    assert "no case-colliding addon folders" in out


def test_doctor_flags_addon_folders_that_collide_by_case(workspace, capsys):
    init(workspace)
    addons = workspace["game"] / "Interface" / "AddOns"
    (addons / "details").mkdir()
    (addons / "details" / "x.lua").write_text("")
    capsys.readouterr()

    assert run(workspace, "doctor") == 1
    out = capsys.readouterr().out
    assert "differ only by case" in out
    assert "macOS cannot hold both" in out


def test_resolve_reports_nothing_when_there_is_no_conflict(workspace, capsys):
    init(workspace)
    run(workspace, "sync")
    capsys.readouterr()

    assert run(workspace, "resolve", "--list") == 0
    assert "No conflict is pending." in capsys.readouterr().out


def test_show_prints_a_file_from_history(workspace, capsys):
    init(workspace)
    run(workspace, "sync")
    capsys.readouterr()

    path = f"WTF/Account/{ACCOUNT}/SavedVariables/Details.lua"
    assert run(workspace, "show", "HEAD", path) == 0
    assert "DetailsDB" in capsys.readouterr().out


def test_unknown_profile_is_a_clear_error(workspace, capsys):
    init(workspace)
    capsys.readouterr()

    assert run(workspace, "--profile", "nope", "pull") == 1
    assert "unknown profile" in capsys.readouterr().err


def test_play_parses_a_launch_command_without_shadowing_the_subcommand():
    from wowsync.cli import build_parser

    args = build_parser().parse_args(["play", "--", "open", "-a", "World of Warcraft"])
    assert args.command == "play"
    assert [a for a in args.launch if a != "--"] == ["open", "-a", "World of Warcraft"]


def test_play_without_a_launch_command_is_allowed():
    from wowsync.cli import build_parser

    args = build_parser().parse_args(["play"])
    assert args.command == "play"
    assert args.launch == []


def test_example_config_matches_what_init_writes():
    """Keeps the checked-in example from drifting away from the generator."""
    import tomllib
    from wowsync.cli import render_config
    from wowsync.config import from_dict

    text = Path("wowsync.example.toml").read_text()
    config = from_dict(tomllib.loads(text))
    profile = config.profile(None)
    assert profile.name == "forever"
    assert profile.machine_local, "the example must show the machine-local list"

    generated = render_config(
        machine_id="mac-studio",
        profile="forever",
        game_dir="/Applications/World of Warcraft Forever/_forever_",
        remote="ssh://you@homeserver.local/Users/you/wowsync/repos/forever.git",
        server="http://homeserver.local:7373",
        token="paste-the-token-from-the-server-here",
    )
    assert generated in text, "regenerate wowsync.example.toml from render_config()"
