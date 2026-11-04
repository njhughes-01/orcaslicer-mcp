"""The CC2 temperature setpoint control (1028) against scripted replies, in the same house style as
the job-control and peripheral tests: nothing opens a socket, the transport is a fake that records
every method and the timeout each request asked for. The 1002 frames are the observed idle and
printing shapes from test_cc2_state with their ``extruder`` and ``heater_bed`` blocks (a scalar
``temperature`` plus a ``target``, the only shape this firmware is known to answer), and the wire
facts are the sources' own: Elegoo's LAN web client (release 20260625) sends
``{extruder: v}`` for the nozzle row and ``{heater_bed: v}`` for the bed row, one heater per request,
and gives those two inputs the bounds 0-350 and 0-110; Elegoo's LAN SDK maps the same call onto the
same two fields. What the tests pin down:

* the heater bar — the two names the protocol commands, each also spelled the way a person reads it,
  and nothing else, with ``chamber`` refused as the reading-only thing it is;
* the number bar — a whole or fractional °C or its text, never a bool or a word, and the ceiling a
  call may ask for is the printer's own control's ceiling per heater: 320 is legal for the nozzle
  and illegal for the bed, which is the proof that no single universal number was assumed;
* the wire shape the sources actually show — one heater field per request, whole numbers sent whole,
  and never a second method of any kind (no 1029/1030/1031, no job control, no 1047);
* the preview sends the read-only 1002 and nothing else, shows this call's exact params, names the
  ceiling it enforced and where that bound comes from, and says so out loud when a call asks above
  the 300 the other Elegoo source names;
* accepted / refused / ambiguous kept apart, only an explicit integer error_code 0 accepted, a
  refusal keeping its code and earning no follow-up read, and a lost or unreadable answer never
  called a refusal and never resent — the one allowed follow-up is a single read-only 1002;
* the read-back is a fact of its own: the target the printer reports afterwards is compared with the
  number just sent and a disagreement is said out loud without earning a second command, a target
  within the stated half-degree tolerance is not a disagreement, an absent field is None rather than
  zero, and a still-cold actual temperature is never read as a failure;
* every note survives the direct tools' reply cap whole, and no address, serial or token rides in
  any reply."""
from __future__ import annotations

import copy
import json
from collections import deque

import pytest

from orcaslicer_mcp.printer import cc2_control as job
from orcaslicer_mcp.printer import cc2_state as st
from orcaslicer_mcp.printer import cc2_temperature as temp
from orcaslicer_mcp.printer.cc2_transport import Cc2TimeoutError, PrinterError

HOST = "192.0.2.10"                  # documented TEST-NET, like the whole lane; never a live address
SN = "CC2TESTSN0001"
FAKE_TOKEN = "SIMULATED-NOT-A-CODE"  # a stand-in credential, proven never to ride any reply
JOB = "benchy-petg-2026-11-04.gcode"

# The observed 1002 shapes (test_cc2_state / test_cc2_start). The two heater blocks are the form the
# protocol doc documents and this firmware answers: a scalar temperature plus a separate target.
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
    "led": {"status": 0},
    "gcode_move_inf": {"x": 88.148, "y": 139.946, "z": 1.6, "e": 138.87, "speed": 9019,
                       "speed_mode": 1},
}


def frame_1002(*, extruder=..., bed=..., state="printing") -> dict:
    """A 1002 as this firmware answers it, with either heater block replaced (or removed with
    ``None``) to whatever a test needs. ``...`` leaves the frame's own block alone."""
    frame = copy.deepcopy(BASE_1002)
    frame["print_status"]["state"] = state
    for key, value in (("extruder", extruder), ("heater_bed", bed)):
        if value is None:
            frame.pop(key)
        elif value is not ...:
            frame[key] = value
    return frame


