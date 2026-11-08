"""The CC2 fan control (method 1030) against scripted replies — a registered tool, an unproven
scale, and nothing here opens a socket. The transport is a fake that records every method and the
timeout each
request asked for, so "sent once, never resent" and "the preview sends only the read-only 1002" are
recorded facts rather than claims.

The 1002 frames carry the ``fans`` block exactly as both readers spell it (``fans.fan.speed``,
``fans.aux_fan.speed``, ``fans.box_fan.speed``, plus the ``controller_fan``/``heater_fan`` blocks the
integration's status mapping names), marked doc-derived: no CC2 has ever answered 1030 for this
project, which is why every reply says so rather than settling it. What the tests pin down:

* the argument bar — a whole percent 0..100 reads, and so does whole-number text; a 0-255 number
  above 100 (127, 255), a float, a bool, a name and a fourth fan name do not;
* the wire is Elegoo's own LAN control's arithmetic unreduced (50% -> 127.5), under the three field
  names that client writes, with a fan left out of the request rather than zeroed, and the older
  websocket ``TargetFanSpeed``/``ModelFan`` frame never appears on the wire;
* the preview sends the read-only 1002 and nothing else, and shows the percent, the number it
  becomes, the formula, and that the scale is unproven;
* accepted / refused / ambiguous are kept apart, only an explicit integer error_code 0 is accepted,
  a refusal keeps its code and earns no follow-up read, and a lost answer is never a refusal and
  never earns a resend;
* the read-back is honest about the unresolved unit: the printer's numbers are reported as they
  arrived, no percentage is derived from them anywhere, ``changed`` compares raw against raw, an
  absent fan block is unknown rather than zero, and every reply says
  ``verified_on_this_printer: false``;
* the confirmed path reads once before the command and once after it — 1002, 1030, 1002, one of each,
  with a pre-read that fails reported rather than hidden, and a refusal still earning no look after
  it;
* the exposed posture — the tool is registered and declared like the other CC2 controls, it is named
  in the server, the README, the listing and the changelog, those lines still say the scale is
  unproven, and every authored string fits the direct tools' reply cap."""
from __future__ import annotations

import copy
import json
from collections import deque
from pathlib import Path

import pytest

from orcaslicer_mcp.printer import cc2_control as job
from orcaslicer_mcp.printer import cc2_fan as fan
from orcaslicer_mcp.printer import cc2_state as st
from orcaslicer_mcp.printer.cc2_transport import Cc2TimeoutError, PrinterError

ROOT = Path(__file__).resolve().parents[1]

HOST = "192.0.2.10"                  # documented TEST-NET, like the whole lane; never a live address
SN = "CC2TESTSN0001"
FAKE_TOKEN = "SIMULATED-NOT-A-CODE"  # a stand-in credential, proven never to ride any reply
JOB = "benchy-petg-2026-11-04.gcode"

# doc-derived for the fans block: the shape the official client reads (fans.<x>.speed) and the
# integration's status mapping agree on, with raw 0-255-shaped numbers on purpose.
BASE_1002 = {
    "error_code": 0,
    "machine_status": {"exception_status": [], "progress": 21, "status": 2, "sub_status": 2075,
                       "sub_status_reason_code": 0},
    "print_status": {"current_layer": 51, "filename": JOB, "print_duration": 573,
                     "remaining_time_sec": 2096, "state": "printing", "total_duration": 2669,
                     "uuid": "80baf1df-e748-4717-adf1-b9487f2c8f32"},
    "extruder": {"filament_detect_enable": 1, "filament_detected": 1, "target": 260,
                 "temperature": 259.6},
    "heater_bed": {"target": 70, "temperature": 69.9},
    "led": {"status": 1},
    "gcode_move_inf": {"x": 88.148, "y": 139.946, "z": 1.6, "e": 138.87, "speed": 9019,
                       "speed_mode": 1},
    "fans": {"fan": {"speed": 0}, "aux_fan": {"speed": 0}, "box_fan": {"speed": 0},
             "controller_fan": {"speed": 128}, "heater_fan": {"speed": 0}},
}


