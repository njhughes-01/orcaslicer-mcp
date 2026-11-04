"""The two CC2 peripheral controls (1029 light, 1031 print speed mode) against scripted replies, in
the same house style as the job-control tests: nothing opens a socket, the transport is a fake that
records every method and the timeout each request asked for. The 1002 frames are the observed idle
and printing shapes from test_cc2_state/test_cc2_start with the two peripheral blocks added exactly
as the protocol doc's full-status sample spells them (``led.status`` and
``gcode_move_inf.speed_mode``) — marked doc-derived for those two fields, because this adapter has
not observed a CC2 answering them live. What the tests pin down:

* the argument bar — mode is the printer's own discrete 0..3 (whole-number text reads, a
  percentage, a float, a bool, a name and 4 do not), and the light is exactly True/False;
* the wire shape the sources actually show — 1029 carries the web client's ``power`` paired with the
  doc table's ``brightness`` so the two can never disagree, 1031 carries ``mode`` alone, and neither
  command ever sends a job method (1021/1022/1023) or a fan/temperature method (1030/1028);
* the preview sends the read-only 1002 and nothing else, and shows this call's exact params;
* accepted / refused / ambiguous are kept apart, only an explicit integer error_code 0 is accepted,
  a refusal keeps its code and earns no follow-up read, and a lost or unreadable answer is never
  called a refusal and never resent — the one allowed follow-up is a single read-only 1002;
* the read-back is a fact of its own: an accepted command whose status look disagrees says so, an
  absent led/speed_mode is None rather than the other value (never the integration's default of 1),
  and a number outside the documented four modes is reported as that number with no name for it;
* every note survives the direct tools' reply cap whole, and no address, serial or token rides in
  any reply."""
from __future__ import annotations

import copy
import json
from collections import deque

import pytest

from orcaslicer_mcp.printer import cc2_control as job
from orcaslicer_mcp.printer import cc2_peripherals as per
from orcaslicer_mcp.printer import cc2_state as st
from orcaslicer_mcp.printer.cc2_transport import Cc2TimeoutError, PrinterError

HOST = "192.0.2.10"                  # documented TEST-NET, like the whole lane; never a live address
SN = "CC2TESTSN0001"
FAKE_TOKEN = "SIMULATED-NOT-A-CODE"  # a stand-in credential, proven never to ride any reply
JOB = "benchy-petg-2026-11-04.gcode"

# The observed 1002 shapes (test_cc2_state / test_cc2_start) plus the two peripheral blocks spelled
# exactly as the protocol doc's full-status sample spells them: led.status 1 for the light and
# gcode_move_inf.speed_mode for the mode. doc-derived for these two fields, observed for the rest.
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
}


def frame_1002(*, led=..., mode=..., state="printing") -> dict:
    """A 1002 as this firmware answers it, with the two peripheral fields set (or removed with
    ``None``) to whatever a test needs. ``...`` leaves the frame's own value alone."""
    frame = copy.deepcopy(BASE_1002)
    frame["print_status"]["state"] = state
    if led is None:
        frame.pop("led")
    elif led is not ...:
        frame["led"] = {"status": led}
    if mode is None:
        frame["gcode_move_inf"].pop("speed_mode")
    elif mode is not ...:
        frame["gcode_move_inf"]["speed_mode"] = mode
    return frame


class FakePeripheralTransport:
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


def timeout() -> PrinterError:
    """The transport's own honest lost-reply error — the reason a setting must come back ambiguous
    and never resent."""
    return Cc2TimeoutError("protocol_error", "simulated: the printer never answered",
                           reason="timeout", ambiguous=True)


def scripted(*, status=None, command=None) -> FakePeripheralTransport:
    """The 1002 answers ``status`` (default: light on, mode 1), and 1029/1031 both answer
    ``command`` (default the documented explicit success) — every method recorded either way, so a
    send the tests did not expect is still a recorded fact."""
    command_reply = {"error_code": 0} if command is None else command
    return FakePeripheralTransport({1002: frame_1002() if status is None else status,
                                    1029: command_reply, 1031: command_reply})


