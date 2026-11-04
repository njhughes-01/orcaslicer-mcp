"""The CC2 delete candidate (method 1047) against scripted replies, in the same house style as the
job-control and information tests: nothing opens a socket, the transport is a fake that records every
method, the exact params it was given and the timeout each request asked for. What the tests pin down
is the whole reason this module is a candidate and not a tool.

**The wire.** A confirmed call puts exactly one 1047 on the wire, with the params Elegoo's own LAN
client builds — ``{"storage_media": "local", "file_path": <name>}`` — waited for 10 s (the longer of
the client's 5 s default and the integration's 10 s command window), and never in any other spelling:
``filename`` (the shape a previous firmware refused at 1003) and ``file_name`` (what 1045 wants) are
both asserted absent from the recorded wire. A name goes out verbatim: no leading slash is added,
none stripped, no ``dir`` prefix invented, and no second ask in a different shape after a refusal.

**The two gates.** A name that is not one of ``cc2_list_files``' own names — a path in either
dialect, ``.``, ``..``, an absolute slash, control characters, padding, over-length, not-text —
refuses with zero requests recorded, so path traversal never reaches a session. And the file the
printer's 1002 names as its job is never deleted, including when the state says a job is running but
names no file: that uncertainty refuses too, on the read alone, with no 1047 sent.

**The honesty contract.** Acceptance (explicit integer ``error_code`` 0), refusal (its code kept,
1003 among them, and never renamed) and ambiguity (no code, an unreadable code, a lost answer, a dead
session) use ``cc2_control``'s vocabulary and are never confused; nothing is ever resent, which the
recorded wire proves for every scripted outcome including the ambiguous ones. The one follow-up is a
single read-only 1044 in the un-paged shape, asked after accepted, refused and unanswered commands
alike — the deliberate difference from the job controls, since a delete is irreversible and this
firmware's file-method codes are uninterpreted — and never after a dead session, which reports
``not_checked`` and sends nothing further. The listing decides ``file_now`` under one rule: a name it
contains is present whatever the reply's own arithmetic says, and only a reply that accounts for its
own files (its ``total`` equal to the distinct names delivered) may say gone. A partial, stalled,
declined, malformed, duplicate-named or unanswered listing therefore never says gone, and the two
contradictions (accepted yet still listed; refused yet gone anyway) are reported as contradictions,
with the second never credited to this call.

**And the posture itself.** The last section fails the moment this candidate is exposed before a live
test has proven it: no cc2_delete tool is registered, no delete name is in the CC2 roster, and neither
``server.py``, ``README.md`` nor ``lhm.plugin.json`` mentions it. Deleting a file is authorized work;
promising a user an untested delete is not. No address, token or live network appears anywhere; the
stands-in are proven never to surface in a reply."""
from __future__ import annotations

import copy
import json
from collections import deque
from pathlib import Path

import pytest

from orcaslicer_mcp.printer import cc2_delete as cd
from orcaslicer_mcp.printer import cc2_state as st
from orcaslicer_mcp.printer.cc2_control import ControlStatus
from orcaslicer_mcp.printer.cc2_transport import Cc2TimeoutError, PrinterError

ROOT = Path(__file__).resolve().parents[1]

HOST = "192.0.2.10"                  # documented TEST-NET, like the whole lane; never a live address
SN = "CC2TESTSN0001"
FAKE_CODE = "SIMULATED-NOT-A-CODE"   # a stand-in credential, proven never to ride any reply
TARGET = "benchy-petg-2026-11-04.gcode"
OTHER = "cube-petg-2026-11-04.gcode"

IDLE_1002 = {  # the verbatim observed idle frame (machine 1, the printer's idle word "")
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
    "print_status": {"current_layer": 51, "filename": TARGET, "print_duration": 573,
                     "remaining_time_sec": 2096, "state": "printing", "total_duration": 2669,
                     "uuid": "80baf1df-e748-4717-adf1-b9487f2c8f32"},
    "extruder": {"filament_detect_enable": 1, "filament_detected": 1, "target": 260,
                 "temperature": 259.6},
    "heater_bed": {"target": 70, "temperature": 69.9},
}

