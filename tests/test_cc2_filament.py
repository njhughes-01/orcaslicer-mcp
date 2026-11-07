"""CC2 filament load/unload (1024 Feed, 1025 Retreat, 2001 LoadFilamenting, 2002
UnloadFilamenting) against scripted replies: nothing opens a socket, the fake transport records
every method with the timeout it was given, and the frames are the observed 1002 shapes of the state
tests plus a 2005 whose tray ids come from the protocol doc's own real-world CANVAS sample. What
these tests hold the module to:

* the four frames are the LAN control's own — 1024/1025 with no parameters at all, 2001/2002 with
  exactly the canvas_id/tray_id pair the printer reported — all four waited for its own 300 s, and
  no identifier is ever invented or hardcoded (a printer naming 2/3 is commanded 2/3);
* the candidate's frame lock: a 1024 carrying a length, a 2001 carrying a colour or a slot map, a
  bool or a negative or a text id, and an id handed to a toolhead verb all refuse before a request
  exists, and nothing about a filament path leaves the wire without ``confirm=True`` — the preview
  sends only 1002, or the 1002-plus-2005 pair the status tool already issues;
* the gates are exactly the vendor control's own states (idle, or a stopped print, for the toolhead
  pair; idle, homing, paused or filament-interruption for a tray), with no temperature, purge or
  bed condition invented, and the tray half reads the printer's own reply: an unaddressable pair, a
  unit that does not report itself connected, and (for a load) an empty or unrecognised tray refuse;
* the acceptance contract is the job controls' word for word: only an explicit integer 0 is
  acceptance, a refusal keeps its code and earns no follow-up read, a lost or unreadable answer is
  ambiguous, the one allowed follow-up is a single read-only look (1002 for the toolhead, the 2005
  Elegoo's own control refreshes for a tray), and a dead session sends nothing further;
* the candidate posture: no tool, no listing, no README or manifest mention, every reply saying
  ``verified_on_this_printer`` false, and every authored string inside the direct tools' reply cap.
"""
from __future__ import annotations

import copy
import inspect
import json
from collections import deque
from pathlib import Path

import pytest

from orcaslicer_mcp.printer import cc2_control as job
from orcaslicer_mcp.printer import cc2_filament as fil
from orcaslicer_mcp.printer.cc2_transport import Cc2TimeoutError, PrinterError

ROOT = Path(__file__).resolve().parents[1]
HOST = "192.0.2.10"                  # documented TEST-NET; never a live address
SN = "CC2TESTSN0001"
FAKE_TOKEN = "SIMULATED-NOT-A-CODE"  # a stand-in credential, proven never to ride any reply
JOB = "benchy-petg-2026-11-04.gcode"

ALL_METHODS = (1024, 1025, 2001, 2002)


def machine(status=1, sub=0, *, printing=False, nozzle=24, target=0) -> dict:
    """A 1002 reply in the observed shape, parked in whichever machine state a test is about."""
    ms = {"exception_status": [], "progress": 21 if printing else 0, "status": status,
          "sub_status": sub, "sub_status_reason_code": 0}
    if printing:
        ps = {"current_layer": 51, "filename": JOB, "state": "printing", "uuid": "80baf1df"}
    else:
        ps = {"current_layer": 0, "filename": "", "state": "", "uuid": ""}
    return {"error_code": 0, "machine_status": ms, "print_status": ps,
            "extruder": {"target": target, "temperature": nozzle},
            "heater_bed": {"target": 0, "temperature": 16},
            "tool_head": {"homed_axes": "xyz"}}


def canvas(*, trays=None, connected=1, active_tray=3, active_canvas=0) -> dict:
    """A 2005 reply shaped like the protocol doc's real-world CANVAS sample. Tray ids are the
    printer's own and every test that needs an unusual tray asks for one explicitly."""
    if trays is None:
        trays = [(0, "#2850DF", "PLA", 1), (1, "#FFFFFF", "PLA Basic", 1),
                 (2, "#F32FF8", "PLA Silk", 1), (3, "#000000", "PLA", 2)]
    units = [{"canvas_id": active_canvas, "connected": connected,
              "tray_list": [{"tray_id": tid, "brand": "ELEGOO", "filament_type": "PLA",
                             "filament_name": name, "filament_color": color,
                             "min_nozzle_temp": 190, "max_nozzle_temp": 230, "status": state}
                            for tid, color, name, state in trays]}]
    return {"error_code": 0, "canvas_info": {"active_canvas_id": active_canvas,
                                             "active_tray_id": active_tray, "auto_refill": False,
                                             "canvas_list": units}}