class FakeTempTransport:
    """Stands in for the registered session exactly as Cc2Transport behaves: records every request
    with the timeout it was given, answers from a method -> script map (a deque shifts, last value
    sticks; exceptions raise as given; an unscripted method answers {}) — and sends nothing twice on
    its own, because the module under test is what must not ask twice."""

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
        return copy.deepcopy(reply)

    def methods(self) -> list[int]:
        return [m for m, _, _ in self.requests]

    def calls(self, method: int) -> list[tuple[int, dict, float | None]]:
        return [r for r in self.requests if r[0] == method]


def timeout() -> PrinterError:
    """The transport's own honest lost-reply error — the reason a setpoint must come back ambiguous
    and never resent."""
    return Cc2TimeoutError("protocol_error", "simulated: the printer never answered",
                           reason="timeout", ambiguous=True)


def scripted(*, status=None, command=None) -> FakeTempTransport:
    """The 1002 answers ``status`` (default: printing, nozzle at target, bed at target), and 1028
    answers ``command`` (default the documented explicit success) — every method recorded either way,
    so a send the tests did not expect is still a recorded fact."""
    command_reply = {"error_code": 0} if command is None else command
    return FakeTempTransport({1002: frame_1002() if status is None else status,
                              1028: command_reply})


# --------------------------------------------------------------------------- the heater bar


@pytest.mark.parametrize("spelling, field", [
    ("extruder", "extruder"), ("extruder ", "extruder"), ("Extruder", "extruder"),
    ("nozzle", "extruder"), ("Nozzle", "extruder"),
    ("heater_bed", "heater_bed"), ("heater_bed  ", "heater_bed"), ("bed", "heater_bed"),
])
def test_either_heater_reads_by_its_wire_name_or_its_row_label(spelling, field):
    # every accepted spelling appears in a source: the wire fields (both senders), the official
    # control's own row label for the nozzle, and the word the maintainer's companion uses for bed
    assert temp.parse_heater(spelling) == field


@pytest.mark.parametrize("bad", ["chamber", "box", "enclosure", "fan", "hotend", "nozzle_temp",
                                 "", " extruder bed", 1, None, True, ["extruder"]],
                         ids=["chamber", "box", "enclosure", "fan", "hotend", "field-plus",
                              "empty", "two-at-once", "number", "missing", "bool", "list"])
def test_anything_that_is_not_one_of_the_two_heaters_refuses(bad):
    with pytest.raises(PrinterError) as caught:
        temp.parse_heater(bad)
    assert caught.value.code == "protocol_error"
    assert caught.value.details["reason"] == "heater_name"
    assert "Nothing was sent" in caught.value.message
    assert "'extruder'" in caught.value.hint and "'heater_bed'" in caught.value.hint


def test_the_chamber_refuses_as_a_reading_not_a_control():
    # the official control's chamber row is not editable and the firmware config gives the box a
    # sensor with no heater behind it, so the refusal says that instead of implying a capability
    with pytest.raises(PrinterError) as caught:
        temp.parse_heater("chamber")
    assert "sensor, no heater" in caught.value.hint
    assert "not editable" in caught.value.hint


# --------------------------------------------------------------------------- the number bar


@pytest.mark.parametrize("value, want", [(220, 220.0), (0, 0.0), (60.5, 60.5), (350, 350.0),
                                         ("220", 220.0), (" 60.5 ", 60.5)])
def test_a_setpoint_reads_as_a_number_or_as_its_text(value, want):
    assert temp.parse_target(value, "extruder") == want


@pytest.mark.parametrize("value", [True, False, "hot", "", "  ", None, [220], {},
                                   float("inf"), float("nan")],
                         ids=["bool-true", "bool-false", "word", "empty", "spaces", "missing",
                              "list", "dict", "infinity", "nan"])
def test_a_setpoint_that_is_not_a_number_refuses(value):
    with pytest.raises(PrinterError) as caught:
        temp.parse_target(value, "extruder")
    assert caught.value.details["reason"] == "temp_value"
    assert "Nothing was sent" in caught.value.message
    assert "degrees Celsius" in caught.value.message
    assert "plain number" in caught.value.hint