def frame_1002(*, fans=..., state="printing") -> dict:
    """A 1002 as this firmware answers it, with the fans block set to whatever a test needs
    (``None`` removes it entirely, ``{...}`` overrides one fan). ``...`` leaves the block alone."""
    frame = copy.deepcopy(BASE_1002)
    frame["print_status"]["state"] = state
    if fans is None:
        frame.pop("fans")
    elif fans is not ...:
        frame["fans"] = copy.deepcopy(fans)
    return frame


class FakeFanTransport:
    """Stands in for the pinned session exactly as Cc2Transport behaves: records every request with
    the timeout it was given, answers from a method -> script map (a deque shifts, last value sticks;
    exceptions raise as given; an unscripted method answers {}) — and sends nothing twice on its own,
    because the module under test is what must not ask twice."""

    def __init__(self, replies=None, *, host=HOST, sn=SN):
        self.host, self.sn = host, sn
        self.replies = dict(replies or {})
        self.requests: list[tuple[int, dict, float | None]] = []

    async def request(self, method: int, params: dict | None = None, *, timeout_s=None) -> dict:
        self.requests.append((method, copy.deepcopy(params or {}), timeout_s))
        reply = self.replies.get(method, {})
        if callable(reply):
            reply = reply(params or {})
        if isinstance(reply, deque):
            reply = reply.popleft() if len(reply) > 1 else reply[0]
        if isinstance(reply, BaseException):
            raise reply
        return copy.deepcopy(reply)

    def methods(self) -> list[int]:
        return [m for m, _, _ in self.requests]

    def calls(self, method: int) -> list[tuple[int, dict, float | None]]:
        return [r for r in self.requests if r[0] == method]


def scripted(*, status=None, command=None) -> FakeFanTransport:
    """The 1002 answers ``status`` (default: every commanded fan at 0) and 1030 answers ``command``
    (default the documented explicit success) — every method recorded either way, so a send the tests
    did not expect is still a recorded fact."""
    command_reply = {"error_code": 0} if command is None else command
    return FakeFanTransport({1002: frame_1002() if status is None else status,
                             1030: command_reply})


def timeout() -> PrinterError:
    return Cc2TimeoutError("protocol_error", "simulated: the printer never answered",
                           reason="timeout", ambiguous=True)


# --------------------------------------------------------------------------- the argument bar


@pytest.mark.parametrize("value,want", [(0, 0), (1, 1), (50, 50), (100, 100), ("50", 50),
                                        (" 100 ", 100), ("+50", 50)])
def test_a_whole_percent_reads_however_it_is_spelled(value, want):
    assert fan.parse_percent(value, "model") == want


@pytest.mark.parametrize("bad", [101, -1, 127, 255, 2.5, True, False, "fast", "50%", "", None],
                         ids=["101", "minus", "a-255-scale-number", "wire-max", "float",
                              "bool-true", "bool-false", "name", "percent-suffix", "empty",
                              "missing"])
def test_anything_that_is_not_a_whole_percent_refuses(bad):
    # 127 and 255 are the mistake this bar exists for: those are the OTHER scale's numbers, and a
    # module that accepted both scales would be guessing which one a caller meant
    with pytest.raises(PrinterError) as caught:
        fan.parse_percent(bad, "model")
    assert caught.value.code == "protocol_error"
    assert caught.value.details["reason"] == "fan_percent_range"
    assert "Nothing was sent" in caught.value.message
    assert "percent 0-100" in caught.value.hint


@pytest.mark.parametrize("bad", ["++5", "+-5", "5 0", "5_0", "0x10", "\uff15\uff10", "\u0665\u0660",
                                 "50.0", "1e2", "50,"],
                         ids=["double-plus", "plus-minus", "inner-space", "underscore", "hex",
                              "full-width-50", "arabic-indic-50", "decimal-text", "exponent",
                              "trailing-comma"])
def test_text_that_only_looks_like_a_percent_refuses_with_this_modules_error(bad):
    # `str.isdigit()` answers yes to '\u0665' and to '\uff15\uff10' and `int()` then reads them happily,
    # while `lstrip("+")` in front of a digit test handed `"++5"` straight to `int()`, which raised its
    # own `ValueError` out of a function whose whole contract is a PrinterError naming the two scales.
    # A raw ValueError reaching a reply is a crash where a caller was promised a reason and "nothing was
    # sent", so these spellings refuse, and refuse in this module's words: the digits a percent may be
    # written in are the ASCII ones this module can spell back in its own refusal.
    with pytest.raises(PrinterError) as caught:
        fan.parse_percent(bad, "model")
    assert caught.value.code == "protocol_error"
    assert caught.value.details["reason"] == "fan_percent_range"
    assert "Nothing was sent" in caught.value.message
    assert "percent 0-100" in caught.value.hint