PAUSED_1002 = {  # doc-derived, never observed live by this adapter: machine status 2 with
                 # sub_status 2502 (PAUSED) and the protocol's Klipper word "paused"
    "error_code": 0,
    "machine_status": {"exception_status": [], "progress": 21, "status": 2, "sub_status": 2502,
                       "sub_status_reason_code": 0},
    "print_status": {"current_layer": 51, "filename": TARGET, "print_duration": 573,
                     "remaining_time_sec": 2096, "state": "paused", "total_duration": 2699,
                     "uuid": "80baf1df-e748-4717-adf1-b9487f2c8f32"},
    "extruder": {"filament_detect_enable": 1, "filament_detected": 1, "target": 260,
                 "temperature": 259.6},
    "heater_bed": {"target": 70, "temperature": 69.9},
}


def unnameable_1002() -> dict:
    frame = copy.deepcopy(PRINTING_1002)
    frame["print_status"]["state"] = "definitely-not-a-documented-word"
    return frame


def printing_without_name() -> dict:
    frame = copy.deepcopy(PRINTING_1002)
    frame["print_status"]["filename"] = ""
    return frame


def unrecognised_state_without_name() -> dict:
    frame = unnameable_1002()
    frame["print_status"]["filename"] = ""
    return frame


def file_row(name: str, size: int = 1024) -> dict:
    return {"filename": name, "size": size, "type": "file", "create_time": 1762200000}


def listing(names, total="same", **extra) -> dict:
    """One 1044 reply. ``total="same"`` makes the reply account for itself (the case that may say
    gone); ``total=None`` reproduces the historical un-paged answer that reports no total at all;
    any other number reproduces a reply that does not add up."""
    body = {"error_code": 0, "file_list": [file_row(n, 1024 + 10 * i)
                                           for i, n in enumerate(names)]}
    counted = len(names) if total == "same" else total
    if counted is not None:
        body["total"] = counted
    body.update(extra)
    return body


class FakeDeleteTransport:
    """Stands in for the registered session exactly as Cc2Transport behaves: records every request
    with the params and the timeout it was given, answers from a method -> script map (a deque
    shifts, last value sticks; exceptions raise as given; an unscripted method answers {}) — and
    never sends anything twice on its own, because the module under test is what must not ask twice."""

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

    def params_for(self, method: int, index: int = 0) -> dict:
        return self.calls(method)[index][1]


def scripted(over: dict | None = None) -> dict:
    """The ordinary conversation: idle printer, the file present and counted, delete accepted. A
    test overrides one method's answer by handing over the key it wants changed ("1002", "1047",
    "1044") — the only script a case needs is the one reply it is disagreeing about."""
    defaults = {"1002": IDLE_1002, "1047": {"error_code": 0}, "1044": listing([TARGET, OTHER])}
    return {**defaults, **(over or {})}


def transport(over: dict | None = None) -> FakeDeleteTransport:
    replies = {int(k): v for k, v in scripted(over).items()}
    return FakeDeleteTransport(replies)


def timeout() -> PrinterError:
    """The transport's own honest lost-reply error — the reason a delete must come back ambiguous
    and never resent."""
    return Cc2TimeoutError("protocol_error", "simulated: the printer never answered",
                           reason="timeout", ambiguous=True)


def dead() -> PrinterError:
    return PrinterError("not_reachable", "simulated: the session ended")


async def confirmed(over: dict | None = None, *, name: str = TARGET
                    ) -> tuple[cd.Cc2DeleteResult, FakeDeleteTransport]:
    t = transport(over)
    result = await cd.delete_file(t, name)
    return result, t


# --------------------------------------------------------------------------- the wire shape


def test_method_and_wait_are_the_sources_own():
    assert cd.METHOD_DELETE == 1047
    # the official LAN client gives this method its default 5 s, the integration waits 10 s for every
    # command, and this package takes the longer of two first-party windows rather than inventing one
    assert cd.DELETE_WAIT_S == 10.0


def test_params_are_the_official_shape_and_pin_the_third_spelling():
    assert cd.delete_params(TARGET) == {"storage_media": "local", "file_path": TARGET}
    params = cd.delete_params(TARGET)
    assert set(params) == {"storage_media", "file_path"}


@pytest.mark.parametrize("wrong", ["filename", "file_name", "file", "path", "dir", "name"])
def test_the_other_spellings_are_never_what_goes_on_the_wire(wrong):
    # 1045 wants file_name, 1046 wants filename, 1047 wants file_path: the family does not share a
    # spelling, and the shape a previous firmware refused at 1003 must not come back as a cleanup
    params = cd.delete_params(TARGET)
    assert wrong not in params
    assert params["storage_media"] == "local"


