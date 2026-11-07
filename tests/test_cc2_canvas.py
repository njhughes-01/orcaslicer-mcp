"""The CC2 CANVAS auto-refill command (2004) against scripted replies, in the house style of the
other CC2 control tests: nothing opens a socket, the transport is a fake that records every method and
the timeout each request asked for, and what the tests pin down is narrow on purpose — the acceptance
vocabulary, the note wording and the preview shape are ``cc2_control``'s and
``tests/test_cc2_peripherals.py`` already proves them, so what is proved here is what belongs to this
command alone:

* the wire shape Elegoo's own LAN client shows: ``{method: 2004, params: {"auto_refill": <bool>}}``,
  one field, and **the older integration manual's ``enable`` spelling proven never to leave**, in any
  combination, with no other CANVAS field riding along (2003 and the tray/canvas ids are a different
  command's business, so a switch call cannot move what the printer prints from);
* the read-back is 2005 ``canvas_info.auto_refill`` and preserves unknown: an absent field, a refused
  read and an unreadable value each stay unknown with the raw value kept, while 0 and 1 read as the
  numbers they are — the ``false`` that ``cc2_state``'s status reader would have defaulted to is
  never reported as a reading;
* the one documented deviation from the light and the speed mode: a CANVAS read that answers nothing
  still gets its preview, because a printer with no CANVAS attached is a legal state for this switch;
* accepted / refused / ambiguous for this command, exactly once on the recorded wire, with the single
  read-only 2005 as the only follow-up an accepted or unanswered command may cause and no read at all
  after an on-record refusal;
* every reply says the command has never met a CC2 (``verified_on_this_printer: false`` plus its
  sentence), every note fits the direct tools' reply cap, and no address, serial or token rides in a
  reply. Nothing here is claimed about a real printer: every case is a scripted reply."""
from __future__ import annotations

import copy
import json
from collections import deque

import pytest

from orcaslicer_mcp.printer import cc2_canvas as cv
from orcaslicer_mcp.printer import cc2_control as job
from orcaslicer_mcp.printer import cc2_state as st
from orcaslicer_mcp.printer.cc2_transport import Cc2TimeoutError, PrinterError

HOST = "192.0.2.10"                  # documented TEST-NET, like the whole lane; never a live address
SN = "CC2TESTSN0001"
FAKE_TOKEN = "SIMULATED-NOT-A-CODE"  # a stand-in credential, proven never to ride any reply

# The smallest 2005 shape this module reads: the flag inside canvas_info. Tray parsing is
# cc2_state's job and is tested in tests/test_cc2_state.py against the observed four-tray reply;
# this module reads one field and nothing else, which is exactly what the tests below hold it to.
CANVAS = {"error_code": 0, "canvas_info": {"active_canvas_id": 0, "active_tray_id": -1,
                                           "auto_refill": False, "canvas_list": [{}]}}


def frame_2005(auto_refill=..., **over) -> dict:
    """A 2005 reply with the flag set to whatever a test needs. ``auto_refill=None`` removes the
    field entirely (the case a status reader would default to false and a read-back may not)."""
    frame = copy.deepcopy(CANVAS)
    if auto_refill is None:
        frame["canvas_info"].pop("auto_refill")
    elif auto_refill is not ...:
        frame["canvas_info"]["auto_refill"] = auto_refill
    frame["canvas_info"].update(over)
    return frame


class FakeCanvasTransport:
    """Stands in for the registered session as ``Cc2Transport`` behaves: records every request with
    the timeout it was given and answers from a method -> script map (a deque shifts, last value
    sticks, exceptions raise as given, an unscripted method answers {}). The module under test is the
    thing that must not ask twice, so nothing here ever refuses twice on its own."""

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
    return Cc2TimeoutError("protocol_error", "simulated: the printer never answered",
                           reason="timeout", ambiguous=True)


def scripted(*, canvas=None, command=None) -> FakeCanvasTransport:
    """2005 answers ``canvas`` (default: the flag off) and 2004 answers ``command`` (default the
    explicit success every source agrees means "the printer did not refuse")."""
    return FakeCanvasTransport({2005: frame_2005() if canvas is None else canvas,
                                2004: {"error_code": 0} if command is None else command})


# --------------------------------------------------------------------------- the argument bar