@pytest.mark.parametrize("bad", [True, False, 0.0, 50.0, 100.5, "50.0", "++5", "\uff15\uff10",
                                 "fast", "50%"],
                         ids=["bool-true", "bool-false", "float-zero", "float-50", "float-100.5",
                              "decimal-text", "double-plus", "full-width-50", "name",
                              "percent-suffix"])
def test_the_schema_door_refuses_what_lax_coercion_would_turn_into_a_percent(bad):
    # the shape this tool publishes is `{"type": "integer"}`, and an argument model in lax mode reads 1
    # out of `true` and 50 out of `50.0` before this module sees anything — so `model=true` would put
    # 2.55 on the part-cooling fan and a float would quietly become a percent. This is the same bar run
    # one layer earlier, on the value while it is still the caller's own.
    with pytest.raises(PrinterError) as caught:
        fan.check_percent_input(bad)
    assert caught.value.code == "protocol_error"
    assert caught.value.details["reason"] == "fan_percent_type"
    assert "Nothing was sent" in caught.value.message
    assert "percent 0-100" in caught.value.hint


@pytest.mark.parametrize("ok", [0, 1, 50, 100, "50", " 100 ", "+50", 101, 255, None])
def test_the_schema_door_passes_through_what_the_printers_door_decides(ok):
    # this door decides spelling, not the number: 101 and 255 are the range's problem, refused by
    # parse_percent with the fan's own name in the sentence, and whole-number text is read on purpose
    assert fan.check_percent_input(ok) is ok


@pytest.mark.parametrize("bad", ["true", "True", "yes", "on", "1", 1, 0, 1.0, None, "", [True]],
                         ids=["true-word", "True-word", "yes", "on", "one-word", "int-1", "int-0",
                              "float", "missing", "empty", "list"])
def test_a_confirm_that_is_not_a_json_boolean_is_never_a_yes(bad):
    # `confirm is True` behind a lax argument model is not the guard it looks like: the schema says
    # boolean and the model happily reads `"true"`, `"yes"`, `"on"` and `1` as true, which is how a
    # two-phase control whose entire job is that nothing moves until somebody says yes gets typed past.
    # A confirmation this server cannot read is refused, and the message names what it was given.
    with pytest.raises(ValueError) as caught:
        fan.strict_confirm(bad)
    said = str(caught.value)
    assert "JSON booleans true or false" in said
    assert "Nothing was sent" in said
    assert "not a yes" in said


@pytest.mark.parametrize("ok", [True, False])
def test_a_real_boolean_confirm_survives_the_validator_untouched(ok):
    # the second lock stays: whatever reaches the tool body is still checked with `confirm is True`
    assert fan.strict_confirm(ok) is ok


@pytest.mark.parametrize("percent,want", [(0, 0), (20, 51), (100, 255), (10, 25.5), (50, 127.5),
                                          (33, 33 * 255 / 100)])
def test_the_wire_value_is_the_official_clients_arithmetic_unreduced(percent, want):
    # Elegoo's LAN control sends `v/100*255` and rounds nothing, so 50% really does leave as 127.5;
    # rounding here would put this project's fingerprint on a frame the official client does not write
    assert fan.wire_value(percent) == want
    assert isinstance(fan.wire_value(percent), int if float(want).is_integer() else float)


def test_the_three_fields_are_the_ones_the_official_client_writes():
    assert fan.FAN_FIELDS == {"model": "fan", "auxiliary": "aux_fan", "chamber": "box_fan"}


def test_params_carry_the_wire_numbers_and_asked_percent_stays_beside_them():
    values = {"model": 100, "auxiliary": 50, "chamber": 0}
    assert fan.fan_params(values) == {"fan": 255, "aux_fan": 127.5, "box_fan": 0}
    assert fan.asked_percent(values) == {"fan": 100, "aux_fan": 50, "box_fan": 0}


