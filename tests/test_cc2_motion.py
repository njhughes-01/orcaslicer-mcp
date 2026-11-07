"""CC2 manual homing (1026) and single-axis movement (1027) against scripted replies: nothing opens
a socket, the fake transport records every method with the timeout it was given, and the frames are
the observed idle 1002 of the state tests with the position/homing blocks the protocol doc and
Elegoo's own LAN control both describe. What these tests hold the module to:

* the wire frames are the vendor client's own, lowercased, one axis per move, waited for its own
  windows (50 s home, 25 s move), and its button table (Z inverted by its own client) is transcribed
  rather than interpreted;
* no direction word is ever a wire parameter, and no distance outside the vendor control's own
  0.1-30 mm envelope, no unsupported axis and no unknown spelling (``"xy"``, ``"e"``, ``"x,y"``, a
  list, a bool, a zero, a nan) ever reaches the wire;
* nothing moves without ``confirm=True``, and no move at all happens without the caller's exact
  ``bed_clear=True`` — those refusions send not even the read-only 1002;
* the two vendor gates (idle-only motion, home-before-move) refuse before the motion method is sent,
  and a homing report that cannot be read refuses nothing;
* the acceptance contract is the job controls' word for word: only an explicit integer 0 is
  acceptance, a refusal keeps its code and earns no follow-up read, a lost or unreadable answer is
  ambiguous, the one allowed follow-up is a single read-only 1002, and a dead session sends nothing
  further;
* the candidate posture: no tool, no listing, no README or manifest mention, and every authored
  string inside the direct tools' reply cap, with ``verified_on_this_printer`` false everywhere.
"""
from __future__ import annotations

import copy
import inspect
import json
from collections import deque
from pathlib import Path

import pytest

from orcaslicer_mcp.printer import cc2_control as job
from orcaslicer_mcp.printer import cc2_motion as mo
from orcaslicer_mcp.printer.cc2_transport import Cc2TimeoutError, PrinterError

ROOT = Path(__file__).resolve().parents[1]
HOST = "192.0.2.10"                  # documented TEST-NET; never a live address
SN = "CC2TESTSN0001"
FAKE_TOKEN = "SIMULATED-NOT-A-CODE"  # a stand-in credential, proven never to ride any reply
JOB = "benchy-petg-2026-11-04.gcode"


def frame(*, homed_axes="xyz", homed_key="tool_head", position=(128.0, 128.0, 5.0),
          printing=False) -> dict:
    """The observed idle 1002 (machine 1/0, the printer's idle word "") with the two blocks this
    module reads: the position block (the doc spells it ``gcode_move_inf``, its mapping table and the
    LAN client spell the homing block ``tool_head`` while its example says ``toolhead``) and the
    homing report. ``homed_axes=None`` leaves the homing block out entirely, and ``printing=True``
    is the observed live shape (machine 2, a filename, state "printing")."""
    if printing:
        ms = {"exception_status": [], "progress": 21, "status": 2, "sub_status": 2075,
              "sub_status_reason_code": 0}
        ps = {"current_layer": 51, "filename": JOB, "state": "printing", "uuid": "80baf1df"}
    else:
        ms = {"exception_status": [], "progress": 0, "status": 1, "sub_status": 0,
              "sub_status_reason_code": 0}
        ps = {"bed_mesh_detect": True, "current_layer": 0, "enable": True, "filament_detect": False,
              "filename": "", "print_duration": 0, "remaining_time_sec": 0, "state": "",
              "total_duration": 0, "uuid": ""}
    reply = {"error_code": 0, "machine_status": ms, "print_status": ps,
             "extruder": {"target": 0, "temperature": 21},
             "heater_bed": {"target": 0, "temperature": 16}}
    if position is not None:
        reply["gcode_move_inf"] = {"x": position[0], "y": position[1], "z": position[2], "e": 0,
                                   "speed": 0, "speed_mode": 1}
    if homed_axes is not None:
        reply[homed_key] = {"homed_axes": homed_axes}
    return reply