class FakeFilamentTransport:
    """Stands in for the registered session as Cc2Transport behaves: records every request with the
    timeout it was given and answers from a method -> script map (a deque shifts, the last value
    sticks; an exception raises; an unscripted method answers {}). The module under test is the only
    thing that may decide to ask twice, and these tests count the frames."""

    def __init__(self, replies=None, *, host=HOST, sn=SN):
        self.host, self.sn = host, sn
        self.replies = dict(replies or {})
        self.requests: list[tuple[int, dict, float | None]] = []

    async def request(self, method: int, params: dict | None = None, *, timeout_s=None) -> dict:
        self.requests.append((method, copy.deepcopy(params or {}), timeout_s))
        reply = self.replies.get(method, {})
        if isinstance(reply, deque):
            reply = reply.popleft() if len(reply) > 1 else reply[0]
        if isinstance(reply, BaseException):
            raise reply
        return reply if isinstance(reply, dict) else {"error_code": reply}

    def methods(self) -> list[int]:
        return [m for m, _p, _t in self.requests]

    def commands(self) -> list[tuple[int, dict, float | None]]:
        return [r for r in self.requests if r[0] in ALL_METHODS]


def transport(*, status=None, ams=None, command=None, verb="toolhead_load") -> FakeFilamentTransport:
    """A transport whose 1002 and 2005 answer the frames above and whose filament methods answer
    ``error_code`` 0 unless a test scripts something else."""
    replies: dict = {1002: status if status is not None else machine(),
                     2005: ams if ams is not None else canvas(),
                     **{m: command if command is not None else {"error_code": 0}
                        for m in ALL_METHODS}}
    assert verb in fil._METHODS
    return FakeFilamentTransport(replies)


OK = {"error_code": 0}
TOOLHEAD = ("toolhead_load", "toolhead_retreat")
TRAYS = ("canvas_load", "canvas_unload")


def ids_for(verb: str) -> dict:
    """A coherent target for the direction under test: the default CANVAS keeps tray 2 present and
    tray 3 in the path (raw 2), which is exactly the distinction the module's load-only rules turn
    on. A toolhead verb takes no ids at all."""
    if verb not in TRAYS:
        return {}
    return {"canvas_id": 0, "tray_id": 3 if verb == "canvas_unload" else 2}


# --------------------------------------------------------------------------- the four wire frames


@pytest.mark.parametrize("verb,method,params", [
    ("toolhead_load", 1024, {}), ("toolhead_retreat", 1025, {}),
    ("canvas_load", 2001, {"canvas_id": 0, "tray_id": 2}),
    ("canvas_unload", 2002, {"canvas_id": 0, "tray_id": 3})])
async def test_the_frame_and_the_300_second_window_are_the_lan_controls_own(verb, method, params):
    t = transport()
    out = await fil.run_filament(t, verb, canvas_id=params.get("canvas_id"),
                                 tray_id=params.get("tray_id"), confirm=True)
    assert t.commands() == [(method, params, 300.0)], "one frame, that frame, waited 300 s"
    assert out.sent == params and out.method == method and out.waited_s == 300.0


async def test_the_tray_pair_is_always_the_printers_own_never_a_hardcoded_zero():
    """A printer whose CANVAS is unit 2 tray 1 is commanded 2 and 1 — and a pair it never named is
    never sent at all."""
    ams = canvas(active_canvas=2, trays=[(1, "#FF0000", "PETG", 1), (5, "#00FF00", "PETG", 2)])
    t = transport(ams=ams)
    out = await fil.run_filament(t, "canvas_load", canvas_id=2, tray_id=1, confirm=True)
    assert t.commands() == [(2001, {"canvas_id": 2, "tray_id": 1}, 300.0)]
    assert out.accepted is True

    t2 = transport(ams=ams)
    with pytest.raises(PrinterError) as exc:
        await fil.run_filament(t2, "canvas_load", canvas_id=0, tray_id=3, confirm=True)
    assert "2/1" in exc.value.details["printer_says"]      # what the printer actually names
    assert t2.commands() == []


@pytest.mark.parametrize("verb", TOOLHEAD)
async def test_the_toolhead_commands_carry_nothing_at_all(verb):
    """No source sends a length, a speed or a temperature with 1024/1025, so an id handed to one of
    them is a caller misunderstanding which command it asked for, and it refuses."""
    t = transport()
    await fil.run_filament(t, verb, confirm=True)
    assert t.commands() == [(fil._METHODS[verb], {}, 300.0)]
    with pytest.raises(PrinterError):
        await fil.run_filament(t, verb, canvas_id=0, tray_id=1, confirm=True)
    assert t.commands() == [(fil._METHODS[verb], {}, 300.0)], "the refusal sent nothing new"