def test_a_fan_left_out_is_left_alone_not_zeroed():
    # the official client builds its params with `value !== void 0 && {field: value}`, so omission is
    # the spelling of "do not touch this fan" and zero is a command to stop it
    assert fan.fan_params({"auxiliary": 75, "model": None}) == {"aux_fan": 191.25}


@pytest.mark.parametrize("values", [{}, {"model": None}, {"auxiliary": None}],
                         ids=["empty", "all-none", "one-none"])
def test_no_fan_at_all_refuses_before_anything_exists(values):
    with pytest.raises(PrinterError) as caught:
        fan.fan_params(values)
    assert caught.value.details["reason"] == "fan_none"
    assert "Nothing was sent" in caught.value.message


@pytest.mark.parametrize("name", ["controller_fan", "heater_fan", "part_fan", "ModelFan", "box"])
def test_a_fourth_fan_name_refuses_rather_than_becoming_an_invented_field(name):
    # controller_fan and heater_fan are real names in the integration's status read and no source
    # commands them, so even a real-looking key must not become a wire field
    with pytest.raises(PrinterError) as caught:
        fan.fan_params({name: 50})
    assert caught.value.details["reason"] == "fan_name"
    assert "controller_fan and heater_fan appear" in caught.value.hint


def test_a_hand_rolled_frame_is_refused_by_the_second_lock():
    for bad in [{"fan": 255.0 + 0.5j}, {"fan": True}, {"fan": 300}, {"ModelFan": 1}, {}, "x"]:
        with pytest.raises((ValueError, TypeError)):
            fan.checked_params(bad)
    assert fan.checked_params({"fan": 127.5}) == {"fan": 127.5}


# --------------------------------------------------------------------------- the preview


async def test_the_preview_sends_the_read_only_status_and_nothing_else():
    t = scripted()
    out = await fan.read_preview(t, {"model": 100})
    assert t.methods() == [1002]
    assert t.calls(1030) == []
    assert out["confirm_required"] is True and out["action"] == "fan"
    assert out["how"] == job.PREVIEW_HOW


async def test_the_preview_shows_the_percent_the_number_and_the_formula_side_by_side():
    t = scripted()
    preview = (await fan.read_preview(t, {"model": 50, "chamber": 0}))["preview"]
    assert preview["will_send"] == {"method": 1030, "params": {"fan": 127.5, "box_fan": 0}}
    assert preview["asked_percent"] == {"fan": 50, "box_fan": 0}
    assert preview["wire_formula"] == fan.SCALE_NOW
    assert "/ 100 * 255" in fan.SCALE_NOW
    assert preview["waits_up_to_s"] == fan.SET_WAIT_S
    assert preview["verified_on_this_printer"] is False
    # the doubt has to be specific: which method, and the two scales that disagree about it
    assert "1030" in preview["unverified"] and "0-255" in preview["unverified"]
    assert "0-100" in preview["unverified"]


async def test_the_preview_carries_the_printers_own_numbers_without_a_unit_claim():
    t = scripted(status=frame_1002(fans={"fan": {"speed": 255}, "aux_fan": {"speed": 0},
                                         "box_fan": {"speed": 0}}))
    preview = (await fan.read_preview(t, {"model": 0}))["preview"]
    assert preview["fans_now_raw"] == {"model": 255, "auxiliary": 0, "chamber": 0}
    assert preview["readback_unit"] == fan.READBACK_UNIT
    assert "No unit" in fan.READBACK_UNIT
    assert not any("percent" in k for k in preview["fans_now_raw"])


async def test_the_preview_says_it_checked_nothing_about_the_moment():
    # a two-phase control reads as a safety check, so a preview that stays silent about the print state
    # it did not read would be read as a clean bill of health. What it carries instead is the truth:
    # nothing about the moment was checked, and `cc2_status` is where the state lives.
    t = scripted()
    out = await fan.read_preview(t, {"model": 100})
    assert out["state_checked"] is False and out["state_note"] == fan.STATE_NOT_CHECKED
    assert out["preview"]["state_checked"] is False
    assert out["preview"]["state_note"] == fan.STATE_NOT_CHECKED
    assert "print is running" in fan.STATE_NOT_CHECKED
    assert "cc2_status" in fan.STATE_NOT_CHECKED
    # and it is a decision, not a missing gate: the sentence says the preview is not evidence of safety
    assert "never evidence" in fan.STATE_NOT_CHECKED