@pytest.mark.parametrize("field, value", [
    ("extruder", -1), ("extruder", 351), ("extruder", 600), ("extruder", 5000),
    ("heater_bed", -0.5), ("heater_bed", 110.5), ("heater_bed", 120), ("heater_bed", 320),
], ids=["nozzle-minus", "nozzle-351", "nozzle-firmware-600", "nozzle-absurd", "bed-minus",
        "bed-110.5", "bed-sdk-120", "bed-nozzle-number"])
def test_every_number_outside_the_heaters_own_window_refuses(field, value):
    with pytest.raises(PrinterError) as caught:
        temp.parse_target(value, field)
    assert caught.value.details["reason"] == "temp_range"
    assert "Nothing was sent" in caught.value.message
    assert "cannot be raised from here" in caught.value.hint


def test_the_two_ceiling_numbers_are_different_so_no_universal_number_was_assumed():
    # 320 is legal for the nozzle and illegal for the bed: the bounds are per heater, read off the
    # printer's own official control (nozzle 0-350, bed 0-110), not one number smeared over both
    assert temp.CEILING_C == {"extruder": 350.0, "heater_bed": 110.0}
    assert temp.parse_target(320, "extruder") == 320.0
    with pytest.raises(PrinterError):
        temp.parse_target(320, "heater_bed")


def test_a_refused_number_names_where_its_ceiling_came_from():
    # a caller who is told "351 is too high" needs to know whose number that is
    with pytest.raises(PrinterError) as caught:
        temp.parse_target(351, "extruder")
    assert "LAN web client release 20260625" in caught.value.message
    assert "not a hardware rating" in caught.value.message


# --------------------------------------------------------------------------- the wire frame


def test_the_frame_carries_one_heater_and_sends_whole_numbers_whole():
    # both first-party senders send whole setpoints whole (the integration sends int(temperature)),
    # so 220 goes out as 220; a genuine fraction goes out as a fraction, the shape the status fields
    # are documented as
    assert temp.temp_params("extruder", 220) == {"extruder": 220}
    assert temp.temp_params("heater_bed", "60") == {"heater_bed": 60}
    assert temp.temp_params("extruder", 60.5) == {"extruder": 60.5}
    assert temp.temp_params("extruder", 0) == {"extruder": 0}


@pytest.mark.parametrize("frame", [{"extruder": 220, "heater_bed": 60},   # both heaters at once
                                   {"extruder": 220, "fan": 9},           # a field that is not one
                                   {"nozzle": 220},                       # a label, not a field
                                   {}, [], None, 220],
                         ids=["both-heaters", "extra-field", "label-not-field", "empty",
                              "list", "nothing", "bare-number"])
async def test_a_frame_that_is_not_one_heater_never_reaches_the_wire(frame):
    tr = scripted()
    with pytest.raises((ValueError, PrinterError)):
        await temp.set_temperature(tr, "extruder", frame)
    with pytest.raises((ValueError, PrinterError)):
        await temp.read_preview(tr, "extruder", frame)
    assert tr.requests == [], "a frame this module will not send is not sent at all"


async def test_a_setpoint_handed_straight_to_the_adapter_is_still_held_to_the_ceiling():
    # the tool layer refuses first; this is the lock behind it, for any caller that skipped the tool
    tr = scripted()
    with pytest.raises(PrinterError):
        await temp.set_temperature(tr, "extruder", {"extruder": 600})
    assert tr.requests == []
    with pytest.raises(PrinterError):
        await temp.read_preview(tr, "heater_bed", {"heater_bed": 500})
    assert tr.requests == []


async def test_the_frame_must_be_for_the_heater_the_call_named():
    # a call for the bed that carries a nozzle field is a bug, not a second opinion
    tr = scripted()
    with pytest.raises(ValueError):
        await temp.set_temperature(tr, "heater_bed", {"extruder": 60})
    assert tr.requests == []


