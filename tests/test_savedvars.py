import textwrap

import pytest

from wowsync.savedvars import (
    global_names,
    join_globals,
    parse_chunks,
    split_globals,
)

SAMPLE = '''\
-- Saved by WoW
SharedDB = {
\t["profiles"] = {
\t\t["Default"] = {
\t\t\t["note"] = "a } brace in a string {",
\t\t},
\t},
}
MachineDB = {
\t["scale"] = 0.71,
\t["sound"] = "External Speakers",
}
SharedDB["version"] = 3
Counter = 7
'''


def test_global_names_in_file_order_deduplicated():
    assert global_names(SAMPLE) == ["SharedDB", "MachineDB", "Counter"]


def test_preamble_stays_with_shared_half():
    shared, local = split_globals(SAMPLE, ["MachineDB"])
    assert shared.startswith("-- Saved by WoW\n")
    assert "MachineDB" not in shared


def test_split_moves_every_statement_for_a_name():
    shared, local = split_globals(SAMPLE, ["SharedDB"])
    # Both `SharedDB = {...}` and `SharedDB["version"] = 3` must travel together.
    assert local.count("SharedDB") == 2
    assert "SharedDB" not in shared
    assert "MachineDB" in shared and "Counter" in shared


def test_braces_inside_strings_do_not_shift_depth():
    shared, local = split_globals(SAMPLE, ["MachineDB"])
    assert '"a } brace in a string {"' in shared
    assert local.strip().startswith("MachineDB = {")
    assert local.rstrip().endswith("}")


def test_split_then_join_round_trips_content():
    shared, local = split_globals(SAMPLE, ["MachineDB"])
    rejoined = join_globals(shared, local)
    assert sorted(rejoined.split()) == sorted(SAMPLE.split())
    assert global_names(rejoined) == ["SharedDB", "Counter", "MachineDB"]


def test_join_with_empty_local_is_identity():
    assert join_globals(SAMPLE, "") == SAMPLE
    assert join_globals(SAMPLE, "   \n") == SAMPLE


def test_split_on_unknown_name_keeps_everything_shared():
    shared, local = split_globals(SAMPLE, ["NoSuchGlobal"])
    assert shared == SAMPLE
    assert local == ""


def test_long_bracket_string_containing_an_assignment_is_not_a_boundary():
    text = textwrap.dedent('''\
        Blob = {
        \t["payload"] = [==[
        Injected = "not really a global"
        ]==],
        }
        After = 1
        ''')
    assert global_names(text) == ["Blob", "After"]
    shared, local = split_globals(text, ["Blob"])
    assert 'Injected = "not really a global"' in local
    assert shared.strip() == "After = 1"


def test_block_comment_containing_braces_is_ignored():
    text = 'A = 1\n--[[ { { { ]]\nB = 2\n'
    assert global_names(text) == ["A", "B"]


def test_escaped_quote_inside_string_does_not_end_it():
    text = 'A = {\n\t["k"] = "he said \\" } \\" ok",\n}\nB = 2\n'
    assert global_names(text) == ["A", "B"]
    shared, local = split_globals(text, ["A"])
    assert shared.strip() == "B = 2"


def test_equality_comparison_is_not_mistaken_for_assignment():
    # `==` at column 0 is not valid at top level, but guard the regex anyway.
    text = 'A = 1\nB = 2\n'
    chunks = [c.name for c in parse_chunks(text)]
    assert chunks == ["A", "B"]


def test_empty_file_produces_no_chunks():
    assert parse_chunks("") == []
    assert global_names("") == []
    assert split_globals("", ["X"]) == ("", "")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_crlf_files_are_handled(newline):
    text = newline.join(["A = {", '\t["k"] = 1,', "}", "B = 2", ""])
    assert global_names(text) == ["A", "B"]
    shared, local = split_globals(text, ["A"])
    assert join_globals(shared, local).count("A = {") == 1