async def test_a_command_reply_carries_the_same_two_admissions_as_a_preview():
    # a confirmed call is where a person could most easily believe the machine was checked, so the
    # admission is on the reply the command earns as well as on the preview that preceded it
    out = (await fan.set_fan(scripted(), {"model": 100})).as_dict()
    assert out["state_checked"] is False and out["state_note"] == fan.STATE_NOT_CHECKED
    assert out["verified_on_this_printer"] is False


def test_a_refusal_before_a_session_exists_still_carries_what_every_fan_reply_carries():
    # the four fields are this tool's promise on every reply, and a pre-session refusal is exactly the
    # reply that needs the scale caveat: whoever just sent a bare 255 is whoever needs to be told which
    # of the two scales the tool refused to pick between
    keys = fan.refusal_keys()
    assert set(keys) == set(fan.FAN_REFUSAL_KEYS)
    assert keys["verified_on_this_printer"] is False and keys["state_checked"] is False
    assert keys["unverified"] == fan.UNVERIFIED and keys["state_note"] == fan.STATE_NOT_CHECKED


async def test_an_absent_fans_block_is_unknown_rather_than_zero():
    t = scripted(status=frame_1002(fans=None))
    preview = (await fan.read_preview(t, {"model": 10}))["preview"]
    assert preview["fans_now_raw"] == {"model": None, "auxiliary": None, "chamber": None}


async def test_a_status_read_that_fails_is_raised_not_papered_over():
    t = FakeFanTransport({1002: st.Cc2DataError("protocol_error", "simulated: unreadable 1002")})
    with pytest.raises(PrinterError):
        await fan.read_preview(t, {"model": 10})
    assert t.methods() == [1002]


# --------------------------------------------------------------------------- the one command


async def test_an_accepted_command_goes_once_and_takes_one_look():
    t = scripted()
    result = await fan.set_fan(t, {"model": 100, "auxiliary": 50})
    assert t.methods() == [1030, 1002]
    assert len(t.calls(1030)) == 1
    assert t.calls(1030)[0][1] == {"fan": 255, "aux_fan": 127.5}
    assert t.calls(1030)[0][2] == 10.0          # the longer of the two first-party waits
    assert result.status is job.ControlStatus.ACCEPTED and result.accepted is True
    assert result.sent == {"fan": 255, "aux_fan": 127.5}
    assert result.asked == {"fan": 100, "aux_fan": 50}


async def test_the_websocket_frames_the_same_bundle_still_carries_are_never_sent():
    t = scripted()
    await fan.set_fan(t, {"model": 100, "auxiliary": 10, "chamber": 100})
    sent = t.calls(1030)[0][1]
    assert set(sent) == {"fan", "aux_fan", "box_fan"}
    assert "TargetFanSpeed" not in json.dumps(t.requests)
    assert "ModelFan" not in json.dumps(t.requests)


@pytest.mark.parametrize("code", [1003, 1010, 1])
async def test_a_refusal_keeps_its_code_and_earns_no_follow_up_read(code):
    t = scripted(command={"error_code": code})
    result = await fan.set_fan(t, {"model": 100})
    assert t.methods() == [1030]
    assert result.status is job.ControlStatus.REFUSED and result.accepted is False
    assert result.error_code == code
    assert any(str(code) in n for n in result.notes)


@pytest.mark.parametrize("reply", [{}, {"error_code": True}, {"error_code": "0"},
                                   {"error_code": 0.0}],
                         ids=["no-code", "bool", "text", "float"])
async def test_an_answer_that_cannot_be_read_is_ambiguous_and_never_resent(reply):
    t = scripted(command=reply)
    result = await fan.set_fan(t, {"model": 100})
    assert t.methods() == [1030]
    assert result.status is job.ControlStatus.AMBIGUOUS and result.accepted is False
    assert result.error_code is None


async def test_a_lost_answer_is_ambiguous_one_look_and_no_resend():
    t = FakeFanTransport({1030: timeout(), 1002: frame_1002()})
    result = await fan.set_fan(t, {"model": 100})
    assert t.methods() == [1030, 1002]
    assert result.status is job.ControlStatus.AMBIGUOUS
    assert any("within 10" in n or "did not answer" in n for n in result.notes)