async def test_a_confirmed_call_asks_each_thing_once_in_one_order():
    result, t = await confirmed()
    assert t.methods() == [1002, 1047, 1044]
    assert len(t.calls(1047)) == 1
    assert result.status is ControlStatus.ACCEPTED


async def test_the_command_carries_exactly_the_official_params_and_the_documented_wait():
    _, t = await confirmed()
    assert t.params_for(1047) == {"storage_media": "local", "file_path": TARGET}
    assert t.calls(1047)[0][2] == cd.DELETE_WAIT_S


async def test_the_name_goes_out_verbatim_with_no_slash_invented():
    # the client's delete path normalizes nothing (its leading-slash fixups belong to 1045/1046 on
    # removable media), so what the listing shows is what the printer is asked for
    for name in ("spaced file name.gcode", "over_90°-C.gcode", "a.b.c.gcode", "Ünïcode-Ä.gcode"):
        t = transport()
        await cd.delete_file(t, name)
        assert t.params_for(1047)["file_path"] == name


async def test_the_verification_read_is_the_one_shape_ever_observed_answering_this_printer():
    # un-paged, like the historical 1044 request; the pager's dir/offset/limit family has never been
    # observed on this firmware either, so a delete never leans on it
    _, t = await confirmed()
    assert t.params_for(1044) == {"storage_media": "local", "path": "/"}


@pytest.mark.parametrize("media", ["u-disk", "udisk", "sd-card", "sdcard", "local ", "LOCAL", "",
                                   None, 1])
async def test_only_the_internal_store_is_ever_asked_and_it_refuses_before_a_request(media):
    t = transport()
    with pytest.raises(PrinterError) as caught:
        cd.checked_storage_media(media)
    assert caught.value.details["reason"] == "storage_unsupported"
    assert t.requests == []


def test_storage_refusal_names_the_two_spellings_the_sources_disagree_on():
    with pytest.raises(PrinterError) as caught:
        cd.checked_storage_media("u-disk")
    hint = caught.value.hint
    assert "u-disk" in hint and "udisk" in hint and "sd-card" in hint and "sdcard" in hint


# --------------------------------------------------------------------------- gate 1: the name


@pytest.mark.parametrize("bad", [
    "", " ", " x.gcode", "x.gcode ", ".", "..", "../escape.gcode", "/absolute.gcode",
    "sub/dir.gcode", "sub\\dir.gcode", "\\", "/", "x" * 256, "x\x00.gcode", "x\n.gcode",
    "x\x7f.gcode", "~/x.gcode", "./x.gcode",
])
async def test_a_name_the_printers_own_listing_could_never_show_refuses_locally(bad):
    t = transport()
    with pytest.raises(st.Cc2DataError) as caught:
        await cd.delete_file(t, bad)
    assert caught.value.details["reason"] == "name_rejected"
    assert t.requests == []          # nothing sent, not even a read-only status


@pytest.mark.parametrize("bad", [None, 12, 1.5, True, ["x.gcode"], {"filename": "x"}, b"x"])
async def test_a_name_that_is_not_text_refuses_locally(bad):
    t = transport()
    with pytest.raises(st.Cc2DataError) as caught:
        await cd.delete_file(t, bad)
    assert caught.value.details["reason"] == "name_rejected"
    assert t.requests == []


async def test_a_refused_name_says_nothing_was_sent_and_points_at_the_listing():
    t = transport()
    with pytest.raises(st.Cc2DataError) as caught:
        await cd.delete_file(t, "sub/dir.gcode")
    assert "Nothing was sent" in caught.value.message
    assert "cc2_list_files" in caught.value.hint


# --------------------------------------------------------------------------- gate 2: the active file


@pytest.mark.parametrize("frame", [PRINTING_1002, PAUSED_1002], ids=["printing", "paused"])
async def test_the_file_the_printer_names_as_its_job_is_never_deleted(frame):
    t = transport({"1002": frame})
    with pytest.raises(st.Cc2DataError) as caught:
        await cd.delete_file(t, TARGET)
    assert caught.value.details["reason"] == "active_file"
    assert t.methods() == [1002]               # refused on the read, no 1047, no 1044
    assert len(t.calls(1047)) == 0