# --------------------------------------------------------------------------- the argument bar


@pytest.mark.parametrize("value, want", [(0, 0), (1, 1), (2, 2), (3, 3), ("2", 2), (" 3 ", 3)])
def test_the_four_modes_read_however_they_are_spelled(value, want):
    assert per.parse_mode(value) == want


@pytest.mark.parametrize("bad", [4, -1, 100, 50, 2.0, True, False, "fast", "mode2", "", None],
                         ids=["four", "minus", "percent-100", "percent-50", "float", "bool-true",
                              "bool-false", "name", "prefixed", "empty", "missing"])
def test_anything_that_is_not_one_of_the_four_modes_refuses(bad):
    # a percentage (100, 50) is the mistake this bar exists for: the CC2 has no percentage, and a
    # silent 100 -> 3 would change how a print runs on a guess
    with pytest.raises(PrinterError) as caught:
        per.parse_mode(bad)
    assert caught.value.code == "protocol_error"
    assert caught.value.details["reason"] == "mode_range"
    assert "Nothing was sent" in caught.value.message
    assert "0 Silent" in caught.value.hint and "3 Ludicrous" in caught.value.hint


@pytest.mark.parametrize("value", [True, False])
def test_the_light_takes_exactly_a_boolean(value):
    assert per.parse_light(value) is value


@pytest.mark.parametrize("bad", ["on", "off", "true", 1, 0, 255, "", None, 1.0],
                         ids=["on", "off", "text-true", "one", "zero", "brightness", "empty",
                              "missing", "float"])
def test_a_value_that_merely_looks_like_yes_or_no_refuses_the_light(bad):
    with pytest.raises(PrinterError) as caught:
        per.parse_light(bad)
    assert caught.value.details["reason"] == "light_argument"
    assert "Nothing was sent" in caught.value.message
    assert "power 1" in caught.value.hint and "not" in caught.value.hint


def test_the_light_params_pair_both_spellings_so_they_can_never_disagree():
    # the web client's power and the doc table's brightness, paired: firmware reading either one
    # hears the same thing, and no combination can mean on and off at once
    assert per.light_params(True) == {"power": 1, "brightness": 255}
    assert per.light_params(False) == {"power": 0, "brightness": 0}


def test_the_speed_params_carry_the_mode_and_nothing_else():
    assert per.speed_params(2) == {"mode": 2}
    with pytest.raises(PrinterError):
        per.speed_params(4)


@pytest.mark.parametrize("frame", per._LIGHT_FRAMES)
def test_a_hand_rolled_frame_is_accepted_only_when_it_is_exactly_the_paired_one(frame):
    assert per.checked_params("light", frame) == frame


@pytest.mark.parametrize("bad", [{"power": 1}, {"power": 1, "brightness": 0},       # half a pair:
                                 {"brightness": 255}, {"power": 2, "brightness": 2},  # a contradiction
                                 {"power": 1, "brightness": 255, "fan": 9}, [], None,
                                 {"mode": 2}],
                         ids=["power-only", "opposite-pair", "brightness-only", "other-numbers",
                              "extra-field", "not-a-frame", "nothing", "wrong-setting"])
async def test_a_light_frame_that_is_not_the_documented_pair_never_reaches_the_wire(bad):
    tr = scripted()
    with pytest.raises((ValueError, PrinterError)):
        await per.set_peripheral(tr, "light", bad)
    with pytest.raises((ValueError, PrinterError)):
        await per.read_preview(tr, "light", bad)
    assert tr.requests == [], "a frame this module will not send is not sent at all"


async def test_a_mode_handed_straight_to_the_adapter_is_still_held_to_the_four():
    # the tool layer refuses first; this is the lock behind it, for any caller that skipped the tool
    tr = scripted()
    with pytest.raises(PrinterError):
        await per.set_peripheral(tr, "speed", {"mode": 47})
    assert tr.requests == []
    with pytest.raises(PrinterError):
        await per.read_preview(tr, "speed", {"mode": 47})
    assert tr.requests == []