async def test_a_dead_session_reports_alone_and_sends_nothing_further():
    t = FakeFanTransport({1030: PrinterError("protocol_error", "simulated: session ended")})
    result = await fan.set_fan(t, {"model": 100})
    assert t.methods() == [1030]
    assert result.status is job.ControlStatus.AMBIGUOUS


# --------------------------------------------------------------------------- the read-back


async def test_the_printers_numbers_are_reported_exactly_as_they_arrived():
    after = frame_1002(fans={"fan": {"speed": 255}, "aux_fan": {"speed": 7}, "box_fan": {"speed": 0}})
    t = scripted(status=after)
    result = await fan.set_fan(t, {"model": 100})
    assert result.observed["raw"] == {"model": 255, "auxiliary": 7, "chamber": 0}


async def test_no_percentage_is_ever_derived_from_the_printers_numbers():
    after = frame_1002(fans={"fan": {"speed": 255}, "aux_fan": {"speed": 100},
                             "box_fan": {"speed": 0}})
    t = scripted(status=after)
    reported = (await fan.set_fan(t, {"model": 100, "auxiliary": 100})).as_dict()
    assert reported["observed_fan_raw"] == {"model": 255, "auxiliary": 100, "chamber": 0}
    percent_keys = [k for k in reported if "percent" in k.lower()]
    assert percent_keys == ["asked_percent"], percent_keys
    assert reported["asked_percent"] == {"fan": 100, "aux_fan": 100}


async def test_changed_compares_raw_against_raw_and_says_true_when_a_number_moved():
    t = scripted()                                   # the "after" frame is all zeros
    before = {"fans_now_raw": {"model": 0, "auxiliary": 128, "chamber": 0}}
    result = await fan.set_fan(t, {"model": 100}, before=before)
    assert result.observed["changed"] == {"model": False, "auxiliary": True, "chamber": False}


async def test_without_a_before_reading_nothing_is_claimed_to_have_stayed_put():
    t = scripted()
    result = await fan.set_fan(t, {"model": 100})
    assert result.observed["changed"] == {"model": None, "auxiliary": None, "chamber": None}
    assert not any("still reports" in n for n in result.notes)


async def test_an_accepted_command_that_moved_nothing_says_so_without_calling_it_a_failure():
    t = scripted()                                   # 0 before, 0 after
    result = await fan.set_fan(t, {"model": 100}, before={"fans_now_raw": {"model": 0}})
    assert result.status is job.ControlStatus.ACCEPTED
    note = [n for n in result.notes if "still reports" in n]
    assert len(note) == 1 and "model" in note[0] and "raw-against-raw" in note[0]
    assert "unproven" in " ".join(result.notes)


async def test_a_status_that_names_none_of_these_fans_leaves_the_question_open():
    t = scripted(status=frame_1002(fans=None))
    result = await fan.set_fan(t, {"model": 100}, before={"fans_now_raw": {"model": 0}})
    assert result.status is job.ControlStatus.ACCEPTED
    assert result.observed["raw"] == {"model": None, "auxiliary": None, "chamber": None}
    assert any("named none of these fans" in n for n in result.notes)


async def test_a_follow_up_look_that_does_not_land_says_which_half_is_missing():
    t = FakeFanTransport({1030: {"error_code": 0}, 1002: st.Cc2DataError("protocol_error",
                                                                        "simulated: no 1002")})
    result = await fan.set_fan(t, {"model": 100})
    assert result.status is job.ControlStatus.ACCEPTED
    assert result.observed is None
    assert any("read-only status look" in n for n in result.notes)


@pytest.mark.parametrize("speed,want", [(255, 255), (0, 0), (127.5, 127.5), (True, None),
                                        ("128", None), (None, None)],
                         ids=["int", "zero", "float", "bool", "text", "missing"])
def test_one_fans_number_is_kept_if_it_is_a_number_and_is_no_reading_otherwise(speed, want):
    assert fan._speed_or_none(speed) == want