async def test_an_active_looking_state_that_names_no_file_refuses_too():
    # the uncertainty Cc2MachineStatus.active_job documents is never resolved by betting a name
    t = transport({"1002": printing_without_name()})
    with pytest.raises(st.Cc2DataError) as caught:
        await cd.delete_file(t, OTHER)
    assert caught.value.details["reason"] == "active_file_unknown"
    assert len(t.calls(1047)) == 0


async def test_a_state_word_this_adapter_does_not_know_refuses_too():
    # an unknown word with no filename is the same uncertainty as an unknown name: it is never
    # resolved in the direction that permits an irreversible call
    t = transport({"1002": unrecognised_state_without_name()})
    with pytest.raises(st.Cc2DataError) as caught:
        await cd.delete_file(t, OTHER)
    assert caught.value.details["reason"] == "active_file_unknown"
    assert len(t.calls(1047)) == 0


async def test_an_unrecognised_state_that_names_a_different_file_does_not_block_the_delete():
    # the word is only used where the name is missing; a printer that names the file it holds has
    # answered the only question this gate asks, whatever word it put next to it
    result, t = await confirmed({"1002": unnameable_1002()}, name=OTHER)
    assert result.job_state is None
    assert result.filename == OTHER and result.command_attempted
    assert t.methods() == [1002, 1047, 1044]
    assert len(t.calls(1047)) == 1


async def test_a_different_file_may_be_deleted_while_the_printer_prints_another_one():
    # the gate is the active file and nothing else: the user's authorization covers deleting files, so
    # an idle-state precondition or an "it is probably in use" heuristic would be a barrier, not
    # safety — the printer is still mid-job TARGET here and the ask goes through for the other name
    t = transport({"1002": PRINTING_1002})
    result = await cd.delete_file(t, OTHER)
    assert t.methods() == [1002, 1047, 1044]
    assert result.status is ControlStatus.ACCEPTED
    assert result.file_now == cd.FILE_NOW_PRESENT      # the scripted listing still names OTHER
    assert result.job_state == "printing"


async def test_the_idle_frame_naming_no_file_is_not_treated_as_active():
    result, _ = await confirmed()
    assert result.command_attempted


async def test_a_status_read_that_fails_is_raised_and_nothing_is_commanded():
    t = transport({"1002": timeout()})
    with pytest.raises(Cc2TimeoutError):
        await cd.delete_file(t, TARGET)
    assert t.methods() == [1002]


# --------------------------------------------------------------------------- reading the answer


async def test_an_explicit_zero_is_the_only_acceptance():
    result, _ = await confirmed()
    assert result.status is ControlStatus.ACCEPTED and result.accepted is True
    assert result.error_code is None


@pytest.mark.parametrize("code", ["0", True, False, 0.0, 1.0, None, "", "error_code:0", [0], {}])
async def test_an_answer_that_cannot_be_read_is_ambiguous_never_accepted(code):
    result, t = await confirmed({"1047": {"error_code": code} if not isinstance(code, dict)
                                 else {"foo": "bar"}})
    assert result.status is ControlStatus.AMBIGUOUS
    assert result.accepted is False
    assert len(t.calls(1047)) == 1             # an unreadable answer never earns a second try


async def test_a_reply_with_no_code_at_all_says_so():
    result, _ = await confirmed({"1047": {"result": "ok"}})
    assert any("carried no error code at all" in n for n in result.notes)


async def test_an_unreadable_code_names_its_type():
    result, _ = await confirmed({"1047": {"error_code": "0"}})
    assert any("cannot be read (str)" in n for n in result.notes)


async def test_a_refusal_keeps_its_number():
    result, _ = await confirmed({"1047": {"error_code": 1003}})
    assert result.status is ControlStatus.REFUSED and result.error_code == 1003
    assert result.accepted is False


async def test_refusal_of_1003_refuses_to_rename_it():
    result, _ = await confirmed({"1047": {"error_code": 1003}})
    note = next(n for n in result.notes if "1003" in n)
    assert "not established" in note
    assert "file not found" not in note.lower()
    assert "no active print" not in note       # the job controls' 1010 gloss is not a fact about files


async def test_another_refusal_number_is_reported_as_itself():
    result, _ = await confirmed({"1047": {"error_code": 42}})
    assert any("error_code 42" in n for n in result.notes)