# --------------------------------------------------------------------------- the two readings


@pytest.mark.parametrize("led, on, raw", [(1, True, 1), (0, False, 0), (200, True, 200),
                                          (-1, None, -1)])
def test_led_status_reads_on_above_zero_off_at_zero(led, on, raw):
    machine = st.Cc2MachineStatus.from_result(frame_1002(led=led))
    assert per.light_word(machine) == (on, raw)


def test_an_absent_led_is_unknown_never_off():
    machine = st.Cc2MachineStatus.from_result(frame_1002(led=None))
    assert per.light_word(machine) == (None, None)


@pytest.mark.parametrize("mode, name", [(0, "Silent"), (1, "Balanced"), (2, "Sport"),
                                        (3, "Ludicrous"), (7, None)])
def test_a_reported_mode_keeps_its_name_or_loses_it_without_a_guess(mode, name):
    machine = st.Cc2MachineStatus.from_result(frame_1002(mode=mode))
    assert per.speed_word(machine) == (mode, name)


def test_an_absent_speed_mode_is_unknown_never_the_integration_default_of_one():
    # the Home Assistant integration defaults an absent speed_mode to 1; that is inventing a state,
    # so here an absent field is no reading at all
    machine = st.Cc2MachineStatus.from_result(frame_1002(mode=None))
    assert per.speed_word(machine) == (None, None)


# --------------------------------------------------------------------------- the preview: read-only


async def test_the_preview_reads_status_once_and_never_sends_a_command():
    tr = FakePeripheralTransport({1002: frame_1002()})   # 1029/1031 unscripted on purpose:
    out = await per.read_preview(tr, "light", per.light_params(True))   # reaching one is recorded
    assert tr.methods() == [1002], "the preview path sends the read-only 1002 and nothing else"
    assert out["confirm_required"] is True and out["action"] == "light"
    p = out["preview"]
    assert p["will_send"] == {"method": 1029, "params": {"power": 1, "brightness": 255}}
    assert p["waits_up_to_s"] == 10.0
    assert p["light_on_now"] is True and p["led_status_now"] == 1 and p["light_is_known"] is True
    assert "Nothing was sent to the printer" in out["how"] and "confirm=true" in out["how"]
    json.dumps(out)


async def test_the_preview_shows_this_call_s_own_params_not_a_general_shape():
    tr = FakePeripheralTransport({1002: frame_1002(led=0)})
    out = await per.read_preview(tr, "light", per.light_params(False))
    assert out["preview"]["will_send"]["params"] == {"power": 0, "brightness": 0}
    assert out["preview"]["light_on_now"] is False     # off now, asked to stay off: both said


async def test_the_speed_preview_names_the_mode_it_would_send_and_the_one_now():
    tr = FakePeripheralTransport({1002: frame_1002(mode=1)})
    out = await per.read_preview(tr, "speed", {"mode": 3})
    p = out["preview"]
    assert p["will_send"] == {"method": 1031, "params": {"mode": 3}}
    assert p["waits_up_to_s"] == 10.0
    assert p["mode_now"] == 1 and p["mode_name_now"] == "Balanced" and p["mode_is_known"] is True


async def test_a_preview_of_a_setting_the_printer_never_reported_says_so():
    tr = FakePeripheralTransport({1002: frame_1002(led=None, mode=None)})
    light = await per.read_preview(tr, "light", per.light_params(True))
    speed = await per.read_preview(tr, "speed", {"mode": 0})
    assert light["preview"]["light_on_now"] is None and light["preview"]["light_is_known"] is False
    assert speed["preview"]["mode_now"] is None and speed["preview"]["mode_is_known"] is False