def test_a_fans_block_that_is_not_the_documented_shape_is_no_reading():
    for raw in [{}, {"fans": []}, {"fans": {"fan": 5}}, {"fans": {"fan": {"speed": None}}}]:
        machine = st.Cc2MachineStatus(machine_status=2, sub_status=2075,
                                      print_state=st.PrintState.PRINTING, raw_print_state="printing",
                                      raw=raw)
        assert set(fan.fan_raws(machine)) == set(fan.FANS)
        assert all(value is None for value in fan.fan_raws(machine).values())


async def test_no_reply_carries_an_address_a_serial_or_a_credential():
    for transport in (scripted(), scripted(command={"error_code": 1003}),
                      FakeFanTransport({1030: timeout(), 1002: frame_1002()})):
        reported = json.dumps((await fan.set_fan(transport, {"model": 100},
                                                 before=None)).as_dict())
        for banned in (HOST, SN, FAKE_TOKEN):
            assert banned not in reported


# --------------------------------------------------------------------------- the confirmed path's two looks


async def test_the_confirmed_path_reads_once_before_the_command_once_after_and_never_thrice():
    # 1002 -> 1030 -> 1002, in that order, one of each: the look before is what makes 'moved' a fact,
    # and three requests is the whole budget — a fourth would be a poll or a resend in disguise
    transport = FakeFanTransport({1002: deque([
        frame_1002(),
        frame_1002(fans={"fan": {"speed": 255}, "aux_fan": {"speed": 0},
                            "box_fan": {"speed": 0}})]),
        1030: {"error_code": 0}})
    out = await fan.set_fan_with_before(transport, {"model": 100})
    assert transport.methods() == [1002, 1030, 1002]
    assert len(transport.calls(1030)) == 1 and transport.calls(1030)[0][1] == {"fan": 255}
    assert out.accepted and out.status is job.ControlStatus.ACCEPTED
    json.dumps(out.as_dict())


async def test_the_number_moving_is_reported_as_moving():
    # 0 -> 255 after an accepted 100%: the one observation the live test came for, and here the unit
    # still says nothing about it — 'moved' is raw against raw
    transport = FakeFanTransport({1002: deque([frame_1002(), frame_1002(
        fans={"fan": {"speed": 255}, "aux_fan": {"speed": 0}, "box_fan": {"speed": 0}})]),
        1030: {"error_code": 0}})
    out = await fan.set_fan_with_before(transport, {"model": 100})
    assert out.observed["raw"]["model"] == 255
    assert out.observed["changed"] == {"model": True, "auxiliary": False, "chamber": False}
    assert out.as_dict()["observed_fan_changed"]["model"] is True


async def test_a_pre_read_that_does_not_lands_still_sends_the_command_and_says_what_is_missing():
    # the printer's answer to the 1030 is still the printer's own words, so a failed read before it
    # is reported, not turned into a reason to withhold the command — and 'changed' stays unknown
    transport = FakeFanTransport({1002: deque([PrinterError("protocol_error", "simulated: no 1002",
                                                            reason="no_data"), frame_1002()]),
                                  1030: {"error_code": 0}})
    out = await fan.set_fan_with_before(transport, {"model": 60})
    assert transport.methods() == [1002, 1030, 1002] and len(transport.calls(1030)) == 1
    assert out.accepted
    assert all(value is None for value in out.observed["changed"].values())
    assert any("did not land" in n and "unknown" in n for n in out.notes)


async def test_a_refusal_from_the_printer_keeps_its_vocabulary_whole_and_reads_nothing_after():
    # the pre-read runs before the command exists, so it does not soften the rule that an on-record
    # refusal earns no follow-up read — and the reply keeps saying that plain thing about itself
    transport = FakeFanTransport({1002: frame_1002(), 1030: {"error_code": 1003}})
    out = await fan.set_fan_with_before(transport, {"chamber": 100})
    assert transport.methods() == [1002, 1030]
    assert out.status is job.ControlStatus.REFUSED and out.error_code == 1003
    assert not any("did not land" in n for n in out.notes)
    assert any("Nothing was resent" in n for n in out.notes)