async def test_a_lost_answer_is_ambiguous_and_its_listing_still_runs():
    result, t = await confirmed({"1047": timeout()})
    assert result.status is ControlStatus.AMBIGUOUS
    assert len(t.calls(1047)) == 1
    assert len(t.calls(1044)) == 1
    assert any("did not answer" in n for n in result.notes)
    assert not result.deletion_proved


async def test_a_lost_answer_never_becomes_a_refusal_or_a_failure():
    result, _ = await confirmed({"1047": timeout()})
    text = " ".join(result.notes).lower()
    assert "refused" not in text and "failed" not in text


async def test_a_dead_session_sends_nothing_further_at_all():
    result, t = await confirmed({"1047": dead()})
    assert result.status is ControlStatus.AMBIGUOUS
    assert result.file_now == cd.FILE_NOW_NOT_CHECKED
    assert t.methods() == [1002, 1047]         # not even the read-only listing is attempted
    assert any("nothing was resent" in n.lower() for n in result.notes)


@pytest.mark.parametrize("reply", [{"error_code": 0}, {"error_code": 1003}, {}, "not-a-dict"])
async def test_the_command_is_published_once_whatever_the_answer_says(reply):
    _, t = await confirmed({"1047": reply})
    assert len(t.calls(1047)) == 1


@pytest.mark.parametrize("code", [0, 1003, 42])
async def test_the_sent_params_are_on_the_record_in_the_result(code):
    result, _ = await confirmed({"1047": {"error_code": code}})
    assert result.sent_params == {"storage_media": "local", "file_path": TARGET}
    assert result.command_attempted is True
    assert result.method == 1047


# --------------------------------------------------------------------------- the verifying listing


async def test_a_self_accounting_listing_without_the_name_is_the_only_gone():
    result, _ = await confirmed({"1044": listing([OTHER])})
    assert result.file_now == cd.FILE_NOW_GONE
    assert result.deletion_proved is True
    assert result.listing_certified is True
    assert result.listing_count == 1 and result.listing_total == 1


async def test_an_accepted_command_whose_file_is_still_listed_is_a_said_aloud_contradiction():
    result, t = await confirmed()                          # 1044 still names TARGET
    assert result.status is ControlStatus.ACCEPTED
    assert result.file_now == cd.FILE_NOW_PRESENT
    assert result.deletion_proved is False
    assert any("NOT gone" in n for n in result.notes)
    assert any("contradiction" in n.lower() for n in result.notes)
    assert len(t.calls(1047)) == 1                         # and it never earns a second command


async def test_a_refused_command_whose_file_is_gone_is_never_credited_to_this_call():
    result, _ = await confirmed({"1047": {"error_code": 1003}, "1044": listing([OTHER])})
    assert result.status is ControlStatus.REFUSED
    assert result.file_now == cd.FILE_NOW_GONE
    assert result.deletion_proved is False
    assert any("not credited" in n for n in result.notes)


async def test_a_refusal_is_still_verified_because_the_code_is_not_the_evidence():
    # the deliberate difference from cc2_control: file-method codes on this firmware are not
    # established, deletion is irreversible, so the listing runs after a refusal too
    result, t = await confirmed({"1047": {"error_code": 1003}})
    assert len(t.calls(1044)) == 1
    assert result.file_now == cd.FILE_NOW_PRESENT
    assert any("Not deleted" in n for n in result.notes)


async def test_a_listing_that_reports_no_total_cannot_say_gone():
    result, _ = await confirmed({"1044": listing([OTHER], total=None)})
    assert result.file_now == cd.FILE_NOW_UNKNOWN
    assert result.listing_certified is False
    assert result.listing_total is None
    assert result.deletion_proved is False
    assert any("could not certify itself" in n for n in result.notes)
    assert "no claim of gone" in " ".join(result.notes)


async def test_a_short_listing_short_of_its_total_cannot_say_gone_either():
    result, _ = await confirmed({"1044": listing([OTHER], total=112)})
    assert result.file_now == cd.FILE_NOW_UNKNOWN
    assert result.listing_count == 1 and result.listing_total == 112
    assert any("delivered 1 of the 112 files it counts" in n for n in result.notes)