class FakeMotionTransport:
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

    def motion(self) -> list[tuple[int, dict, float | None]]:
        return [r for r in self.requests if r[0] in (mo.METHOD_HOME_AXES, mo.METHOD_MOVE_AXES)]


def transport(**frames) -> FakeMotionTransport:
    """A transport whose 1002 answers a given frame and whose motion methods answer error_code 0."""
    status = frames.pop("status", None)
    motion = frames.pop("motion", {"error_code": 0})
    replies: dict = {1002: status if status is not None else frame(),
                     mo.METHOD_HOME_AXES: motion, mo.METHOD_MOVE_AXES: motion}
    assert not frames, f"unknown frames: {sorted(frames)}"
    return FakeMotionTransport(replies)


OK = {"error_code": 0}


def lost() -> Cc2TimeoutError:
    return Cc2TimeoutError("protocol_error", "simulated: the printer never answered",
                           reason="timeout", ambiguous=True)

# --------------------------------------------------------------------------- the wire frames


@pytest.mark.parametrize("axes,wire", [("xyz", "xyz"), ("XYZ", "xyz"), (" X ", "x"), ("y", "y"),
                                       ("Z", "z")])
async def test_home_sends_the_vendors_own_frame_once_and_waits_its_fifty_seconds(axes, wire):
    t = transport()
    result = await mo.run_motion(t, "home", axes=axes, confirm=True)
    assert t.motion() == [(mo.METHOD_HOME_AXES, {"homed_axes": wire}, 50.0)]
    assert result.accepted and str(result.status) == "accepted"
    # one gate read before, one courtesy read after, and nothing else
    assert t.methods() == [1002, mo.METHOD_HOME_AXES, 1002]


@pytest.mark.parametrize("axis,distance", [("z", -10.0), ("Z", 10.0), ("x", 0.1), ("y", -30.0),
                                           ("x", "2.5")])
async def test_move_sends_one_axis_and_the_signed_distance_and_waits_its_twenty_five(axis,
                                                                                      distance):
    t = transport()
    result = await mo.run_motion(t, "move", axes=axis, distance=distance, confirm=True,
                                 bed_clear=True)
    assert t.motion() == [(mo.METHOD_MOVE_AXES, {"axes": axis.lower(), "distance": float(distance)},
                           25.0)]
    assert result.sent == {"axes": axis.lower(), "distance": float(distance)}


def test_the_button_table_is_the_client_s_own_arrows_with_z_inverted():
    # read off the control: its arrows multiply the step by +1/-1 and its client negates that for Z
    assert mo.vendor_button_distance("x", "up", 10.0) == 10.0
    assert mo.vendor_button_distance("x", "down", 10.0) == -10.0
    assert mo.vendor_button_distance("y", "up", 1.0) == 1.0
    assert mo.vendor_button_distance("z", "up", 10.0) == -10.0
    assert mo.vendor_button_distance("Z", "Down", 0.1) == 0.1
    assert mo.vendor_button_distance("z", "down", 30) == 30.0


async def test_nothing_on_the_wire_is_named_up_down_or_raise():
    t = transport()
    await mo.run_motion(t, "move", axes="z", distance=-0.1, confirm=True, bed_clear=True)
    assert sorted(t.motion()[0][1]) == ["axes", "distance"]
    assert mo.move_params("z", 1) == {"axes": "z", "distance": 1.0}


# --------------------------------------------------------------------------- refused arguments


@pytest.mark.parametrize("bad", ["", "xy", "e", "x,y", "xx", "xy z", "extruder", None, 5,
                                 ["x"], True])
async def test_an_unsupported_homing_set_refuses_before_any_request(bad):
    t = transport()
    with pytest.raises(PrinterError) as e:
        await mo.run_motion(t, "home", axes=bad, confirm=True)
    assert e.value.details["reason"] == "home_axes"
    assert "Nothing was sent." in e.value.message
    assert t.requests == []                     # not even the read-only status