@pytest.mark.parametrize("params", [{"length": 100}, {"temperature": 200}, {"tray_id": 1},
                                    {"canvas_id": 0, "tray_id": 1, "filament_color": "#FF0000"},
                                    {"canvas_id": 0}, {"canvas_id": "0", "tray_id": 1},
                                    {"canvas_id": True, "tray_id": 1}, {"canvas_id": 0, "tray_id": -1},
                                    "0/1", None, [0, 1]])
def test_the_frame_lock_refuses_anything_that_is_not_the_documented_frame(params):
    with pytest.raises((PrinterError, ValueError)):
        fil.checked_params("canvas_load", params)
    with pytest.raises((PrinterError, ValueError)):
        fil.checked_params("toolhead_load", params)
    assert fil.checked_params("toolhead_load", {}) == {}
    assert fil.checked_params("canvas_load", {"canvas_id": 2, "tray_id": 1}) == {"canvas_id": 2,
                                                                                 "tray_id": 1}


@pytest.mark.parametrize("verb", TOOLHEAD + TRAYS)
async def test_without_confirm_no_filament_method_leaves_and_the_call_says_so(verb):
    t = transport()
    ids = ids_for(verb)
    with pytest.raises(PrinterError) as exc:
        await fil.run_filament(t, verb, **ids)
    assert exc.value.details["reason"] == "filament_unconfirmed"
    assert t.commands() == [] and t.methods() == []


@pytest.mark.parametrize("verb,method", [("toolhead_load", 1002), ("canvas_load", 2005)])
async def test_a_preview_reads_and_sends_no_command(verb, method):
    t = transport()
    ids = ids_for(verb)
    out = await fil.read_preview(t, verb, **ids)
    assert t.commands() == []
    assert (method in t.methods()) and out["confirm_required"] is True
    assert out["preview"]["will_send"]["method"] == fil._METHODS[verb]
    assert out["preview"]["waits_up_to_s"] == 300.0
    assert out["preview"]["verified_on_this_printer"] is False


async def test_a_preview_refuses_a_bad_pair_without_reading_the_printer():
    t = transport()
    with pytest.raises((PrinterError, ValueError)):
        await fil.read_preview(t, "canvas_load", canvas_id="three", tray_id=3)
    assert t.methods() == []


async def test_a_preview_whose_read_fails_is_raised_not_guessed():
    t = FakeFilamentTransport({1002: PrinterError("not_reachable", "no session"),
                               2005: canvas()})
    with pytest.raises(PrinterError):
        await fil.read_preview(t, "toolhead_load")


# --------------------------------------------------------------------------- the vendor's own gates


@pytest.mark.parametrize("status,sub,printing,allowed", [
    (1, 0, False, True),                                     # idle
    (2, 2504, True, True),                                   # printing, but stopped
    (2, 2075, True, False),                                  # printing
    (3, 1152, False, False),                                 # mid canvas load
    (14, 0, False, False)])                                  # emergency stop
async def test_the_toolhead_gate_is_idle_or_a_stopped_print_and_nothing_else(status, sub, printing,
                                                                             allowed):
    t = transport(status=machine(status, sub, printing=printing))
    if allowed:
        assert (await fil.run_filament(t, "toolhead_load", confirm=True)).accepted
    else:
        with pytest.raises(PrinterError) as exc:
            await fil.run_filament(t, "toolhead_retreat", confirm=True)
        assert exc.value.details["reason"] == "filament_gate"
    assert t.methods() == ([1002, 1024, 1002] if allowed else [1002]), \
        "a refusal reads once and never commands; an accepted run adds its one look"
    assert t.commands() == ([] if not allowed else [(1024, {}, 300.0)])


@pytest.mark.parametrize("status,sub,printing,allowed", [
    (1, 0, False, True), (10, 0, False, True),               # idle, homing
    (2, 2502, True, True), (2, 2505, True, True),            # paused, filament interruption
    (2, 2075, True, False), (2, 2504, True, False), (0, 0, False, False)])