# --------------------------------------------------------------------------- the reading of a heater


def test_both_heaters_read_theirs_apart_from_one_status_frame():
    machine = st.Cc2MachineStatus.from_result(frame_1002())
    assert temp.temp_word(machine, "extruder") == (259.6, 260)
    assert temp.temp_word(machine, "heater_bed") == (69.9, 70)


@pytest.mark.parametrize("field", ["extruder", "heater_bed"])
def test_a_heater_the_frame_never_named_is_two_unknowns_never_two_zeroes(field):
    machine = st.Cc2MachineStatus.from_result(frame_1002(extruder=None, bed=None))
    assert temp.temp_word(machine, field) == (None, None)


def test_a_heater_block_without_a_target_reports_the_actual_and_an_unknown_target():
    machine = st.Cc2MachineStatus.from_result(frame_1002(extruder={"temperature": 24.3}))
    assert temp.temp_word(machine, "extruder") == (24.3, None)


async def test_a_heater_this_module_does_not_speak_is_a_programmer_error():
    tr = scripted()
    with pytest.raises(ValueError):
        await temp.set_temperature(tr, "chamber", {"chamber": 40})
    assert tr.requests == []


# --------------------------------------------------------------------------- the preview: read-only


async def test_the_preview_reads_status_once_and_never_sends_a_command():
    tr = FakeTempTransport({1002: frame_1002()})       # 1028 unscripted on purpose: reaching
    out = await temp.read_preview(tr, "extruder", {"extruder": 220})   # it would still be recorded
    assert tr.methods() == [1002], "the preview path sends the read-only 1002 and nothing else"
    assert out["confirm_required"] is True and out["action"] == "extruder_temperature"
    p = out["preview"]
    assert p["will_send"] == {"method": 1028, "params": {"extruder": 220}}
    assert p["waits_up_to_s"] == 10.0
    assert p["heater"] == "extruder" and p["ceiling_c"] == 350.0
    assert "LAN web client release 20260625" in p["limit_basis"]
    assert p["actual_now_c"] == 259.6 and p["target_now_c"] == 260
    assert p["heater_is_known"] is True
    assert "Nothing was sent to the printer" in out["how"] and "confirm=true" in out["how"]
    json.dumps(out)


async def test_the_preview_states_the_ceiling_it_enforced_and_where_it_came_from():
    tr = FakeTempTransport({1002: frame_1002()})
    out = await temp.read_preview(tr, "heater_bed", {"heater_bed": 60})
    note = " ".join(out["notes"])
    assert "110°C for the bed" in note
    assert "a hardware rating" in note and "max_temp" in note
    assert out["preview"]["ceiling_c"] == 110.0
    assert out["action"] == "bed_temperature"


@pytest.mark.parametrize("value, expect_note", [(350, True), (300.5, True), (300, False),
                                                (220, False), (0, False)])
async def test_only_an_extruder_number_above_the_other_sources_300_buys_the_disagreement_note(
        value, expect_note):
    # the tool does not choose between the two Elegoo sources on a hunch: below the SDK's 300 there
    # is nothing to flag, above it the disagreement is said out loud on every path
    tr = FakeTempTransport({1002: frame_1002()})
    out = await temp.read_preview(tr, "extruder", {"extruder": value})
    above = [n for n in out["notes"] if "two Elegoo sources disagree" in n]
    assert bool(above) is expect_note


async def test_a_zero_preview_says_the_heater_turns_off_rather_than_running_cold():
    tr = FakeTempTransport({1002: frame_1002()})
    out = await temp.read_preview(tr, "extruder", {"extruder": 0})
    assert any("heater is off" in n for n in out["notes"])
    assert out["preview"]["will_send"]["params"] == {"extruder": 0}