async def test_a_preview_that_cannot_read_the_printer_raises_instead_of_bluffing():
    tr = FakePeripheralTransport({1002: timeout()})
    with pytest.raises(PrinterError):
        await per.read_preview(tr, "light", per.light_params(True))
    assert tr.methods() == [1002]          # the failed read is not retried either


@pytest.mark.parametrize("bad_setting", ["fan", "temperature", "LIGHT", ""])
async def test_a_setting_this_module_does_not_speak_is_a_programmer_error(bad_setting):
    tr = FakePeripheralTransport()
    with pytest.raises(ValueError):
        await per.set_peripheral(tr, bad_setting, {"power": 1})
    with pytest.raises(ValueError):
        await per.read_preview(tr, bad_setting, {"power": 1})
    assert tr.requests == []


# --------------------------------------------------------------------------- the one command


@pytest.mark.parametrize(("setting", "method", "params"),
                         [("light", 1029, {"power": 1, "brightness": 255}),
                          ("speed", 1031, {"mode": 2})])
async def test_each_setting_sends_its_one_method_with_the_documented_params_and_wait(setting,
                                                                                      method,
                                                                                      params):
    tr = scripted()
    result = await per.set_peripheral(tr, setting, params)
    assert result.status is per.ControlStatus.ACCEPTED and result.accepted
    sent = tr.calls(method)
    assert len(sent) == 1 and sent[0][1] == params and sent[0][2] == 10.0
    assert result.waited_s == 10.0 and result.method == method and result.sent == params
    # the one accepted-path follow-up: exactly one read-only 1002, and nothing else
    assert tr.methods() == [method, 1002]


async def test_a_peripheral_command_never_sends_a_job_fan_or_heater_method():
    tr = scripted()
    await per.set_peripheral(tr, "light", per.light_params(True))
    await per.set_peripheral(tr, "speed", {"mode": 0})
    forbidden = {job.METHOD_PAUSE, job.METHOD_STOP, job.METHOD_RESUME, 1028, 1030}
    assert not (set(tr.methods()) & forbidden), tr.methods()


async def test_an_accepted_light_reports_what_the_printer_says_separately():
    tr = scripted(status=frame_1002(led=1))
    result = await per.set_peripheral(tr, "light", per.light_params(True))
    assert result.accepted
    assert result.observed == {"observed_light_on": True, "observed_led_status": 1}
    joined = " ".join(result.notes)
    assert "did not refuse" in joined and "never part of acceptance" in joined
    assert not any("disagreement" in n for n in result.notes)   # it agrees: nothing to flag


async def test_an_accepted_command_the_status_disagrees_with_is_said_out_loud():
    # the printer answered error_code 0 and then reported the light off: two facts, both reported,
    # neither overwriting the other, and above all no second command offered
    tr = scripted(status=frame_1002(led=0))
    result = await per.set_peripheral(tr, "light", per.light_params(True))
    assert result.status is per.ControlStatus.ACCEPTED and result.accepted
    assert result.observed["observed_light_on"] is False
    contra = [n for n in result.notes if "disagreement" in n]
    assert len(contra) == 1 and "reports the light off" in contra[0]
    assert len(tr.calls(1029)) == 1 and tr.methods().count(1002) == 1


async def test_the_same_disagreement_for_the_speed_mode_names_both_numbers():
    tr = scripted(status=frame_1002(mode=1))
    result = await per.set_peripheral(tr, "speed", {"mode": 3})
    assert result.accepted and result.observed["observed_speed_mode"] == 1
    contra = [n for n in result.notes if "disagreement" in n]
    assert contra and "accepted mode 3" in contra[0] and "reports mode 1" in contra[0]


async def test_a_mode_outside_the_documented_four_is_reported_as_itself_with_no_name():
    tr = scripted(status=frame_1002(mode=7))
    result = await per.set_peripheral(tr, "speed", {"mode": 2})
    assert result.accepted
    assert result.observed == {"observed_speed_mode": 7, "observed_speed_name": None}
    # 7 is not 2, so the disagreement note is said, and it names the number rather than a name
    assert any("reports mode 7" in n for n in result.notes)


