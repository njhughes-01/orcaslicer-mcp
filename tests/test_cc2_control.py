"""The three CC2 job controls (1021 pause, 1022 stop, 1023 resume) against scripted replies, in
the same house style as the start tests: nothing opens a socket, the transport is a fake that
records every method and the timeout each request asked for. The frames are the observed ones from
test_cc2_state/test_cc2_start (the idle 1002 with its empty state word, the live printing 1002),
plus one paused frame assembled from the protocol doc's own tables (machine status 2, sub_status
2502 PAUSED, print state the documented Klipper word "paused") — marked as doc-derived, because
this adapter has not observed a paused 1002 live. What the tests pin down is the honesty contract:
the preview path sends the one read-only 1002 and never a control method; a confirmed call sends
its one method exactly once with empty params, waited for the official UI's window (60 s pause,
60 s stop, 120 s resume); only an explicit integer error_code 0 is accepted; a refusal keeps its
code (1010 says 'no active print') and earns no follow-up read; a lost answer is ambiguous — never
called a refusal, never called a failure, never resent — and its single allowed follow-up is one
read-only status look; resume's lost answer says plainly that the window expiring is a normal
outcome of a real resume (the measured answer came home at 122 s, outside this window) and that
the result never requires the printer to report running. A dead session sends nothing further at
all. No address, token or live network appears anywhere; the stands-ins are proven never to
surface in a reply."""
from __future__ import annotations

import copy
from collections import deque

import pytest

from orcaslicer_mcp.printer import cc2_control as cc
from orcaslicer_mcp.printer.cc2_transport import Cc2TimeoutError, PrinterError

HOST = "192.0.2.10"                  # documented TEST-NET, like the whole lane; never a live address
SN = "CC2TESTSN0001"
FAKE_TOKEN = "SIMULATED-NOT-A-CODE"  # a stand-in credential, proven never to ride any reply
JOB = "benchy-petg-2026-11-04.gcode"

IDLE_1002 = {  # the verbatim observed idle frame (machine 1/0, the printer's idle word "")
    "error_code": 0,
    "machine_status": {"exception_status": [], "progress": 0, "status": 1, "sub_status": 0,
                       "sub_status_reason_code": 0},
    "print_status": {"bed_mesh_detect": True, "current_layer": 0, "enable": True,
                     "filament_detect": False, "filename": "", "print_duration": 0,
                     "remaining_time_sec": 0, "state": "", "total_duration": 0, "uuid": ""},
    "extruder": {"filament_detect_enable": 1, "filament_detected": 1, "target": 0,
                 "temperature": 21},
    "heater_bed": {"target": 0, "temperature": 16},
}

PRINTING_1002 = {  # the observed live shape: machine 2, lowercase "printing", filename set
    "error_code": 0,
    "machine_status": {"exception_status": [], "progress": 21, "status": 2, "sub_status": 2075,
                       "sub_status_reason_code": 0},
    "print_status": {"current_layer": 51, "filename": JOB, "print_duration": 573,
                     "remaining_time_sec": 2096, "state": "printing", "total_duration": 2669,
                     "uuid": "80baf1df-e748-4717-adf1-b9487f2c8f32"},
    "extruder": {"filament_detect_enable": 1, "filament_detected": 1, "target": 260,
                 "temperature": 259.6},
    "heater_bed": {"target": 70, "temperature": 69.9},
}

PAUSED_1002 = {  # doc-derived, never observed live by this adapter: the protocol doc's machine
                 # status 2 with sub_status 2502 (PAUSED) and its Klipper word "paused"
    "error_code": 0,
    "machine_status": {"exception_status": [], "progress": 21, "status": 2, "sub_status": 2502,
                       "sub_status_reason_code": 0},
    "print_status": {"current_layer": 51, "filename": JOB, "print_duration": 573,
                     "remaining_time_sec": 2096, "state": "paused", "total_duration": 2669,
                     "uuid": "80baf1df-e748-4717-adf1-b9487f2c8f32"},
    "extruder": {"filament_detect_enable": 1, "filament_detected": 1, "target": 260,
                 "temperature": 259.6},
    "heater_bed": {"target": 70, "temperature": 69.9},
}