async def test_a_pre_read_failure_never_echoes_the_transport_and_stays_inside_the_cap():
    # the after-command look's note quotes the transport, the house pattern; this module's own rule is
    # that a note it authors carries no address, no serial and no token, so the pre-read note stays
    # its own sentence however noisy the failure underneath it was
    host_sounding = PrinterError("protocol_error",
                                 f"simulated: the printer at {HOST} ({SN}) refused with token "
                                 f"{FAKE_TOKEN}", reason="no_data")
    transport = FakeFanTransport({1002: deque([host_sounding, frame_1002()]), 1030: {"error_code": 0}})
    out = await fan.set_fan_with_before(transport, {"model": 100})
    reported = json.dumps(out.as_dict())
    assert any("did not land" in n for n in out.notes) and out.accepted
    for banned in (HOST, SN, FAKE_TOKEN):
        assert banned not in reported, banned
    assert all(len(note) <= job.NOTE_CAP for note in out.notes), [len(n) for n in out.notes]


# --------------------------------------------------------------------------- the exposed posture


def test_the_fan_tool_is_registered_and_declared_like_every_other_cc2_control():
    import orcaslicer_mcp.server as srv

    names = set(srv.mcp._tool_manager._tools)
    assert "cc2_set_fan" in names
    assert "cc2_set_fan" in srv.CC2_TOOL_NAMES
    assert "cc2_set_fan" in srv._OPEN_WORLD_TOOLS
    title, read_only, destructive = srv._TOOL_ANNOTATIONS["cc2_set_fan"]
    assert title and read_only is False and destructive is False


@pytest.mark.parametrize("relative", ["src/orcaslicer_mcp/server.py", "README.md",
                                      "lhm.plugin.json", "CHANGELOG.md"])
def test_the_fan_tool_is_named_where_a_client_or_a_human_reads_capabilities(relative):
    text = (ROOT / relative).read_text(encoding="utf-8")
    assert "cc2_set_fan" in text, relative


@pytest.mark.parametrize("relative", ["README.md", "CHANGELOG.md"])
def test_the_docs_keep_saying_the_scale_is_unproven_where_they_name_the_fan_tool(relative):
    # exposure must not read as proof: the line that names cc2_set_fan has to carry the caveat with it
    lines = (ROOT / relative).read_text(encoding="utf-8").splitlines()
    named = [line for line in lines if "cc2_set_fan" in line]
    assert named, relative
    assert any("unproven" in line or "never answered 1030" in line for line in named), relative


def test_the_module_is_importable_but_imports_nothing_from_the_server():
    import inspect

    source = Path(inspect.getsourcefile(fan)).read_text(encoding="utf-8")
    assert "import server" not in source
    assert "@mcp.tool" not in source
    assert "open_transport" not in source      # the module never makes its own session


def test_the_peripheral_module_still_refuses_to_send_1030():
    # exposing the fan as its own tool changes nothing about the module that held the method back:
    # two implementations of one disagreement is bad, and quietly widening a tested refusal is worse
    from orcaslicer_mcp.printer import cc2_peripherals as per

    assert fan.METHOD_SET_FAN not in per._METHODS.values()


def test_the_live_test_recipe_names_the_edits_a_real_one_requires():
    server = (ROOT / "src/orcaslicer_mcp/server.py").read_text(encoding="utf-8")
    for structure in ("CC2_TOOL_NAMES", "_TOOL_ANNOTATIONS", "_OPEN_WORLD_TOOLS", "@mcp.tool()"):
        assert structure in server

async def test_every_authored_string_fits_the_direct_tools_reply_cap():
    texts = [fan.UNVERIFIED, fan.SCALE_NOW, fan.READBACK_UNIT, fan.CONTRADICT.format(
        fan="model", value=0), fan.OBSERVE_UNKNOWN_NOTE, fan.PRE_READ_LOST_NOTE, fan._AT_LEAST_ONE,
        fan.STATE_NOT_CHECKED, job.PREVIEW_HOW]
    texts += [n for n in (await fan.set_fan(scripted(), {"model": 100})).notes]
    texts += [n for n in (await fan.set_fan(scripted(command={"error_code": 1003}),
                                            {"model": 100})).notes]
    texts += [n for n in (await fan.set_fan(FakeFanTransport({1030: timeout(),
                                                              1002: frame_1002()}),
                                            {"model": 100})).notes]
    for note in texts:
        assert len(note) <= job.NOTE_CAP, f"{note[:50]!r} is {len(note)} characters"