async def test_a_preview_of_a_heater_the_printer_never_reported_says_so():
    tr = FakeTempTransport({1002: frame_1002(extruder=None, bed=None)})
    out = await temp.read_preview(tr, "heater_bed", {"heater_bed": 60})
    p = out["preview"]
    assert p["actual_now_c"] is None and p["target_now_c"] is None
    assert p["heater_is_known"] is False


async def test_a_preview_that_cannot_read_the_printer_raises_instead_of_bluffing():
    tr = FakeTempTransport({1002: timeout()})
    with pytest.raises(PrinterError):
        await temp.read_preview(tr, "extruder", {"extruder": 220})
    assert tr.methods() == [1002]          # the failed read is not retried either


# --------------------------------------------------------------------------- the one command


@pytest.mark.parametrize(("field", "params"),
                         [("extruder", {"extruder": 220}), ("heater_bed", {"heater_bed": 60})])
async def test_each_heater_sends_its_one_frame_with_the_documented_wait(field, params):
    tr = scripted()
    result = await temp.set_temperature(tr, field, params)
    assert result.status is temp.ControlStatus.ACCEPTED and result.accepted
    sent = tr.calls(1028)
    assert len(sent) == 1 and sent[0][1] == params and sent[0][2] == 10.0
    assert result.waited_s == 10.0 and result.method == 1028 and result.sent == params
    assert result.heater == field
    # the one accepted-path follow-up: exactly one read-only 1002, and nothing else
    assert tr.methods() == [1028, 1002]


async def test_a_temperature_command_never_sends_any_other_method():
    tr = scripted()
    await temp.set_temperature(tr, "extruder", {"extruder": 220})
    await temp.set_temperature(tr, "heater_bed", {"heater_bed": 60})
    forbidden = {job.METHOD_PAUSE, job.METHOD_STOP, job.METHOD_RESUME,
                 1029, 1030, 1031, 1047, 1020}
    assert not (set(tr.methods()) & forbidden), tr.methods()


async def test_an_accepted_setpoint_reports_the_targets_and_actuals_separately():
    tr = scripted(status=frame_1002(extruder={"temperature": 212.4, "target": 220}))
    result = await temp.set_temperature(tr, "extruder", {"extruder": 220})
    assert result.accepted
    assert result.observed == {"observed_target_c": 220, "observed_actual_c": 212.4}
    assert result.ceiling_c == 350.0
    joined = " ".join(result.notes)
    assert "did not refuse" in joined and "never part of acceptance" in joined
    assert not any("disagreement" in n for n in result.notes)     # it agrees: nothing to flag
    # still climbing is not a complaint: a cold actual temperature says nothing about acceptance
    assert not any("disagree" in n for n in result.notes)


async def test_a_cold_actual_temperature_is_never_read_as_a_failure():
    tr = scripted(status=frame_1002(extruder={"temperature": 24.3, "target": 220}))
    result = await temp.set_temperature(tr, "extruder", {"extruder": 220})
    assert result.status is temp.ControlStatus.ACCEPTED and result.accepted
    assert result.observed["observed_actual_c"] == 24.3
    assert not any("disagreement" in n for n in result.notes)


async def test_an_accepted_setpoint_the_reported_target_disagrees_with_is_said_out_loud():
    # the printer answered error_code 0 and then reported its target as still 0: two facts, both
    # reported, neither overwriting the other, and above all no second command offered
    tr = scripted(status=frame_1002(extruder={"temperature": 24.3, "target": 0}))
    result = await temp.set_temperature(tr, "extruder", {"extruder": 220})
    assert result.status is temp.ControlStatus.ACCEPTED and result.accepted
    assert result.observed["observed_target_c"] == 0
    contra = [n for n in result.notes if "disagreement" in n]
    assert len(contra) == 1
    assert "accepted 220°C" in contra[0] and "reports the target as 0°C" in contra[0]
    assert len(tr.calls(1028)) == 1 and tr.methods().count(1002) == 1