async def test_a_listing_that_names_more_than_its_total_cannot_say_gone():
    result, _ = await confirmed({"1044": listing([OTHER, "x.gcode"], total=1)})
    assert result.file_now == cd.FILE_NOW_UNKNOWN
    assert any("reporting a total of 1" in n for n in result.notes)


async def test_presence_is_presence_even_in_a_listing_that_proves_nothing_else():
    # a partial reply can still prove the file is there, and this says so instead of going unknown
    result, _ = await confirmed({"1044": listing([TARGET], total=None)})
    assert result.file_now == cd.FILE_NOW_PRESENT
    assert result.listing_certified is False
    assert result.listing_why is None


async def test_a_listing_that_declines_is_unknown_with_the_printers_own_reason():
    result, _ = await confirmed({"1044": {"error_code": 1003}})
    assert result.file_now == cd.FILE_NOW_UNKNOWN
    assert "1044" in (result.listing_why or "")
    assert any("could not certify itself" in n for n in result.notes)


async def test_a_listing_that_never_answers_is_unknown_and_is_not_asked_again():
    result, t = await confirmed({"1044": timeout()})
    assert len(t.calls(1044)) == 1
    assert len(t.calls(1047)) == 1
    assert result.file_now == cd.FILE_NOW_UNKNOWN
    assert any("could not certify itself" in n for n in result.notes)


async def test_a_listing_with_a_repeated_name_reads_as_nothing_proven():
    reply = {"error_code": 0, "file_list": [file_row(OTHER), file_row(OTHER)], "total": 2}
    result, _ = await confirmed({"1044": reply})
    assert result.file_now == cd.FILE_NOW_UNKNOWN
    assert "arrived twice" in (result.listing_why or "")


async def test_a_listing_that_is_not_an_object_reads_as_nothing_proven():
    result, _ = await confirmed({"1044": ["not", "a", "reply"]})
    assert result.file_now == cd.FILE_NOW_UNKNOWN


async def test_a_listing_with_a_hole_in_it_reads_as_nothing_proven():
    reply = {"error_code": 0, "file_list": [{"filename": "bad/name.gcode", "size": 1}], "total": 1}
    result, _ = await confirmed({"1044": reply})
    assert result.file_now == cd.FILE_NOW_UNKNOWN


async def test_the_verification_never_turns_into_a_second_command():
    for reply in (timeout(), dead(), {"error_code": 1003}, {}, ["nope"]):
        _, t = await confirmed({"1044": reply})
        assert len(t.calls(1047)) == 1
        assert len(t.calls(1044)) == 1


# --------------------------------------------------------------------------- the 3x3 of outcome words


@pytest.mark.parametrize("status,file_now,expect", [
    (ControlStatus.ACCEPTED, cd.FILE_NOW_GONE, "Deleted"),
    (ControlStatus.ACCEPTED, cd.FILE_NOW_PRESENT, "NOT gone"),
    (ControlStatus.ACCEPTED, cd.FILE_NOW_UNKNOWN, "unproven"),
    (ControlStatus.REFUSED, cd.FILE_NOW_PRESENT, "Not deleted"),
    (ControlStatus.REFUSED, cd.FILE_NOW_GONE, "not credited"),
    (ControlStatus.REFUSED, cd.FILE_NOW_UNKNOWN, "unproven"),
    (ControlStatus.AMBIGUOUS, cd.FILE_NOW_PRESENT, "not having landed"),
    (ControlStatus.AMBIGUOUS, cd.FILE_NOW_GONE, "having landed"),
    (ControlStatus.AMBIGUOUS, cd.FILE_NOW_UNKNOWN, "unknown"),
    (ControlStatus.AMBIGUOUS, cd.FILE_NOW_NOT_CHECKED, "unknown"),
])
def test_every_cell_of_the_matrix_says_its_own_true_thing(status, file_now, expect):
    answer = cd.plain_answer(status, file_now, 1003)
    assert expect in answer
    assert len(answer) <= cd.NOTE_CAP


def test_only_one_cell_of_the_matrix_is_called_a_deletion():
    proved = [(s, f) for s in ControlStatus for f in
              (cd.FILE_NOW_GONE, cd.FILE_NOW_PRESENT, cd.FILE_NOW_UNKNOWN,
               cd.FILE_NOW_NOT_CHECKED)
              if "Deleted" in cd.plain_answer(s, f, 1003)]
    assert proved == [(ControlStatus.ACCEPTED, cd.FILE_NOW_GONE)]