async def test_an_unanswerable_observation_is_no_contradiction_and_no_guess():
    tr = scripted(status=frame_1002(led=None))
    result = await per.set_peripheral(tr, "light", per.light_params(True))
    assert result.accepted
    assert result.observed == {"observed_light_on": None, "observed_led_status": None}
    assert not any("disagreement" in n for n in result.notes)


async def test_observation_that_cannot_land_stays_an_honest_unknown():
    tr = scripted()
    tr.replies[1002] = timeout()           # the command answered 0; the one look afterwards fails
    result = await per.set_peripheral(tr, "speed", {"mode": 2})
    assert result.status is per.ControlStatus.ACCEPTED and result.accepted
    assert result.observed is None
    assert any("unknown" in n for n in result.notes)
    assert len(tr.calls(1031)) == 1        # nothing was retried after the failed look


# --------------------------------------------------------------------------- refusals, kept on record


@pytest.mark.parametrize("code", [1010, 1012])
async def test_a_refusal_keeps_its_code_and_earns_no_further_read(code):
    tr = scripted(command={"error_code": code})
    result = await per.set_peripheral(tr, "light", per.light_params(True))
    assert result.status is per.ControlStatus.REFUSED and not result.accepted
    assert result.error_code == code
    assert str(code) in " ".join(result.notes)
    assert tr.methods() == [1029]          # an on-record refusal is not followed by reads


async def test_a_refusal_reads_like_the_job_controls_refusal():
    # one refusal, one kind of no: 1010 carries the documented 'no active print' gloss here too
    tr = scripted(command={"error_code": 1010})
    result = await per.set_peripheral(tr, "speed", {"mode": 0})
    joined = " ".join(result.notes)
    assert "no active print" in joined and "Nothing was resent" in joined


@pytest.mark.parametrize("answer", [{}, {"error_code": True}, {"error_code": "0"},
                                    {"error_code": 0.0}, {"error_code": None}],
                         ids=["empty", "bool-zero", "string-zero", "float-zero", "null-zero"])
async def test_answers_without_an_explicit_integer_are_ambiguous_never_retried(answer):
    tr = scripted(command=answer)
    result = await per.set_peripheral(tr, "speed", {"mode": 2})
    assert result.status is per.ControlStatus.AMBIGUOUS and not result.accepted
    assert result.error_code is None
    assert tr.methods() == [1031]          # a reply with no readable meaning gets no second send
    assert any("neither success nor refusal" in n for n in result.notes)


# --------------------------------------------------------------------------- the lost answer


async def test_a_lost_answer_is_ambiguous_never_a_refusal_never_resent_with_one_look():
    tr = scripted(command=timeout(), status=frame_1002(mode=2))
    result = await per.set_peripheral(tr, "speed", {"mode": 2})
    assert result.status is per.ControlStatus.AMBIGUOUS and not result.accepted
    joined = " ".join(result.notes)
    assert "neither a refusal nor a failure" in joined and "nothing was retried" in joined
    assert len(tr.calls(1031)) == 1, "a lost answer never becomes a resend"
    assert tr.methods() == [1031, 1002]    # exactly the one read-only look the ambiguity allows
    assert result.observed["observed_speed_mode"] == 2   # what it says rides along, unspun


async def test_a_lost_answer_whose_look_also_fails_is_still_one_send_and_an_unknown():
    tr = scripted(command=timeout())
    tr.replies[1002] = timeout()
    result = await per.set_peripheral(tr, "light", per.light_params(False))
    assert result.status is per.ControlStatus.AMBIGUOUS and not result.accepted
    assert result.observed is None
    assert any("unknown" in n for n in result.notes)
    assert len(tr.calls(1029)) == 1