@pytest.mark.parametrize("seen", [220, 220.4, 219.6])
async def test_the_stated_half_degree_tolerance_is_what_separates_agreement_from_disagreement(seen):
    tr = scripted(status=frame_1002(extruder={"temperature": 200.0, "target": seen}))
    result = await temp.set_temperature(tr, "extruder", {"extruder": 220})
    assert not any("disagreement" in n for n in result.notes)


async def test_a_setpoint_outside_the_tolerance_by_its_own_measure_is_a_disagreement():
    tr = scripted(status=frame_1002(bed={"temperature": 60.0, "target": 111}))
    result = await temp.set_temperature(tr, "heater_bed", {"heater_bed": 60})
    contra = [n for n in result.notes if "disagreement" in n]
    assert contra and "accepted 60°C" in contra[0] and "111°C" in contra[0] and "bed" in contra[0]


async def test_an_unanswerable_observation_is_no_contradiction_and_no_guess():
    tr = scripted(status=frame_1002(extruder=None, bed=None))
    result = await temp.set_temperature(tr, "heater_bed", {"heater_bed": 60})
    assert result.accepted
    assert result.observed == {"observed_target_c": None, "observed_actual_c": None}
    assert not any("disagreement" in n for n in result.notes)


async def test_observation_that_cannot_land_stays_an_honest_unknown():
    tr = scripted()
    tr.replies[1002] = timeout()           # the command answered 0; the one look afterwards fails
    result = await temp.set_temperature(tr, "extruder", {"extruder": 220})
    assert result.status is temp.ControlStatus.ACCEPTED and result.accepted
    assert result.observed is None
    assert any("unknown" in n for n in result.notes)
    assert len(tr.calls(1028)) == 1        # nothing was retried after the failed look


async def test_an_extruder_number_above_the_other_sources_300_is_said_to_be_disputed():
    # the tool sends it, because the printer's own control reaches this high, and it refuses to
    # imply that the disagreement about the rating was resolved
    tr = scripted(status=frame_1002(extruder={"temperature": 300.0, "target": 320}))
    result = await temp.set_temperature(tr, "extruder", {"extruder": 320})
    assert result.accepted
    assert any("two Elegoo sources disagree" in n for n in result.notes)
    assert temp._SDK_EXTRUDER_C == 300.0


async def test_a_bed_number_near_its_ceiling_buys_no_dispute_note():
    # the 300 dispute is about the extruder row only; the bed's two numbers are both 110
    tr = scripted(status=frame_1002(bed={"temperature": 60.0, "target": 110}))
    result = await temp.set_temperature(tr, "heater_bed", {"heater_bed": 110})
    assert result.accepted and result.ceiling_c == 110.0
    assert not any("two Elegoo sources disagree" in n for n in result.notes)


async def test_a_zero_command_says_what_a_zero_means():
    tr = scripted(status=frame_1002(extruder={"temperature": 150.0, "target": 0}))
    result = await temp.set_temperature(tr, "extruder", {"extruder": 0})
    assert result.accepted
    assert any("heater is off" in n for n in result.notes)
    assert result.observed["observed_target_c"] == 0


# --------------------------------------------------------------------------- refusals, kept on record


@pytest.mark.parametrize("code", [1010, 1012])
async def test_a_refusal_keeps_its_code_and_earns_no_further_read(code):
    tr = scripted(command={"error_code": code})
    result = await temp.set_temperature(tr, "extruder", {"extruder": 220})
    assert result.status is temp.ControlStatus.REFUSED and not result.accepted
    assert result.error_code == code
    assert str(code) in " ".join(result.notes)
    assert tr.methods() == [1028]          # an on-record refusal is not followed by reads


async def test_a_refusal_reads_like_the_job_controls_refusal():
    # one refusal, one kind of no: 1010 carries the documented 'no active print' gloss here too
    tr = scripted(command={"error_code": 1010})
    result = await temp.set_temperature(tr, "heater_bed", {"heater_bed": 60})
    joined = " ".join(result.notes)
    assert "no active print" in joined and "Nothing was resent" in joined