def unnameable_1002() -> dict:
    frame = copy.deepcopy(PRINTING_1002)
    frame["print_status"]["state"] = "definitely-not-a-documented-word"
    return frame


class FakeControlTransport:
    """Stands in for the registered session exactly as Cc2Transport behaves: records every
    request with the timeout it was given, answers from a method -> script map (a deque shifts,
    last value sticks; exceptions raise as given; an unscripted method answers {}) — and never
    sends anything twice on its own, because the module under test is what must not ask twice."""

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
    """The transport's own honest lost-reply error — the reason a control must come back
    ambiguous and never resent."""
    return Cc2TimeoutError("protocol_error", "simulated: the printer never answered",
                           reason="timeout", ambiguous=True)


def scripted(*, status=None, command=None) -> FakeControlTransport:
    """Status reads answer ``status`` (default idle), the three control methods answer
    ``command`` (default the documented explicit success) — every method recorded either way."""
    command_reply = {"error_code": 0} if command is None else command
    return FakeControlTransport({1002: IDLE_1002 if status is None else status,
                                 1021: command_reply, 1022: command_reply, 1023: command_reply})


# --------------------------------------------------------------------------- the preview: read-only


async def test_preview_reads_status_once_and_never_a_control_method():
    tr = FakeControlTransport({1002: PRINTING_1002})   # control methods unscripted on purpose:
    out = await cc.read_preview(tr, "pause")           # reaching one would still be recorded
    assert tr.methods() == [1002], "the preview path sends the read-only 1002 and nothing else"
    assert out["confirm_required"] is True and out["action"] == "pause"
    assert out["preview"]["will_send"] == {"method": 1021, "params": {}}
    assert out["preview"]["waits_up_to_s"] == 60.0
    assert out["preview"]["job_state"] == "printing" and out["preview"]["job_state_known"] is True
    assert out["preview"]["filename"] == JOB and out["preview"]["progress"] == 21
    assert out["preview"]["machine_status"] == 2 and out["preview"]["sub_status"] == 2075
    assert "Nothing was sent to the printer" in out["how"] and "confirm=true" in out["how"]


async def test_preview_reports_an_unnameable_state_as_unknown_not_as_a_guess():
    tr = FakeControlTransport({1002: unnameable_1002()})
    out = await cc.read_preview(tr, "stop")
    assert tr.methods() == [1002]
    assert out["preview"]["job_state"] is None
    assert out["preview"]["job_state_known"] is False


async def test_preview_that_cannot_read_the_printer_raises_instead_of_bluffing():
    tr = FakeControlTransport({1002: timeout()})
    with pytest.raises(PrinterError):
        await cc.read_preview(tr, "resume")
    assert tr.methods() == [1002]          # the failed read is not retried either


@pytest.mark.parametrize("bad_action", ["extrude", "PAUSE", ""])
async def test_an_action_this_module_does_not_speak_is_a_programmer_error(bad_action):
    tr = FakeControlTransport()
    with pytest.raises(ValueError):
        await cc.control_job(tr, bad_action)
    with pytest.raises(ValueError):
        await cc.read_preview(tr, bad_action)
    assert tr.requests == []


# --------------------------------------------------------------------------- one command, the documented waits


@pytest.mark.parametrize(("action", "method", "wait"),
                         [("pause", 1021, 60.0), ("stop", 1022, 60.0), ("resume", 1023, 120.0)])
async def test_each_action_sends_its_one_method_with_no_params_and_the_official_wait(action,
                                                                                      method, wait):
    tr = scripted()
    result = await cc.control_job(tr, action)
    assert result.status is cc.ControlStatus.ACCEPTED and result.accepted
    sent = tr.calls(method)
    assert len(sent) == 1 and sent[0][1] == {} and sent[0][2] == wait
    assert result.waited_s == wait and result.method == method
    # the one accepted-path follow-up: exactly one read-only 1002, and nothing else
    assert tr.methods() == [method, 1002]