@pytest.mark.parametrize("bad", ["", "xy", "xyz", "e", 1, None, True, ["z"]])
async def test_a_move_that_is_not_one_axis_refuses_before_any_request(bad):
    t = transport()
    with pytest.raises(PrinterError) as e:
        await mo.run_motion(t, "move", axes=bad, distance=10, confirm=True, bed_clear=True)
    assert e.value.details["reason"] == "move_axis"
    assert t.requests == []


@pytest.mark.parametrize("bad", [0, 0.0, "0", 0.05, 30.0001, 100, -1000, True, False, None, "abc",
                                 "", float("nan"), float("inf"), ["10"]])
async def test_a_distance_outside_the_vendors_own_envelope_refuses_before_any_request(bad):
    t = transport()
    with pytest.raises(PrinterError) as e:
        await mo.run_motion(t, "move", axes="z", distance=bad, confirm=True, bed_clear=True)
    assert e.value.details["reason"] in ("move_distance", "move_distance_range")
    assert t.requests == []


async def test_a_direction_word_is_never_accepted_where_a_distance_belongs():
    t = transport()
    with pytest.raises(PrinterError) as e:
        await mo.run_motion(t, "move", axes="z", distance="up", confirm=True, bed_clear=True)
    assert e.value.details["reason"] == "move_distance"     # "up" is a button, not a wire number
    assert t.requests == []
    with pytest.raises(PrinterError) as bad_dir:
        mo.vendor_button_distance("z", "left", 10)
    assert bad_dir.value.details["reason"] == "move_direction"


@pytest.mark.parametrize("frame_lock", [{"homed_axes": "xyz", "speed": 10},
                                        {"homed_axes": ["x", "y", "z"]}, {}, "xyz"])
def test_the_frame_lock_refuses_a_hand_rolled_home(frame_lock):
    with pytest.raises((PrinterError, ValueError)):
        mo.checked_params("home", frame_lock)


@pytest.mark.parametrize("frame_lock", [{"axes": "z", "distance": 10, "speed": 5},
                                        {"axes": "xyz", "distance": 10},
                                        {"axes": "z", "distance": 999}, {"distance": 10}])
def test_the_frame_lock_refuses_a_hand_rolled_move(frame_lock):
    with pytest.raises((PrinterError, ValueError)):
        mo.checked_params("move", frame_lock)


async def test_an_unknown_kind_is_a_programmer_error_not_a_wire_method():
    t = transport()
    with pytest.raises(ValueError):
        await mo.run_motion(t, "home_all", axes="xyz", confirm=True)
    assert t.requests == []


# --------------------------------------------------------------------------- the two locks that
# --------------------------------------------------------------------------- precede every request


@pytest.mark.parametrize("bed_clear", [False, None, "yes", 1, 0])
async def test_a_move_without_the_bed_assertion_sends_absolutely_nothing(bed_clear):
    t = transport()
    with pytest.raises(PrinterError) as e:
        await mo.run_motion(t, "move", axes="z", distance=-10, confirm=True, bed_clear=bed_clear)
    assert e.value.details["reason"] == "bed_not_clear"
    assert "Nothing was sent." in e.value.message
    assert t.requests == []          # not even the state read: nothing about the printer was needed


@pytest.mark.parametrize("axes,distance,kind", [("xyz", None, "home"), ("z", -10, "move")])
async def test_without_confirm_no_motion_method_leaves_and_the_call_says_so(axes, distance, kind):
    t = transport()
    with pytest.raises(PrinterError) as e:
        await mo.run_motion(t, kind, axes=axes, distance=distance, bed_clear=True)
    assert e.value.details["reason"] == "motion_unconfirmed"
    assert "confirm=True" in e.value.message
    assert t.motion() == []
    assert t.requests == []          # the preview is the read-only call, not this one


# --------------------------------------------------------------------------- the vendor's own gates