@pytest.mark.parametrize("answer", [{}, {"error_code": True}, {"error_code": "0"},
                                    {"error_code": 0.0}, {"error_code": None}],
                         ids=["empty", "bool-zero", "string-zero", "float-zero", "null-zero"])
async def test_answers_without_an_explicit_integer_are_ambiguous_never_retried(answer):
    tr = scripted(command=answer)
    result = await temp.set_temperature(tr, "extruder", {"extruder": 220})
    assert result.status is temp.ControlStatus.AMBIGUOUS and not result.accepted
    assert result.error_code is None
    assert tr.methods() == [1028]          # a reply with no readable meaning gets no second send
    assert any("neither success nor refusal" in n for n in result.notes)


# --------------------------------------------------------------------------- the lost answer


async def test_a_lost_answer_is_ambiguous_never_a_refusal_never_resent_with_one_look():
    tr = scripted(command=timeout(),
                  status=frame_1002(extruder={"temperature": 100.0, "target": 220}))
    result = await temp.set_temperature(tr, "extruder", {"extruder": 220})
    assert result.status is temp.ControlStatus.AMBIGUOUS and not result.accepted
    joined = " ".join(result.notes)
    assert "neither a refusal nor a failure" in joined and "nothing was retried" in joined
    assert len(tr.calls(1028)) == 1, "a lost answer never becomes a resend"
    assert tr.methods() == [1028, 1002]    # exactly the one read-only look the ambiguity allows
    assert result.observed["observed_target_c"] == 220   # what it says rides along, unspun


async def test_a_lost_answer_whose_look_also_fails_is_still_one_send_and_an_unknown():
    tr = scripted(command=timeout())
    tr.replies[1002] = timeout()
    result = await temp.set_temperature(tr, "heater_bed", {"heater_bed": 60})
    assert result.status is temp.ControlStatus.AMBIGUOUS and not result.accepted
    assert result.observed is None
    assert any("unknown" in n for n in result.notes)
    assert len(tr.calls(1028)) == 1


async def test_a_dead_session_is_ambiguous_and_asks_that_session_for_nothing_further():
    dead = PrinterError("not_reachable", "simulated: the session ended")
    tr = scripted(command=dead)
    result = await temp.set_temperature(tr, "extruder", {"extruder": 220})
    assert result.status is temp.ControlStatus.AMBIGUOUS and not result.accepted
    assert tr.methods() == [1028]          # no status read a dead session could not carry
    assert any("nothing was resent" in n for n in result.notes)


# --------------------------------------------------------------------------- one honesty, shared


def test_the_temperature_control_uses_the_job_controls_vocabulary_verbatim():
    # the whole point of importing the words: a model reading a setpoint result and a pause result
    # gets one vocabulary for accepted, refused, unreadable and lost. Drift here is a test failure.
    assert temp.ControlStatus is job.ControlStatus
    for name in ("ACCEPT_BASE", "LOST_NOTE", "UNREADABLE_NOTE", "DEAD_NOTE", "OBSERVE_LOST_NOTE",
                 "NO_OBSERVE_AFTER_REFUSAL", "PREVIEW_HOW"):
        assert getattr(temp.job, name) is getattr(job, name), name
    assert temp.job.plain_int is job.plain_int
    assert temp.job.refusal_note is job.refusal_note


def test_this_module_speaks_exactly_one_method_and_two_heaters():
    # 1028 is the whole vocabulary, with the two heaters the protocol can command: the fan (1030)
    # is still out because the first-party sources disagree about its scale, and the chamber has no
    # heater to command, so the guard is that this module cannot send either
    assert temp.METHOD_SET_TEMPERATURE == 1028
    assert sorted(temp._FIELDS) == ["extruder", "heater_bed"]
    assert sorted(temp._HEATERS) == ["bed", "extruder", "heater_bed", "nozzle"]
    assert not hasattr(temp, "METHOD_SET_FAN") and not hasattr(temp, "METHOD_SET_LIGHT")