async def test_accepted_pause_reports_observation_apart_from_acceptance():
    tr = scripted(status=PAUSED_1002)
    result = await cc.control_job(tr, "pause")
    assert result.status is cc.ControlStatus.ACCEPTED and result.accepted
    assert result.observed_state == "paused" and result.observed_job == JOB
    assert result.machine_status == 2 and result.sub_status == 2502
    joined = " ".join(result.notes)
    assert "did not refuse" in joined and "observed fields" in joined


async def test_accepted_resume_never_requires_running_and_says_the_measured_timing():
    # the answer arrived, so by the integration's measurement the resume completed — yet the one
    # status look still says "paused", and accepted stays accepted: observation never doubts it
    tr = scripted(status=PAUSED_1002)
    result = await cc.control_job(tr, "resume")
    assert result.status is cc.ControlStatus.ACCEPTED and result.accepted
    assert result.observed_state == "paused"
    joined = " ".join(result.notes)
    assert "122" in joined and "completion word" in joined
    assert "running" not in str(result.as_dict())   # nothing in the reply claims a running print


async def test_accepted_stop_names_the_point_of_no_return():
    tr = scripted(status=IDLE_1002)
    result = await cc.control_job(tr, "stop")
    assert result.status is cc.ControlStatus.ACCEPTED and result.accepted
    assert result.observed_state == "idle" and result.sub_status == 0
    joined = " ".join(result.notes)
    assert "irreversible" in joined and "STOPPED" in joined


async def test_observation_that_cannot_land_stays_an_honest_unknown():
    tr = scripted()
    tr.replies[1002] = timeout()           # the command answered 0; the one look afterwards fails
    result = await cc.control_job(tr, "pause")
    assert result.status is cc.ControlStatus.ACCEPTED and result.accepted
    assert result.observed_state is None and result.observed_job is None
    assert any("unknown" in n for n in result.notes)
    assert len(tr.calls(1021)) == 1        # nothing was retried after the failed look


# --------------------------------------------------------------------------- refusals, kept on record


async def test_no_active_print_refusal_keeps_its_code_and_earns_no_further_read():
    tr = scripted(command={"error_code": 1010})
    result = await cc.control_job(tr, "resume")
    assert result.status is cc.ControlStatus.REFUSED and not result.accepted
    assert result.error_code == 1010
    joined = " ".join(result.notes)
    assert "no active print" in joined and "Nothing was resent" in joined
    assert tr.methods() == [1023]          # an on-record refusal is not followed by reads


async def test_any_other_explicit_code_is_a_refusal_with_the_number_kept():
    tr = scripted(command={"error_code": 1012})
    result = await cc.control_job(tr, "pause")
    assert result.status is cc.ControlStatus.REFUSED and result.error_code == 1012
    assert "1012" in " ".join(result.notes)
    assert tr.methods() == [1021]


@pytest.mark.parametrize("answer", [{}, {"error_code": True}, {"error_code": "0"},
                                    {"error_code": 0.0}, {"error_code": None}],
                         ids=["empty", "bool-zero", "string-zero", "float-zero", "null-zero"])
async def test_answers_without_an_explicit_integer_are_ambiguous_never_retried(answer):
    tr = scripted(command=answer)
    result = await cc.control_job(tr, "stop")
    assert result.status is cc.ControlStatus.AMBIGUOUS and not result.accepted
    assert result.error_code is None
    assert tr.methods() == [1022]          # a reply with no readable meaning gets no second send
    assert any("neither success nor refusal" in n for n in result.notes)


# --------------------------------------------------------------------------- the lost answer