async def test_the_tray_gate_is_the_four_states_the_panel_offers_them_in(status, sub, printing,
                                                                         allowed):
    t = transport(status=machine(status, sub, printing=printing))
    if allowed:
        assert (await fil.run_filament(t, "canvas_load", canvas_id=0, tray_id=2,
                                       confirm=True)).accepted
    else:
        with pytest.raises(PrinterError):
            await fil.run_filament(t, "canvas_load", canvas_id=0, tray_id=2, confirm=True)
    assert t.commands() == ([] if not allowed else [(2001, {"canvas_id": 0, "tray_id": 2}, 300.0)])


@pytest.mark.parametrize("verb", TRAYS)
async def test_a_disconnected_unit_refuses_its_tray(verb):
    t = transport(ams=canvas(connected=0))
    with pytest.raises(PrinterError) as exc:
        await fil.run_filament(t, verb, canvas_id=0, tray_id=3, confirm=True)
    assert "connected" in exc.value.message
    assert t.commands() == []


async def test_an_empty_or_unrecognised_tray_is_not_offered_a_load_but_an_unload_still_works():
    """The panel greys both buttons for those trays; the empty case is load-only here on purpose,
    because pulling a ran-out spool back out is the interruption state's whole point."""
    empty = canvas(trays=[(0, "#2850DF", "PLA", 1), (1, "", "", 0)])
    t = transport(ams=empty)
    with pytest.raises(PrinterError) as exc:
        await fil.run_filament(t, "canvas_load", canvas_id=0, tray_id=1, confirm=True)
    assert "empty" in exc.value.message
    assert t.commands() == []
    assert (await fil.run_filament(t, "canvas_unload", canvas_id=0, tray_id=1,
                                   confirm=True)).accepted
    assert t.commands() == [(2002, {"canvas_id": 0, "tray_id": 1}, 300.0)]

    t2 = transport(ams=canvas(trays=[(2, "", "", 1)]))
    with pytest.raises(PrinterError) as exc2:
        await fil.run_filament(t2, "canvas_load", canvas_id=0, tray_id=2, confirm=True)
    assert "unrecognised" in exc2.value.message
    assert t2.commands() == []


async def test_an_unreadable_tray_state_refuses_both_directions_with_its_number():
    t = transport(ams=canvas(trays=[(0, "#2850DF", "PLA", 7)]))
    for verb in TRAYS:
        with pytest.raises(PrinterError) as exc:
            await fil.run_filament(t, verb, canvas_id=0, tray_id=0, confirm=True)
        assert "7" in exc.value.details["printer_says"]
    assert t.commands() == []


async def test_a_gate_read_that_fails_refuses_rather_than_commanding_blind():
    t = FakeFilamentTransport({1002: PrinterError("not_reachable", "session closed"), 2005: canvas()})
    with pytest.raises(PrinterError) as exc:
        await fil.run_filament(t, "canvas_load", canvas_id=0, tray_id=0, confirm=True)
    assert exc.value.details["reason"] == "filament_gate_read"
    assert t.commands() == []


async def test_a_preview_shows_the_reason_a_confirmed_call_would_be_refused():
    t = transport(status=machine(2, 2075, printing=True))
    out = await fil.read_preview(t, "toolhead_load")
    assert "idle" in out["preview"]["would_refuse"]
    assert out["preview"]["nozzle_temp"] == 24 and t.commands() == []


async def test_no_temperature_condition_is_invented_and_the_printers_number_is_shown():
    """The vendor control offers these cold, so a hot nozzle is neither required nor claimed: the
    preview shows the printer's own numbers and says the rule is the caller's to read."""
    t = transport(status=machine(1, 0, nozzle=205, target=200))
    out = await fil.read_preview(t, "toolhead_load")
    assert out["preview"]["nozzle_temp"] == 205 and out["preview"]["nozzle_target"] == 200
    assert "No source states a temperature condition" in out["preview"]["no_temperature_condition"]
    assert out["preview"]["would_refuse"] is None


# --------------------------------------------------------------------------- acceptance contract


@pytest.mark.parametrize("reply,expect,code", [
    (OK, "accepted", None), ({"error_code": 1010}, "refused", 1010),
    ({"error_code": True}, "ambiguous", None), ({"error_code": "0"}, "ambiguous", None),
    ({"error_code": 0.0}, "ambiguous", None), ({}, "ambiguous", None)])
async def test_only_an_explicit_integer_zero_is_acceptance(reply, expect, code):
    t = transport(command=reply)
    out = await fil.run_filament(t, "toolhead_load", confirm=True)
    assert str(out.status) == expect and out.error_code == code
    assert t.methods().count(1024) == 1