@pytest.mark.parametrize("value", [True, False])
def test_the_switch_takes_exactly_a_boolean(value):
    assert cv.parse_refill(value) is value
    assert cv.checked_params({"auto_refill": value}) == {"auto_refill": value}


@pytest.mark.parametrize("bad", ["on", "off", "true", 1, 0, 2, "", None, 1.0, [True]],
                         ids=["on", "off", "text-true", "one", "zero", "two", "empty", "missing",
                              "float", "list"])
def test_a_value_that_merely_looks_like_yes_or_no_refuses(bad):
    with pytest.raises(PrinterError) as caught:
        cv.parse_refill(bad)
    assert caught.value.code == "protocol_error"
    assert caught.value.details["reason"] == "refill_argument"
    assert "Nothing was sent" in caught.value.message
    assert "auto_refill" in caught.value.hint and "enable" in caught.value.hint


def test_the_command_carries_the_one_field_elegoo_s_own_client_sends():
    # the printer's own client sends params:{auto_refill:<bool>} and answers the same name in its own
    # canvas_info, so one field, under that name, is the whole documented command
    assert cv.METHOD_SET_AUTO_REFILL == 2004
    assert cv.refill_params(True) == {"auto_refill": True}
    assert cv.refill_params(False) == {"auto_refill": False}
    assert cv.SET_WAIT_S == 10.0


@pytest.mark.parametrize("frame", [{"enable": True}, {"enable": False},
                                   {"auto_refill": True, "enable": True},
                                   {"auto_refill": True, "tray_id": 2},
                                   {"auto_refill": True, "active_tray_id": 0},
                                   {"auto_refill": "yes"}, {"auto_refill": 1}, {}, "true", None],
                         ids=["enable-only", "enable-off", "both-spellings", "tray-rides-along",
                              "active-tray-rides-along", "not-a-bool", "int-not-bool", "empty",
                              "text", "nothing"])
async def test_any_other_frame_refuses_before_the_wire_with_the_manual_s_own_spelling_named(frame):
    # enable is the older integration manual's spelling, its own sample and Elegoo's own client both
    # say auto_refill, so this module sends one field; pairing them "for compatibility" would be
    # offering this printer an undocumented field no Elegoo client has ever sent
    tr = scripted()
    with pytest.raises((ValueError, PrinterError)):
        await cv.set_auto_refill(tr, frame)
    with pytest.raises((ValueError, PrinterError)):
        await cv.read_preview(tr, frame)
    assert tr.requests == [], "a frame this module will not send is not sent at all"


# --------------------------------------------------------------------------- the reading


@pytest.mark.parametrize("reply, want, raw", [
    (frame_2005(True), True, True),
    (frame_2005(False), False, False),
    (frame_2005(1), True, 1),                      # the numbers the integration records another
    (frame_2005(0), False, 0),                     # Elegoo machine answering where a bool is expected
    (frame_2005(None), None, None),                # absent: no reading, and never a default of off
    (frame_2005("yes"), None, "yes"),              # unreadable: unknown with the value kept
    ({"error_code": 0, "canvas_list": [{}], "auto_refill": True}, True, True),   # bare inner object
    ({"error_code": 1003}, None, None),            # a refused read is no reading
    ({}, None, None), (None, None, None), ("nope", None, None),
], ids=["true", "false", "int-one", "int-zero", "absent", "junk", "bare", "refused", "empty",
        "nothing", "not-an-object"])
def test_the_read_back_reports_what_arrived_and_never_invents_a_state(reply, want, raw):
    assert cv.refill_word(reply) == (want, raw)


async def test_an_absent_flag_stays_unknown_rather_than_the_status_reader_s_default_of_false():
    # cc2_state.canvas must answer, so it defaults a missing auto_refill to False; a control's
    # read-back may not borrow that default, because "the printer said nothing" is not "off"
    status_frame = {"error_code": 0, "canvas_info": {"active_canvas_id": 0, "active_tray_id": -1,
                       "canvas_list": [{"canvas_id": 0,
                       "connected": 1, "tray_list": [{"tray_id": 0, "status": 0}]}]}}
    assert st.Cc2CanvasStatus.from_result(status_frame).auto_refill is False   # the default, there
    tr = FakeCanvasTransport({2005: status_frame, 2004: {"error_code": 0}})
    result = await cv.set_auto_refill(tr, cv.refill_params(True))
    assert result.accepted and result.observed is None      # no reading, so nothing is claimed
    d = result.as_dict()
    assert d["observed_auto_refill"] is None and d["observed_auto_refill_raw"] is None
    assert not any("disagreement" in n for n in result.notes)
    assert any("stays unknown" in n for n in result.notes)