def test_the_module_says_which_sources_it_read_and_what_it_refused_to_invent():
    doc = " ".join((temp.__doc__ or "").split())
    assert "max_temp" in doc and "heaters.cpp" in doc
    assert "no temperature-ceiling key" in doc       # the slicer profiles offer nothing
    assert "editable: false" in doc                  # the chamber row is a reading


# --------------------------------------------------------------------------- shape and privacy


async def test_the_result_dict_is_plain_json_and_keeps_the_facts_apart():
    tr = scripted(status=frame_1002(extruder={"temperature": 212.4, "target": 220}))
    result = await temp.set_temperature(tr, "extruder", {"extruder": 220})
    d = result.as_dict()
    assert set(d) == {"heater", "method", "sent", "status", "accepted", "error_code", "waited_s",
                      "ceiling_c", "limit_basis", "observed_target_c", "observed_actual_c",
                      "notes"}
    assert d["heater"] == "extruder" and d["method"] == 1028 and d["status"] == "accepted"
    assert d["sent"] == {"extruder": 220} and d["accepted"] is True and d["error_code"] is None
    assert d["waited_s"] == 10.0 and d["ceiling_c"] == 350.0
    assert d["observed_target_c"] == 220 and d["observed_actual_c"] == 212.4
    assert "not a hardware rating" in d["limit_basis"]
    json.dumps(d)


async def test_every_note_of_every_outcome_fits_the_reply_cap():
    seen_any = False
    for command in ({"error_code": 0}, {"error_code": 1010}, {}, timeout()):
        for field, params in (("extruder", {"extruder": 350}), ("extruder", {"extruder": 0}),
                              ("heater_bed", {"heater_bed": 110})):
            for status in (frame_1002(extruder={"temperature": 24.3, "target": 0}, bed=None),
                           frame_1002(extruder=None, bed=None),
                           frame_1002(extruder={"temperature": 349.9, "target": 350},
                                       bed={"temperature": 109.9, "target": 110})):
                tr = scripted(command=command, status=status)
                result = await temp.set_temperature(tr, field, params)
                for n in result.notes:
                    seen_any = True
                    assert len(n) <= job.NOTE_CAP, (field, command, n[:80], len(n))
                for n in (await temp.read_preview(tr, field, params))["notes"]:
                    seen_any = True
                    assert len(n) <= job.NOTE_CAP, (field, n[:80], len(n))
    assert seen_any


async def test_no_address_no_token_rides_any_reply():
    tr = scripted(status=frame_1002())
    texts = []
    for field, params in (("extruder", {"extruder": 320}), ("heater_bed", {"heater_bed": 60})):
        result = await temp.set_temperature(tr, field, params)
        texts.append(json.dumps(result.as_dict()))
        texts.append(json.dumps(await temp.read_preview(tr, field, params)))
    blob = "\n".join(texts)
    assert HOST not in blob and FAKE_TOKEN not in blob and SN not in blob


@pytest.mark.parametrize("bad", ["chamber", 351, -1, "hot", True, None, {}],
                         ids=["chamber", "nozzle-351", "minus", "word", "bool", "missing", "dict"])
def test_a_refusal_dict_is_plain_json_and_names_its_reason(bad):
    for call in (lambda: temp.parse_heater(bad), lambda: temp.parse_target(bad, "heater_bed")):
        with pytest.raises(PrinterError) as caught:
            call()
    out = caught.value.as_dict()
    assert out["error"] == "protocol_error"
    assert out["reason"] in {"heater_name", "temp_value", "temp_range"}
    assert len(out["message"]) <= job.NOTE_CAP and len(out["hint"]) <= job.NOTE_CAP
    json.dumps(out)