@pytest.mark.parametrize("kind,axes,distance", [("home", "xyz", None), ("move", "z", -10)])
async def test_a_printer_that_is_not_idle_refuses_motion_before_it_is_sent(kind, axes, distance):
    # the control's own rule: xyzDisabled = computed(() => !isIdle)
    t = transport(status=frame(printing=True, homed_axes="xyz"))
    with pytest.raises(PrinterError) as e:
        await mo.run_motion(t, kind, axes=axes, distance=distance, confirm=True, bed_clear=True)
    assert e.value.details["reason"] == "motion_gate"
    assert JOB in e.value.details["printer_says"]       # the printer's own words, whole
    assert e.value.message.endswith(". Nothing was sent.")
    assert len(e.value.message) <= job.NOTE_CAP
    assert t.motion() == []
    assert t.methods() == [1002]


async def test_the_vendors_home_before_move_gate_refuses_a_move_it_could_have_sent():
    t = transport(status=frame(homed_axes=""))
    with pytest.raises(PrinterError) as e:
        await mo.run_motion(t, "move", axes="z", distance=-10, confirm=True, bed_clear=True)
    assert e.value.details["reason"] == "motion_gate"
    assert "home" in e.value.hint.lower()
    assert t.motion() == []


async def test_homing_is_still_offered_when_the_axes_are_not_homed():
    # 1026 is the fix for the gate above, so it must never be gated by it
    t = transport(status=frame(homed_axes=""))
    result = await mo.run_motion(t, "home", axes="xyz", confirm=True)
    assert result.accepted and t.motion()[0][1] == {"homed_axes": "xyz"}


@pytest.mark.parametrize("key", ["tool_head", "toolhead"])
async def test_both_spellings_of_the_homing_report_are_read(key):
    t = transport(status=frame(homed_key=key, homed_axes=""))
    with pytest.raises(PrinterError):
        await mo.run_motion(t, "move", axes="z", distance=10, confirm=True, bed_clear=True)
    assert t.motion() == []


async def test_a_homing_report_that_cannot_be_read_refuses_nothing():
    # the doc's example and the doc's own mapping table disagree on the key name; guessing "" here
    # would block every honest move on a naming difference
    t = transport(status=frame(homed_axes=None))
    result = await mo.run_motion(t, "move", axes="z", distance=10, confirm=True, bed_clear=True)
    assert result.accepted
    assert result.observed["observed_homed_axes"] is None


async def test_a_state_read_that_fails_refuses_rather_than_moving_blind():
    t = FakeMotionTransport({1002: PrinterError("not_reachable", "no answer to 1002")})
    with pytest.raises(PrinterError) as e:
        await mo.run_motion(t, "move", axes="z", distance=10, confirm=True, bed_clear=True)
    assert e.value.details["reason"] == "motion_gate_read"
    assert "Nothing was sent." in e.value.message
    assert t.motion() == []


# --------------------------------------------------------------------------- the preview


@pytest.mark.parametrize("kind,axes,distance,bed", [("home", "xyz", None, False),
                                                    ("move", "z", -10.0, True)])
async def test_the_preview_shows_the_exact_frame_and_never_sends_it(kind, axes, distance, bed):
    t = transport()
    out = await mo.read_preview(t, kind, axes=axes, distance=distance)
    assert t.motion() == []
    assert t.methods() == [1002]                       # the one read-only method this path knows
    assert out["confirm_required"] is True and out["action"] == kind
    want = {"home": {"homed_axes": "xyz"}, "move": {"axes": "z", "distance": -10.0}}[kind]
    p = out["preview"]
    assert p["will_send"] == {"method": mo.METHOD_HOME_AXES if kind == "home" else mo.METHOD_MOVE_AXES, "params": want}
    assert p["waits_up_to_s"] == mo.MOTION_WAIT_S[kind]
    assert p["bed_clear_required"] is bed
    assert p["would_refuse"] is None and p["printer_idle"] is True
    assert p["homed_axes_now"] == "xyz" and p["position_now"] == [128.0, 128.0, 5.0]
    assert p["vendor_step_choices_mm"] == [0.1, 1.0, 10.0, 30.0]
    assert "positive 1027 distance" in p["sign_convention"]
    assert p["verified_on_this_printer"] is False