async def test_a_dead_session_is_ambiguous_and_asks_that_session_for_nothing_further():
    dead = PrinterError("not_reachable", "simulated: the session ended")
    tr = scripted(command=dead)
    result = await per.set_peripheral(tr, "light", per.light_params(True))
    assert result.status is per.ControlStatus.AMBIGUOUS and not result.accepted
    assert tr.methods() == [1029]          # no status read a dead session could not carry
    assert any("nothing was resent" in n for n in result.notes)


# --------------------------------------------------------------------------- one honesty, shared


def test_the_peripheral_controls_use_the_job_controls_vocabulary_verbatim():
    # the whole point of importing the words: a model reading a light result and a pause result gets
    # one vocabulary for accepted, refused, unreadable and lost. Drift here is a test failure.
    assert per.ControlStatus is job.ControlStatus
    assert job.NOTE_CAP == job.NOTE_CAP
    for name in ("ACCEPT_BASE", "LOST_NOTE", "UNREADABLE_NOTE", "DEAD_NOTE", "OBSERVE_LOST_NOTE",
                 "NO_OBSERVE_AFTER_REFUSAL", "PREVIEW_HOW"):
        assert getattr(per.job, name) is getattr(job, name), name
    assert per.job.plain_int is job.plain_int
    assert per.job.refusal_note is job.refusal_note


def test_this_module_speaks_exactly_two_methods():
    # the revised plan bars the fan (its scale is disputed between the two first-party sources), so
    # the guard is that this module cannot send that command at all. Temperature is not a gap any
    # more — it has its own module now, with a ceiling read out of Elegoo's own sources — but this
    # module still does not send it, and where the fan's evidence stands is still said out loud
    assert per._METHODS == {"light": 1029, "speed": 1031}
    assert not hasattr(per, "METHOD_SET_FAN") and not hasattr(per, "METHOD_SET_TEMPERATURE")
    doc = " ".join((per.__doc__ or "").split())
    assert "1030 SET_FAN_SPEED" in doc and "does not guess a scale" in doc
    assert "1028 SET_TEMPERATURE" in doc and "own module" in doc
    assert "deliberately NOT here" in doc


# --------------------------------------------------------------------------- shape and privacy


async def test_the_result_dict_is_plain_json_and_keeps_the_facts_apart():
    tr = scripted(status=frame_1002(mode=2))
    result = await per.set_peripheral(tr, "speed", {"mode": 2})
    d = result.as_dict()
    assert set(d) == {"setting", "method", "sent", "status", "accepted", "error_code", "waited_s",
                      "observed_speed_mode", "observed_speed_name", "notes"}
    assert d["setting"] == "speed" and d["method"] == 1031 and d["status"] == "accepted"
    assert d["sent"] == {"mode": 2} and d["accepted"] is True and d["error_code"] is None
    assert d["waited_s"] == 10.0 and d["observed_speed_name"] == "Sport"
    json.dumps(d)


async def test_every_note_of_every_outcome_fits_the_reply_cap():
    seen_any = False
    for command in ({"error_code": 0}, {"error_code": 1010}, {}, timeout()):
        for setting, params in (("light", per.light_params(True)), ("speed", {"mode": 3})):
            for status in (frame_1002(led=0, mode=0), frame_1002(led=None, mode=None),
                           frame_1002(led=1, mode=7)):
                tr = scripted(command=command, status=status)
                result = await per.set_peripheral(tr, setting, params)
                for n in result.notes:
                    seen_any = True
                    assert len(n) <= job.NOTE_CAP, (setting, command, n)
    assert seen_any


async def test_no_address_no_token_rides_any_reply():
    tr = scripted(status=frame_1002())
    texts = []
    for setting, params in (("light", per.light_params(True)), ("speed", {"mode": 1})):
        result = await per.set_peripheral(tr, setting, params)
        texts.append(json.dumps(result.as_dict()))
        texts.append(json.dumps(await per.read_preview(tr, setting, params)))
    blob = "\n".join(texts)
    assert HOST not in blob and FAKE_TOKEN not in blob and SN not in blob