async def test_an_unanswered_command_whose_file_is_gone_is_not_claimed_as_this_deletion():
    result, _ = await confirmed({"1047": timeout(), "1044": listing([OTHER])})
    assert result.file_now == cd.FILE_NOW_GONE
    assert result.deletion_proved is False
    assert "consistent with" in " ".join(result.notes)


# --------------------------------------------------------------------------- the preview


async def test_the_preview_sends_no_delete_at_all():
    t = transport()
    out = await cd.read_preview(t, TARGET)
    assert t.methods() == [1002, 1044]
    assert t.calls(1047) == []
    assert out["confirm_required"] is True
    assert out["action"] == "delete"


async def test_the_preview_shows_the_exact_request_a_confirmed_call_would_send():
    out = await cd.read_preview(transport(), TARGET)
    preview = out["preview"]
    assert preview["will_send"] == {"method": 1047,
                                    "params": {"storage_media": "local", "file_path": TARGET}}
    assert preview["waits_up_to_s"] == cd.DELETE_WAIT_S
    assert preview["then_reads"] == {"method": 1044,
                                     "params": {"storage_media": "local", "path": "/"}}


async def test_the_preview_reports_the_job_and_the_listing_it_read():
    out = await cd.read_preview(transport(), OTHER)
    preview = out["preview"]
    assert preview["job_state"] == "idle"
    assert preview["job_filename"] is None
    assert preview["file_in_this_listing"] == "listed"
    assert preview["file_size_bytes"] == 1024 + 10 * 1
    assert preview["listing_certified"] is True
    assert preview["listing_count"] == 2 and preview["listing_total"] == 2


async def test_a_preview_of_a_name_that_is_not_there_reports_not_listed_not_a_guess():
    out = await cd.read_preview(transport({"1044": listing([OTHER])}), TARGET)
    assert out["preview"]["file_in_this_listing"] == "not_listed"
    assert out["preview"]["file_size_bytes"] is None


async def test_a_preview_whose_listing_cannot_certify_says_unknown_with_numbers():
    out = await cd.read_preview(transport({"1044": listing([OTHER], total=112)}), TARGET)
    preview = out["preview"]
    assert preview["file_in_this_listing"] == "unknown"
    assert preview["listing_proves_nothing_because"] == "it delivered 1 of the 112 files it counts"
    assert preview["listing_certified"] is False


async def test_a_preview_that_cannot_read_the_listing_reports_that_rather_than_guessing():
    out = await cd.read_preview(transport({"1044": timeout()}), TARGET)
    preview = out["preview"]
    assert preview["file_in_this_listing"] == "unknown"
    assert preview["listing_proves_nothing_because"] == "simulated: the printer never answered"


async def test_the_preview_carries_the_open_questions_it_owes_the_user():
    preview = (await cd.read_preview(transport(), TARGET))["preview"]
    text = " ".join(preview["unverified"])
    assert "file_path" in text
    assert "1003" in text
    assert "leading slash" in text
    assert "no method restores" in text
    assert len(preview["unverified"]) == len(cd.UNVERIFIED)


async def test_the_preview_of_an_active_file_refuses_before_the_listing_is_read():
    t = transport({"1002": PRINTING_1002})
    with pytest.raises(st.Cc2DataError):
        await cd.read_preview(t, TARGET)
    assert t.methods() == [1002]
    assert t.calls(1047) == []


async def test_the_preview_of_a_path_refuses_without_any_request():
    t = transport()
    with pytest.raises(st.Cc2DataError):
        await cd.read_preview(t, "../escape.gcode")
    assert t.requests == []


async def test_the_preview_how_names_the_permanence_and_the_confirmation():
    out = await cd.read_preview(transport(), TARGET)
    assert "Nothing was sent" in out["how"]
    assert "confirm=true" in out["how"]
    assert "undoes" in out["how"]


# --------------------------------------------------------------------------- hygiene


async def test_a_result_is_plain_json_and_carries_no_credential():
    result, _ = await confirmed({"1047": {"error_code": 1003}})
    blob = json.dumps(result.as_dict())
    assert FAKE_CODE not in blob
    assert isinstance(result.as_dict()["notes"], list)
    assert result.as_dict()["status"] == "refused"


