# tests/test_tool_annotations.py
from pathlib import Path

import anyio

import orcaslicer_mcp.server as srv

ROOT = Path(__file__).resolve().parents[1]


def test_annotation_table_matches_registered_tools_exactly():
    # the table is the completeness contract, in both directions: every tool this server declares has
    # an entry, and every entry is a tool that is really registered. A decorator lost in a refactor,
    # a registration skipped by accident, or a tool added without thinking about its hints all fail
    # here instead of quietly changing what a client is shown.
    assert set(srv.mcp._tool_manager._tools) == set(srv._TOOL_ANNOTATIONS)


def test_the_direct_cc2_tools_are_declared_like_every_other_tool():
    # the discovery invariant, stated positively now that there are no read/write switches: the nine
    # direct tools are in tools/list because they are declared, whatever the environment happens to
    # have configured. A printer address is what lets one of them reach a printer; it is not what
    # makes it exist, and no spelling of "unconfigured" may hide a tool from a client.
    registered = set(srv.mcp._tool_manager._tools)
    assert set(srv.CC2_TOOL_NAMES) <= registered
    assert {n for n in registered if n.startswith("cc2_")} == set(srv.CC2_TOOL_NAMES)
    listed = {t.name for t in anyio.run(srv.mcp.list_tools)}
    assert listed == registered


def test_the_read_and_write_switches_are_gone_from_the_code_and_the_docs():
    # the switches were removed on purpose — configuring the printer is the only opt-in there is — so
    # the guard is that their names cannot come back unnoticed, in the server or in what it promises
    # a user. A gate that returns under these names fails this before it can hide a tool again.
    gone = ("ENV_CC2_READ", "ENV_CC2_WRITE", "_cc2_enabled", "_cc2_gate", "_cc2_gated_tool",
            "cc2_hidden_tools", "cc2_visible_tools", "register_cc2_tools", "apply_cc2_discovery")
    for name in gone:
        assert not hasattr(srv, name), f"server still carries {name}"
    for relative in ("src/orcaslicer_mcp/server.py", "README.md", "scripts/gen_lhm_tools.py"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        for name in ("ORCA_CC2_READ", "ORCA_CC2_WRITE"):
            assert name not in text, f"{relative} still mentions {name}"


def test_every_tool_has_title_and_hints():
    for name, tool in srv.mcp._tool_manager._tools.items():
        ann = tool.annotations
        assert ann is not None, name
        assert ann.title, name
        assert ann.read_only_hint in (True, False), name
        if ann.read_only_hint:
            assert ann.destructive_hint is None, name
        else:
            assert ann.destructive_hint in (True, False), name


def test_annotations_surface_in_list_tools():
    tools = anyio.run(srv.mcp.list_tools)
    by_name = {t.name: t for t in tools}
    assert by_name["get_status"].annotations.read_only_hint is True
    assert by_name["delete_preset"].annotations.read_only_hint is False
    assert by_name["delete_preset"].annotations.destructive_hint is True
    assert by_name["load_model"].annotations.destructive_hint is False
    assert all(t.annotations and t.annotations.title for t in tools)


def test_printer_tools_are_read_only_and_open_world():
    ann = srv.mcp._tool_manager._tools["get_printer_status"].annotations
    assert ann.read_only_hint is True and ann.open_world_hint is True


def test_wait_for_printer_is_read_only_and_open_world():
    ann = srv.mcp._tool_manager._tools["wait_for_printer"].annotations
    assert ann.read_only_hint is True and ann.open_world_hint is True


def test_list_print_history_writes_locally_but_is_safe_to_repeat():
    ann = srv.mcp._tool_manager._tools["list_print_history"].annotations
    assert ann.read_only_hint is False and ann.destructive_hint is False
    assert ann.idempotent_hint is True and ann.open_world_hint is True


def test_check_printer_match_is_read_only_and_open_world():
    tool = srv.mcp._tool_manager._tools["check_printer_match"]
    ann = tool.annotations
    assert tool.title == "Check the profile against the printer"
    assert ann.title == "Check the profile against the printer"
    assert ann.read_only_hint is True and ann.destructive_hint is None
    assert ann.idempotent_hint is None and ann.open_world_hint is True