async def test_lost_answer_is_ambiguous_never_a_refusal_never_resent_with_one_look():
    tr = scripted(command=timeout(), status=PAUSED_1002)
    result = await cc.control_job(tr, "pause")
    assert result.status is cc.ControlStatus.AMBIGUOUS and not result.accepted
    joined = " ".join(result.notes)
    assert "neither a refusal nor a failure" in joined
    assert "nothing was retried" in joined
    assert len(tr.calls(1021)) == 1, "a lost answer never becomes a resend"
    assert tr.methods() == [1021, 1002]    # exactly the one read-only look the ambiguity allows
    assert result.observed_state == "paused"   # and what it says is reported, apart


async def test_resume_window_expiry_says_the_wait_expiring_is_normal_and_the_command_stays_sent():
    # the measured resume answer came home at 122 s — outside the 120 s window this tool (and the
    # official UI) waits — so the expiry is a known outcome of a real resume, not bad news, and
    # the one already-sent command is never reconsidered
    tr = scripted(command=timeout(), status=PAUSED_1002)
    result = await cc.control_job(tr, "resume")
    assert result.status is cc.ControlStatus.AMBIGUOUS and not result.accepted
    joined = " ".join(result.notes)
    assert "expected outcome, not bad news" in joined and "122" in joined
    assert "may well be resuming right now" in joined
    assert "nothing was retried" in joined
    assert len(tr.calls(1023)) == 1 and tr.methods().count(1002) == 1
    assert result.observed_state == "paused"   # what the printer claims rides along, unspun
    assert result.waited_s == 120.0


async def test_resume_expiry_whose_look_also_fails_is_still_ambiguity_with_one_send():
    tr = scripted(command=timeout())
    tr.replies[1002] = timeout()
    result = await cc.control_job(tr, "resume")
    assert result.status is cc.ControlStatus.AMBIGUOUS and not result.accepted
    assert result.observed_state is None
    assert any("unknown" in n for n in result.notes)
    assert len(tr.calls(1023)) == 1


async def test_dead_session_is_ambiguous_and_asks_that_session_for_nothing_further():
    dead = PrinterError("not_reachable", "simulated: the session ended")
    tr = scripted(command=dead)
    result = await cc.control_job(tr, "stop")
    assert result.status is cc.ControlStatus.AMBIGUOUS and not result.accepted
    assert tr.methods() == [1022]          # no status read a dead session could not carry
    assert any("nothing was resent" in n for n in result.notes)


# --------------------------------------------------------------------------- shape and privacy


async def test_result_dict_is_plain_json_and_keeps_the_three_facts_apart():
    tr = scripted(status=PAUSED_1002)
    result = await cc.control_job(tr, "pause")
    d = result.as_dict()
    assert set(d) == {"action", "method", "status", "accepted", "error_code", "waited_s",
                      "observed_state", "observed_job", "machine_status", "sub_status", "notes"}
    assert d["action"] == "pause" and d["method"] == 1021 and d["status"] == "accepted"
    assert d["accepted"] is True and d["error_code"] is None and d["waited_s"] == 60.0
    # every note survives the direct tools' reply cap whole — a truncated note would lose the
    # honesty riding at its end
    assert all(len(n) <= cc.NOTE_CAP for n in d["notes"]), d["notes"]
    import json
    json.dumps(d)


async def test_every_note_of_every_outcome_fits_the_reply_cap():
    seen_any = False
    for command in ({"error_code": 0}, {"error_code": 1010}, {}, timeout()):
        for action in ("pause", "stop", "resume"):
            tr = scripted(command=command, status=PAUSED_1002)
            result = await cc.control_job(tr, action)
            for n in result.notes:
                seen_any = True
                assert len(n) <= cc.NOTE_CAP, (action, command, n)
    assert seen_any


async def test_no_address_no_token_rides_any_reply():
    import json

    tr = scripted(status=PAUSED_1002)
    texts = []
    for action in ("pause", "stop", "resume"):
        result = await cc.control_job(tr, action)
        texts.append(json.dumps(result.as_dict()))
        texts.append(json.dumps(await cc.read_preview(tr, action)))
    blob = "\n".join(texts)
    assert HOST not in blob and FAKE_TOKEN not in blob and SN not in blob