# --------------------------------------------------------------------------- the preview


async def test_the_preview_reads_the_canvas_once_and_never_sends_the_command():
    tr = FakeCanvasTransport({2005: frame_2005(False)})      # 2004 unscripted on purpose: reaching
    out = await cv.read_preview(tr, cv.refill_params(True))  # one would still be a recorded fact
    assert tr.methods() == [2005], "the preview path sends the read-only 2005 and nothing else"
    assert out["confirm_required"] is True and out["action"] == "auto_refill"
    p = out["preview"]
    assert p["will_send"] == {"method": 2004, "params": {"auto_refill": True}}
    assert p["waits_up_to_s"] == 10.0
    assert p["auto_refill_now"] is False and p["auto_refill_is_known"] is True
    assert p["auto_refill_raw_now"] is False
    assert "Nothing was sent to the printer" in out["how"] and "confirm=true" in out["how"]
    assert out["verified_on_this_printer"] is False and "2004" in out["unverified"]
    json.dumps(out)


async def test_a_preview_of_a_flag_the_printer_never_reported_says_so_rather_than_off():
    tr = FakeCanvasTransport({2005: frame_2005(None)})
    out = await cv.read_preview(tr, cv.refill_params(True))
    p = out["preview"]
    assert p["auto_refill_now"] is None and p["auto_refill_is_known"] is False
    assert p["auto_refill_raw_now"] is None
    assert any("stays unknown" in n for n in out["notes"])
    assert tr.methods() == [2005]


async def test_a_canvas_read_that_will_not_answer_still_gets_its_preview_and_its_reason():
    # the documented difference from the light and the speed mode: no CANVAS attached is a legal
    # state for a switch the printer stores for itself, so this is a barrier this module refuses to
    # add — but the unknown is said, with the reason, rather than smoothed into false
    tr = FakeCanvasTransport({2005: timeout()})
    out = await cv.read_preview(tr, cv.refill_params(False))
    assert out["confirm_required"] is True
    assert out["preview"]["auto_refill_is_known"] is False
    assert out["preview"]["will_send"] == {"method": 2004, "params": {"auto_refill": False}}
    assert any("stays unknown" in n for n in out["notes"]) and any("never answered" in n
                                                                   for n in out["notes"])
    assert tr.methods() == [2005]          # the failed read is not retried either


# --------------------------------------------------------------------------- the one command


async def test_the_command_is_one_2004_with_the_one_field_and_the_longer_of_the_two_waits():
    tr = scripted()
    result = await cv.set_auto_refill(tr, cv.refill_params(True))
    assert result.status is cv.ControlStatus.ACCEPTED and result.accepted
    sent = tr.calls(2004)
    assert len(sent) == 1 and sent[0][1] == {"auto_refill": True} and sent[0][2] == 10.0
    assert result.method == 2004 and result.sent == {"auto_refill": True} and result.waited_s == 10.0
    # the accepted path's only follow-up: exactly one read-only 2005, and nothing else
    assert tr.methods() == [2004, 2005]


async def test_the_switch_sends_no_other_method_belonging_to_canvas_a_job_a_file_or_a_heater():
    tr = scripted()
    await cv.set_auto_refill(tr, cv.refill_params(True))
    await cv.set_auto_refill(tr, cv.refill_params(False))
    forbidden = {2003, 1020, job.METHOD_PAUSE, job.METHOD_STOP, job.METHOD_RESUME,
                 1028, 1029, 1030, 1031, 1043, 1047}
    assert not (set(tr.methods()) & forbidden), tr.methods()


async def test_an_accepted_switch_reports_what_the_canvas_says_separately():
    tr = scripted(canvas=frame_2005(True))
    result = await cv.set_auto_refill(tr, cv.refill_params(True))
    assert result.accepted
    assert result.observed == {"observed_auto_refill": True, "observed_auto_refill_raw": True}
    joined = " ".join(result.notes)
    assert "did not refuse" in joined and "never part of acceptance" in joined
    assert not any("disagreement" in n for n in result.notes)         # it agrees: nothing to flag