async def test_the_preview_of_a_move_shows_the_axis_gate_and_the_printers_own_reason():
    out = await mo.read_preview(transport(status=frame(homed_axes="")), "move", axes="z",
                               distance=10)
    p = out["preview"]
    assert p["axis_homed"] is False and "does not name" in p["would_refuse"]
    out = await mo.read_preview(transport(status=frame(printing=True)), "move", axes="z",
                                distance=10)
    assert out["preview"]["printer_idle"] is False and JOB in out["preview"]["idle_reason"]
    out = await mo.read_preview(transport(status=frame(homed_axes=None)), "move", axes="z",
                               distance=10)
    assert out["preview"]["axis_homed"] is None and out["preview"]["would_refuse"] is None


async def test_a_preview_refuses_a_bad_frame_without_reading_the_printer():
    t = transport()
    with pytest.raises(PrinterError):
        await mo.read_preview(t, "move", axes="z", distance=500)
    assert t.requests == []


async def test_a_preview_whose_status_read_fails_is_raised_not_guessed():
    t = FakeMotionTransport({1002: PrinterError("not_reachable", "printer did not answer 1002")})
    with pytest.raises(PrinterError):
        await mo.read_preview(t, "home", axes="xyz")
    assert t.motion() == []


# --------------------------------------------------------------------------- the answer decides


@pytest.mark.parametrize("reply,expect,code", [
    ({"error_code": 0}, "accepted", None),
    ({"error_code": 1010}, "refused", 1010),
    ({"error_code": 7}, "refused", 7),
    ({}, "ambiguous", None),
    ({"error_code": True}, "ambiguous", None),
    ({"error_code": "0"}, "ambiguous", None),
    ({"error_code": 0.0}, "ambiguous", None),
])
async def test_only_an_explicit_integer_zero_is_acceptance(reply, expect, code):
    t = transport(motion=reply)
    r = await mo.run_motion(t, "home", axes="xyz", confirm=True)
    assert str(r.status) == expect
    assert r.error_code == code
    assert r.accepted is (expect == "accepted")
    assert t.methods().count(mo.METHOD_HOME_AXES) == 1        # the command once, in every outcome
    # a refusal on record earns no follow-up read; acceptance earns exactly one; an unreadable
    # answer earns none, because nothing about it says the method was not taken
    assert t.methods().count(1002) == (2 if expect == "accepted" else 1)


async def test_a_refusal_keeps_the_printers_own_code_and_moves_nothing_else():
    t = transport(motion={"error_code": 22})
    r = await mo.run_motion(t, "move", axes="z", distance=10, confirm=True, bed_clear=True)
    assert str(r.status) == "refused" and r.error_code == 22
    assert t.motion() == [(mo.METHOD_MOVE_AXES, {"axes": "z", "distance": 10.0}, 25.0)]
    assert t.methods() == [1002, mo.METHOD_MOVE_AXES]
    assert any("error_code 22" in n for n in r.notes)
    assert job.NO_OBSERVE_AFTER_REFUSAL in r.notes


async def test_a_lost_answer_is_ambiguous_never_a_refusal_and_earns_one_read():
    t = FakeMotionTransport({1002: frame(homed_axes="xyz"),
                             mo.METHOD_HOME_AXES: deque([lost(), frame(homed_axes="xyz")])})
    r = await mo.run_motion(t, "home", axes="xyz", confirm=True)
    assert str(r.status) == "ambiguous" and r.accepted is False
    assert t.methods() == [1002, mo.METHOD_HOME_AXES, 1002]     # the command once, never twice
    assert r.observed["observed_homed_axes"] == "xyz"
    joined = " ".join(r.notes)
    assert "neither a refusal nor a failure" in joined and "nothing was retried" in joined