@pytest.mark.parametrize("verb", TOOLHEAD + TRAYS)
async def test_a_refusal_keeps_the_printers_code_and_asks_nothing_further(verb):
    t = transport(command={"error_code": 5001})
    ids = ids_for(verb)
    out = await fil.run_filament(t, verb, confirm=True, **ids)
    assert str(out.status) == "refused" and out.error_code == 5001
    assert t.methods() == [1002] + ([2005] if verb in TRAYS else []) + [fil._METHODS[verb]]


@pytest.mark.parametrize("verb", TOOLHEAD + TRAYS)
async def test_a_lost_answer_is_ambiguous_never_a_refusal_and_earns_one_read(verb):
    t = transport()
    ids = ids_for(verb)
    t.replies[fil._METHODS[verb]] = Cc2TimeoutError("protocol_error", "no answer", reason="timeout",
                                                    ambiguous=True)
    out = await fil.run_filament(t, verb, confirm=True, **ids)
    assert str(out.status) == "ambiguous"
    assert any("300" in n for n in out.notes) and any("mid-operation" in n for n in out.notes)
    assert t.methods().count(fil._METHODS[verb]) == 1, "an expired window never earns a second one"
    reads = [m for m in t.methods() if m in (1002, 2005)]
    assert reads == ([1002, 2005, 2005] if verb in TRAYS else [1002, 1002]), \
        "one gate read (a tray's is the 1002+2005 pair cc2_status already issues), then one look"


async def test_a_lost_answer_with_a_dead_read_says_so_without_resending():
    t = transport()
    t.replies[1025] = Cc2TimeoutError("protocol_error", "no answer", reason="timeout",
                                     ambiguous=True)
    t.replies[1002] = deque([machine(), PrinterError("not_reachable", "the session ended")])
    out = await fil.run_filament(t, "toolhead_retreat", confirm=True)
    assert str(out.status) == "ambiguous" and out.observed is None
    assert any("produced no reading" in n for n in out.notes)


async def test_a_dead_session_sends_nothing_further_at_all():
    t = transport()
    t.replies[1002] = deque([machine(), PrinterError("not_reachable", "the session ended")])
    t.replies[1024] = PrinterError("not_reachable", "the session ended")
    out = await fil.run_filament(t, "toolhead_load", confirm=True)
    assert str(out.status) == "ambiguous" and t.methods() == [1002, 1024]
    assert any("nothing was resent" in n.lower() or "session ended" in n for n in out.notes)


async def test_an_accepted_toolhead_command_never_claims_filament_moved_and_reports_one_look():
    t = transport()
    t.replies[1002] = deque([machine(), machine(2, 1063, nozzle=200, target=0)])
    out = await fil.run_filament(t, "toolhead_load", confirm=True)
    assert out.accepted and out.verified_on_this_printer is False
    assert out.observed["sub_status"] == 1063
    assert out.observed["sub_status_word"] == "extruder load complete"
    assert t.methods() == [1002, 1024, 1002], "one gate read, one command, one look"


async def test_an_accepted_tray_command_refreshes_the_2005_its_own_control_refreshes_and_nothing_more():
    t = transport()
    t.replies[2005] = deque([canvas(), canvas(active_tray=2)])
    out = await fil.run_filament(t, "canvas_load", canvas_id=0, tray_id=2, confirm=True)
    assert out.accepted and out.verified_on_this_printer is False
    assert out.observed["observed_active_tray_id"] == 2
    assert out.observed["observed_tray_states"]["0/2"] == "present"
    assert t.methods() == [1002, 2005, 2001, 2005], "the pair, the command, then one 2005"


async def test_a_tray_command_reports_what_the_printers_enum_names_that_sub_status():
    assert fil.sub_status_word(1061) == "extruder loading"
    assert fil.sub_status_word(1152) == "canvas load waiting for manual insert"
    assert fil.sub_status_word(2075) is None
    t = transport(status=machine(2, 1061, printing=True, nozzle=210, target=210))
    out = await fil.read_preview(t, "toolhead_load")
    assert out["preview"]["sub_status_word"] == "extruder loading"
    assert out["preview"]["driving_a_filament_operation"] is True