async def test_an_accepted_command_the_canvas_disagrees_with_is_said_out_loud():
    # the printer answered error_code 0 and its own canvas_info still reads off: two facts, both
    # reported, neither overwriting the other, and above all no second command offered
    tr = scripted(canvas=frame_2005(False))
    result = await cv.set_auto_refill(tr, cv.refill_params(True))
    assert result.status is cv.ControlStatus.ACCEPTED and result.accepted
    contra = [n for n in result.notes if "disagreement" in n]
    assert len(contra) == 1 and "accepted auto-refill on" in contra[0] and "reports it off" in contra[0]
    assert len(tr.calls(2004)) == 1 and tr.methods().count(2005) == 1


async def test_an_unreadable_observation_is_no_contradiction_and_keeps_the_value_it_sent():
    tr = scripted(canvas=frame_2005("yes"))
    result = await cv.set_auto_refill(tr, cv.refill_params(True))
    assert result.accepted
    assert result.observed == {"observed_auto_refill": None, "observed_auto_refill_raw": "yes"}
    assert not any("disagreement" in n for n in result.notes)


async def test_observation_that_cannot_land_stays_an_honest_unknown_and_earns_no_retry():
    tr = scripted()
    tr.replies[2005] = timeout()            # the command answered 0; the one look afterwards fails
    result = await cv.set_auto_refill(tr, cv.refill_params(True))
    assert result.status is cv.ControlStatus.ACCEPTED and result.accepted
    assert result.observed is None
    assert any("stays unknown" in n for n in result.notes)
    assert any("never answered" in n for n in result.notes)
    assert len(tr.calls(2004)) == 1 and tr.methods().count(2005) == 1


@pytest.mark.parametrize("answer", [{"error_code": 1010}, {"error_code": 1012}],
                         ids=["no-active-print", "other-code"])
async def test_a_refusal_keeps_its_code_and_earns_no_further_read(answer):
    tr = scripted(command=answer)
    result = await cv.set_auto_refill(tr, cv.refill_params(True))
    assert result.status is cv.ControlStatus.REFUSED and not result.accepted
    assert result.error_code == answer["error_code"]
    assert str(answer["error_code"]) in " ".join(result.notes)
    assert tr.methods() == [2004]            # an on-record refusal is not followed by reads


@pytest.mark.parametrize("answer", [{}, {"error_code": True}, {"error_code": "0"},
                                    {"error_code": 0.0}, {"error_code": None}],
                         ids=["empty", "bool-zero", "string-zero", "float-zero", "null-zero"])
async def test_answers_without_an_explicit_integer_are_ambiguous_never_retried(answer):
    tr = scripted(command=answer)
    result = await cv.set_auto_refill(tr, cv.refill_params(True))
    assert result.status is cv.ControlStatus.AMBIGUOUS and not result.accepted
    assert result.error_code is None
    assert tr.methods() == [2004]            # a reply with no readable meaning gets no second send
    assert any("neither success nor refusal" in n for n in result.notes)


async def test_a_lost_answer_is_ambiguous_never_a_refusal_never_resent_with_one_look():
    tr = scripted(command=timeout(), canvas=frame_2005(True))
    result = await cv.set_auto_refill(tr, cv.refill_params(True))
    assert result.status is cv.ControlStatus.AMBIGUOUS and not result.accepted
    joined = " ".join(result.notes)
    assert "neither a refusal nor a failure" in joined and "nothing was retried" in joined
    assert len(tr.calls(2004)) == 1, "a lost answer never becomes a resend"
    assert tr.methods() == [2004, 2005]      # exactly the one read-only look ambiguity allows
    assert result.observed["observed_auto_refill"] is True     # what it says rides along, unspun


async def test_a_dead_session_is_ambiguous_and_asks_that_session_for_nothing_further():
    tr = scripted(command=PrinterError("not_reachable", "simulated: the session ended"))
    result = await cv.set_auto_refill(tr, cv.refill_params(True))
    assert result.status is cv.ControlStatus.AMBIGUOUS and not result.accepted
    assert tr.methods() == [2004]            # no canvas read a dead session could not carry
    assert any("nothing was resent" in n for n in result.notes)


# --------------------------------------------------------------------------- the candidate label