async def test_a_dead_session_sends_nothing_further_at_all():
    t = FakeMotionTransport({1002: frame(),
                             mo.METHOD_HOME_AXES: PrinterError("not_reachable", "session ended")})
    r = await mo.run_motion(t, "home", axes="xyz", confirm=True)
    assert str(r.status) == "ambiguous"
    assert t.methods() == [1002, mo.METHOD_HOME_AXES]


async def test_a_home_whose_observation_cannot_be_read_still_reports_the_answer_it_had():
    t = FakeMotionTransport({1002: deque([frame(), PrinterError("not_reachable", "no reply")]),
                             mo.METHOD_HOME_AXES: OK})
    r = await mo.run_motion(t, "home", axes="xyz", confirm=True)
    assert r.accepted and r.observed is None
    assert any("did not land" in n for n in r.notes)


async def test_the_accepted_move_says_who_asserted_the_bed():
    t = transport()
    r = await mo.run_motion(t, "move", axes="z", distance=-0.1, confirm=True, bed_clear=True)
    joined = " ".join(r.notes)
    assert mo.BED_NOTE in r.notes and "bed_clear=True" in joined
    assert any(mo.SIGN_NOTE in n for n in r.notes)
    assert "positive 1027 distance" in joined      # the sign is never quietly explained away


# --------------------------------------------------------------------------- the candidate posture


async def test_every_reply_says_the_motion_is_unproven_on_hardware_and_carries_no_secret():
    t = transport()
    r = (await mo.run_motion(t, "home", axes="xyz", confirm=True)).as_dict()
    assert r["verified_on_this_printer"] is False and r["unverified"] == mo.UNVERIFIED
    out = await mo.read_preview(transport(), "move", axes="z", distance=10)
    assert out["preview"]["verified_on_this_printer"] is False
    blob = json.dumps(r) + json.dumps(out)
    for secret in (FAKE_TOKEN, HOST, SN):
        assert secret not in blob, f"{secret!r} rode a reply"


def test_no_motion_tool_is_registered():
    import orcaslicer_mcp.server as srv

    names = set(srv.mcp._tool_manager._tools)
    assert not [n for n in names if n.startswith("cc2_") and "motion" in n]
    assert not [n for n in names if n in ("cc2_home", "cc2_home_axes", "cc2_move_axes")]
    assert not [n for n in srv.CC2_TOOL_NAMES if "motion" in n or "home" in n or "move" in n]
    assert not [n for n in srv._TOOL_ANNOTATIONS if n.startswith("cc2_")
                and ("motion" in n or "home" in n or "move" in n)]


@pytest.mark.parametrize("relative", ["src/orcaslicer_mcp/server.py", "README.md",
                                      "lhm.plugin.json", "mcpb/manifest.json",
                                      "scripts/gen_lhm_tools.py"])
def test_the_candidate_is_not_offered_as_a_supported_capability(relative):
    text = (ROOT / relative).read_text(encoding="utf-8")
    assert "cc2_motion" not in text
    assert "cc2_home_axes" not in text and "cc2_move_axes" not in text


def test_the_module_is_importable_but_declares_no_tool():
    source = Path(inspect.getsourcefile(mo)).read_text(encoding="utf-8")
    assert "import server" not in source and "@mcp.tool" not in source


def test_no_authored_string_outgrows_the_direct_tools_reply_cap():
    long = [n for n, v in vars(mo).items()
            if isinstance(v, str) and not n.startswith("__") and len(v) > job.NOTE_CAP]
    long += [f"{name}[{key}]" for name, value in vars(mo).items()
             if isinstance(value, dict) and not name.startswith("__")
             for key, v in value.items() if isinstance(v, str) and len(v) > job.NOTE_CAP]
    assert long == [], f"these strings would be truncated in a reply: {long}"