async def test_every_authored_string_fits_the_direct_tools_reply_cap():
    # the direct tools' own cap is what a note gets (server.py's _CC2_STR_MAX), and a note cut
    # mid-sentence loses the half a model acts on, so every string this module writes is measured
    # here rather than discovered by a truncation later
    texts = [cd.PREVIEW_HOW, cd._ACCEPT_TAIL, *cd.UNVERIFIED,
             cd._INCOMPLETE_LIST_NOTE.format(why="x" * cd._WHY_MAX),
             cd.delete_refusal_note(1003), cd.delete_refusal_note(7)]
    texts += [cd.plain_answer(s, f, 1003) for s in ControlStatus
              for f in (cd.FILE_NOW_GONE, cd.FILE_NOW_PRESENT, cd.FILE_NOW_UNKNOWN,
                        cd.FILE_NOW_NOT_CHECKED)]
    for text in texts:
        assert len(text) <= cd.NOTE_CAP, f"{text[:40]!r} is {len(text)} characters"


async def test_why_a_read_proved_nothing_stays_short_enough_to_ride_a_note():
    # the untrimmed reason is kept in its own field, where the cap has a whole budget to itself,
    # while the note gets a word-boundary trim with an ellipsis that says something was cut
    long_reason = "simulated: " + "the printer mumbled an extraordinarily long reason " * 6
    trimmed = cd._short(long_reason)
    assert trimmed.endswith("…") and len(trimmed) <= cd._WHY_MAX + 1
    note = cd._INCOMPLETE_LIST_NOTE.format(why=trimmed)
    assert len(note) <= cd.NOTE_CAP
    assert "no claim of gone is made" in note


@pytest.mark.parametrize("bad", ["sub/dir.gcode", "..", "", "x" * 256, None])
async def test_gate_messages_fit_the_cap_and_say_nothing_was_sent(bad):
    with pytest.raises(st.Cc2DataError) as caught:
        await cd.delete_file(transport(), bad)
    assert len(caught.value.message) <= cd.NOTE_CAP
    assert len(caught.value.hint or "") <= cd.NOTE_CAP


async def test_a_printing_frame_message_fits_the_cap():
    t = transport({"1002": printing_without_name()})
    with pytest.raises(st.Cc2DataError) as caught:
        await cd.delete_file(t, "a-fairly-long-test-file-name-2026-11-04.gcode")
    assert len(caught.value.message) <= cd.NOTE_CAP


# --------------------------------------------------------------------------- the candidate posture


def test_no_delete_tool_is_registered():
    # the module is an internal candidate until a live test on a uniquely named file has answered
    # what 1047 does on this firmware; until then no client may be shown that this server deletes
    import orcaslicer_mcp.server as srv

    names = set(srv.mcp._tool_manager._tools)
    assert not [n for n in names if "delete" in n and n.startswith("cc2_")]
    assert not [n for n in srv.CC2_TOOL_NAMES if "delete" in n.lower()]
    assert not [n for n in srv._TOOL_ANNOTATIONS if n.startswith("cc2_") and "delete" in n]


@pytest.mark.parametrize("relative", ["src/orcaslicer_mcp/server.py", "README.md",
                                      "lhm.plugin.json", "mcpb/manifest.json",
                                      "scripts/gen_lhm_tools.py"])
def test_the_candidate_is_not_offered_as_a_supported_capability(relative):
    # the second half of the same rule: it is not promised in the docs or the manifests either, and
    # README still says plainly what the server does and does not claim about deletion
    text = (ROOT / relative).read_text(encoding="utf-8")
    assert "cc2_delete" not in text
    assert "cc2_delete_file" not in text


def test_the_module_is_importable_but_imports_nothing_from_the_server():
    import inspect

    source = Path(inspect.getsourcefile(cd)).read_text(encoding="utf-8")
    assert "import server" not in source
    assert "@mcp.tool" not in source


def test_the_documented_exposure_steps_are_the_ones_a_live_test_has_to_take():
    # if the docstring's recipe ever goes stale, an exposed tool arrives without somebody having
    # read this file, so the four named edits are asserted against server.py's real structures
    server = (ROOT / "src/orcaslicer_mcp/server.py").read_text(encoding="utf-8")
    for structure in ("CC2_TOOL_NAMES", "_TOOL_ANNOTATIONS", "_OPEN_WORLD_TOOLS", "@mcp.tool()"):
        assert structure in server