async def test_every_outcome_says_the_command_has_never_met_a_cc2():
    # the point of the label is that it cannot be lost: it rides on the accepted reply, the refusal,
    # the ambiguity and the preview alike, because every one of them is a claim about a command that
    # has never been answered by this printer
    cases = [scripted(), scripted(command={"error_code": 1010}), scripted(command={}),
             scripted(command=timeout())]
    for tr in cases:
        result = await cv.set_auto_refill(tr, cv.refill_params(True))
        assert result.verified_on_this_printer is False
        assert result.as_dict()["verified_on_this_printer"] is False
        assert "2004" in result.as_dict()["unverified"]
    preview = await cv.read_preview(scripted(), cv.refill_params(True))
    assert preview["verified_on_this_printer"] is False


def test_the_unverified_sentence_survives_the_reply_cap_whole():
    # the sanitiser caps strings at exactly NOTE_CAP characters, and the half of a caveat that gets
    # cut is the half a model would otherwise be trusted to fill in
    assert len(cv.UNVERIFIED) <= job.NOTE_CAP
    assert len(cv.OBSERVE_UNKNOWN_NOTE) <= job.NOTE_CAP
    assert len(cv.CONTRADICT.format(asked="off", seen="on")) <= job.NOTE_CAP
    assert len(cv._ACCEPT_TAIL) <= job.NOTE_CAP


async def test_every_note_of_every_outcome_fits_the_reply_cap():
    seen_any = False
    for command in ({"error_code": 0}, {"error_code": 1010}, {"error_code": 1012}, {}, timeout(),
                    PrinterError("not_reachable", "simulated: the session ended")):
        for canvas in (frame_2005(True), frame_2005(False), frame_2005(None), frame_2005("yes"),
                       timeout()):
            tr = scripted(canvas=canvas, command=command)
            for want in (True, False):
                for n in (await cv.set_auto_refill(tr, cv.refill_params(want))).notes:
                    seen_any = True
                    assert len(n) <= job.NOTE_CAP, (command, canvas, want, n)
    assert seen_any


# --------------------------------------------------------------------------- shape and privacy


async def test_the_result_dict_is_plain_json_and_keeps_the_facts_apart():
    tr = scripted(canvas=frame_2005(1))
    result = await cv.set_auto_refill(tr, cv.refill_params(True))
    d = result.as_dict()
    assert set(d) == {"setting", "method", "sent", "status", "accepted", "error_code", "waited_s",
                      "observed_auto_refill", "observed_auto_refill_raw",
                      "verified_on_this_printer", "unverified", "notes"}
    assert d["setting"] == "auto_refill" and d["method"] == 2004 and d["status"] == "accepted"
    assert d["sent"] == {"auto_refill": True} and d["accepted"] is True and d["error_code"] is None
    assert d["observed_auto_refill"] is True and d["observed_auto_refill_raw"] == 1
    json.dumps(d)
    # and the unknown keys are still there to be read as unknown when nothing landed
    tr2 = scripted()
    tr2.replies[2005] = timeout()
    d2 = (await cv.set_auto_refill(tr2, cv.refill_params(True))).as_dict()
    assert d2["observed_auto_refill"] is None and d2["observed_auto_refill_raw"] is None


async def test_no_address_no_token_rides_any_reply():
    tr = scripted(canvas=frame_2005(True))
    texts = [json.dumps((await cv.set_auto_refill(tr, cv.refill_params(True))).as_dict()),
             json.dumps(await cv.read_preview(tr, cv.refill_params(False)))]
    blob = "\n".join(texts)
    assert HOST not in blob and FAKE_TOKEN not in blob and SN not in blob


def test_the_module_speaks_one_command_and_says_where_the_sources_disagree():
    # one method, and the disagreement written down where the next reader of this file will find it:
    # the older manual's enable is a documented dead end, not an alternative shape to try
    doc = " ".join((cv.__doc__ or "").split())
    assert cv.METHOD_SET_AUTO_REFILL == 2004 and cv.SETTING == "auto_refill"
    assert not [m for m in vars(cv) if m.startswith("METHOD_") and m != "METHOD_SET_AUTO_REFILL"]
    assert "enable" in doc and "auto_refill" in doc and "2005" in doc
    assert "never been offered to a CC2" in doc