async def test_the_tray_the_printer_calls_loaded_may_be_unloaded_and_never_loaded_twice():
    """The vendor's own rule, enforced in the one direction it is a safety rule in: the tray reporting
    status 2 is in the path, so it may come out and may not be pushed into again. The other direction
    stays the printer's call, because after a run-out the tray worth removing is the odd one out."""
    t = transport()
    with pytest.raises(PrinterError) as exc:
        await fil.run_filament(t, "canvas_load", canvas_id=0, tray_id=3, confirm=True)
    assert "already in its path" in exc.value.message
    assert t.commands() == []
    assert (await fil.run_filament(t, "canvas_unload", canvas_id=0, tray_id=3,
                                   confirm=True)).accepted
    assert t.commands() == [(2002, {"canvas_id": 0, "tray_id": 3}, 300.0)]
    assert (await fil.run_filament(t, "canvas_load", canvas_id=0, tray_id=2,
                                   confirm=True)).accepted


async def test_the_vendor_loaded_tray_reading_is_the_printers_own_first_status_two_tray():
    """Elegoo's rule is its own ``loaded`` state (raw 2), so the reading it reports is the printer's,
    not this module's guess. It is reported and not gated on: copying it as a rule would refuse the
    unload of a spool that ran out, which is the interruption flow's whole point."""
    from orcaslicer_mcp.printer import cc2_state as st
    status = st.Cc2CanvasStatus.from_result(canvas(active_tray=3))
    assert fil.vendor_loaded_tray(status).tray_id == 3      # the first tray reporting status 2
    ran_out = st.Cc2CanvasStatus.from_result(
        canvas(trays=[(0, "#2850DF", "PLA", 0), (1, "#FFFFFF", "PLA", 2)], active_tray=0))
    assert fil.vendor_loaded_tray(ran_out).tray_id == 1     # the printer's own order, its own number
    t = transport(ams=canvas(active_tray=3))
    out = await fil.read_preview(t, "canvas_unload", canvas_id=0, tray_id=3)
    assert out["preview"]["vendor_loaded_tray_id"] == 3
    assert out["preview"]["observed_active_tray_id"] == 3
    assert out["preview"]["would_refuse"] is None


# --------------------------------------------------------------------------- the candidate posture


def test_no_filament_tool_is_registered():
    import orcaslicer_mcp.server as srv

    names = set(srv.mcp._tool_manager._tools)
    invented = ("cc2_filament", "cc2_load_filament", "cc2_unload_filament", "cc2_load",
                "cc2_unload", "cc2_feed", "cc2_retreat", "cc2_canvas_load", "cc2_canvas_unload")
    assert not [n for n in names if n in invented]
    assert not [n for n in srv.CC2_TOOL_NAMES if n in invented]
    assert not [n for n in srv._TOOL_ANNOTATIONS if n in invented]


@pytest.mark.parametrize("relative", ["src/orcaslicer_mcp/server.py", "README.md",
                                      "lhm.plugin.json", "mcpb/manifest.json",
                                      "scripts/gen_lhm_tools.py"])
def test_the_candidate_is_not_offered_as_a_supported_capability(relative):
    text = (ROOT / relative).read_text(encoding="utf-8")
    assert "cc2_filament" not in text
    assert "cc2_load_filament" not in text and "cc2_unload_filament" not in text


def test_the_module_is_importable_but_declares_no_tool():
    source = Path(inspect.getsourcefile(fil)).read_text(encoding="utf-8")
    assert "import server" not in source and "@mcp.tool" not in source


async def test_every_reply_says_the_candidate_is_unproven_and_carries_no_secret():
    for verb in TOOLHEAD + TRAYS:
        for command in (OK, {"error_code": 5001}, {}):
            t = transport(command=command)
            t.replies[1002] = deque([machine(), machine()])
            t.replies[2005] = deque([canvas(), canvas()])
            ids = ids_for(verb)
            out = (await fil.run_filament(t, verb, confirm=True, **ids)).as_dict()
            assert out["verified_on_this_printer"] is False
            assert "1024, 1025, 2001 or 2002" in out["unverified"]
            blob = json.dumps(out, default=str) + " ".join(out["notes"])
            for secret in (FAKE_TOKEN, SN, HOST, "password", "access_code"):
                assert secret not in blob, f"{secret!r} rode a reply"


def test_no_authored_string_outgrows_the_direct_tools_reply_cap():
    long = [n for n, v in vars(fil).items()
            if isinstance(v, str) and not n.startswith("__") and len(v) > job.NOTE_CAP]
    long += [f"{name}[{key}]" for name, value in vars(fil).items()
             if isinstance(value, dict) and not name.startswith("__")
             for key, v in value.items() if isinstance(v, str) and len(v) > job.NOTE_CAP]
    assert long == [], f"these strings would be truncated in a reply: {long}"
