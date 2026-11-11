"""CC2's single guarded start, against scripted replies: the one write method (1020) sits behind
a receipt, a lock, and fresh reads of 1002/2005/1044, and every frame here is the observed one.
The 1020 params are the shape the Home Assistant CC2 client (v2.16.0-beta.2) sends on this
firmware and this printer printed with: ``storage_media`` ``"local"``, the exact ``filename``, and
a ``config`` of exactly ``slot_map`` (complete ``t``/``canvas_id``/``tray_id`` entries — measured:
a partial entry is acknowledged error_code 0 and the printer silently prints from tray 0, so the
mapping must be right BEFORE sending) and ``printer_check``, sent explicitly every time because
the firmware remembers the last value it was given — measured: after a job that sent true, a
following job that omitted the key levelled again. Default False, True only on an explicit
request, and both values are proven on the wire here. The success answer is ``error_code`` 0;
1009 is busy. The status/canvas/inventory fixtures are the frames of test_cc2_state: the verbatim
idle 1002, the live printing 1002 with its lowercase state word and filename, the observed
four-tray canvas where tray 3 answers status 2 (active) and tray 2 status 1 (present) — and the
present tray is proven to start too, which is the mistake the Home Assistant helper once made.
The temperatures are the mount file's real ones: 140 °C non-extruding preheat and 260 °C PETG,
and the tests prove the split a range check must respect — 140 never blocks a PETG start because
extrusion never ran there, while an extrusion outside the tray's rated range, or any commanded
nozzle target above the tray's maximum, stops before the wire. Failure is proven once-each: the
bed assertion is explicit or there is no call at all, a held lock refuses rather than queues,
missing/corrupt/mismatched receipts and partial listings refuse before any method, busy and
error frames refuse, and a lost acknowledgment never becomes a second 1020 — the only follow-up
ever sent is the one read-only 1002, whose lowercase "printing" naming this file is the ONLY way
'running' becomes true. Nothing here opens a socket; the transport is a scripted fake, the access
code is a stand-in string proven never to surface, and no printer address or credential is read
from anywhere."""
from __future__ import annotations

import asyncio
import copy
from collections import deque
from datetime import UTC, datetime

import pytest

from orcaslicer_mcp.printer import cc2_start as cs
from orcaslicer_mcp.printer import cc2_state as st
from orcaslicer_mcp.printer.cc2_receipts import (Cc2ReceiptStore, Cc2UploadReceipt,
                                                 ToolReceipt)
from orcaslicer_mcp.printer.errors import PrinterError

# --------------------------------------------------------------------------- observed wire frames

HOST = "192.0.2.10"           # documented TEST-NET, like the whole lane; never a live address
SN = "CC2TESTSN0001"
FAKE_TOKEN = "SIMULATED-NOT-A-CODE"  # a stand-in with the same role the fake MQTT password plays
DEST = "denbjj-part-flat-petg-2026-11-03.gcode"
SIZE = 4839523                 # the byte count the real listing reported for the mount file
SHA = "3" * 64                 # a stand-in digest; the real file's bytes are nobody's test input

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
    "machine_status": {"exception_status": [], "progress": 21, "status": 2,
                       "sub_status": 2075, "sub_status_reason_code": 0},
    "print_status": {"bed_mesh_detect": True, "current_layer": 51, "enable": True,
                     "filament_detect": False, "filename": DEST, "print_duration": 573,
                     "remaining_time_sec": 2096, "state": "printing", "total_duration": 2669,
                     "uuid": "80baf1df-e748-4717-adf1-b9487f2c8f32"},
    "extruder": {"filament_detect_enable": 1, "filament_detected": 1,
                 "target": 260, "temperature": 259.6},
    "heater_bed": {"target": 70, "temperature": 69.9},
}


def machine_payload(*, machine=(1, 0), state="", filename="", exception_status=None,
                    message=None) -> dict:
    """A 1002 result in the firmware's own sparse lowercase shape, state trio to order."""
    print_status = {"current_layer": 0, "filename": filename, "print_duration": 0,
                    "remaining_time_sec": 0, "state": state, "total_duration": 0, "uuid": ""}
    if message is not None:
        print_status["message"] = message
    return {"error_code": 0,
            "machine_status": {"exception_status": [] if exception_status is None
                               else exception_status,
                               "progress": 0, "status": machine[0], "sub_status": machine[1],
                               "sub_status_reason_code": 0},
            "print_status": print_status,
            "extruder": {"filament_detect_enable": 1, "filament_detected": 1,
                         "target": 0, "temperature": 21},
            "heater_bed": {"target": 0, "temperature": 16}}


def tray(tray_id, status, *, ftype="PETG", brand="ELEGOO", mn=230, mx=260, **kw) -> dict:
    entry = {"tray_id": tray_id, "status": status, "filament_type": ftype, "brand": brand,
             "filament_name": f"{brand} {ftype}".strip(), "filament_color": "00B0F0",
             "min_nozzle_temp": mn, "max_nozzle_temp": mx}
    entry.update(kw)
    return entry


def canvas_replies(*, connected=1, active_tray=3, trays=None) -> dict:
    """A 2005 result as the integration parses it. Default roster: the observed one — A1/A2
    empty, A3 Generic PETG present (status 1), A4 ELEGOO PETG active (status 2)."""
    roster = trays if trays is not None else [
        tray(0, 0, ftype="", brand="", mn=0, mx=0),
        tray(1, 0, ftype="", brand="", mn=0, mx=0),
        tray(2, 1, brand="Generic"),
        tray(3, 2, brand="ELEGOO"),
    ]
    return {"error_code": 0, "canvas_info": {
        "active_canvas_id": 0, "active_tray_id": active_tray, "auto_refill": False,
        "canvas_list": [{"canvas_id": 0, "connected": connected, "tray_list": roster}]}}


def files_result(files: list[dict], *, total: int | None = None) -> dict:
    return {"error_code": 0, "total": len(files) if total is None else total,
            "file_list": files}


def file_entry(name=DEST, size=SIZE) -> dict:
    return {"filename": name, "size": size, "type": "file", "layer": 240,
            "color_map": [{"t": 0, "color": "#00B0F0", "name": "PETG"}]}


def listing_replies(*entries: dict, truncate=False) -> dict:
    """A 1044 answer carrying ``entries``. ``truncate=True`` replays a simulated capped reply
    — 100 of a 112-file listing — whose paging walk repeats page one, which cc2_state must
    refuse as incomplete; the start may then never send anything."""
    if not truncate:
        return files_result(list(entries) or [file_entry()])
    page = [{"filename": f"filler-{i:03d}.gcode", "size": 10 + i, "type": "file"}
            for i in range(100)]

    def reply(params):
        # whether paged or not, the printer hands back the same hundred — the observed trap
        return files_result(page, total=112)
    return reply


# --------------------------------------------------------------------------- scripted session fake


class FakeStartTransport:
    """Stands in for the registered session exactly as Cc2Transport behaves: records every
    request in order and answers from a method -> script map. A script value answers forever; a
    deque answers per call (shift, last value sticks) — which is how one method can first raise
    and then answer, the way a session behaves across a lost reply. Exceptions raise as given —
    a lost reply stays lost here exactly as live — and an unscripted method answers {}."""

    def __init__(self, replies=None, *, host=HOST, sn=SN, display="CC2-test"):
        self.host, self.sn, self.display = host, sn, display
        self.replies = dict(replies or {})
        self.requests: list[tuple[int, dict]] = []

    async def request(self, method: int, params: dict | None = None, *, timeout_s=None) -> dict:
        self.requests.append((method, copy.deepcopy(params or {})))
        reply = self.replies.get(method, {})
        if callable(reply):
            reply = reply(params or {})
        if isinstance(reply, deque):
            reply = reply.popleft() if len(reply) > 1 else reply[0]
        if isinstance(reply, BaseException):
            raise reply
        return copy.deepcopy(reply)

    def methods(self) -> list[int]:
        return [m for m, _ in self.requests]

    def sent(self) -> list[tuple[int, dict]]:
        return [(m, p) for m, p in self.requests if m == cs.METHOD_START]

    def start_params(self, n: int = 0) -> dict:
        return self.sent()[n][1]


def healthy(replies: dict | None = None) -> FakeStartTransport:
    """Every read answers the way the observed printer answers a startable machine: idle 1002,
    connected four-tray canvas, a complete listing naming the received file — and the one start
    command is answered the observed success. ``replies`` overrides any of those by method."""
    return FakeStartTransport({st.METHOD_STATUS: IDLE_1002, st.METHOD_CANVAS: canvas_replies(),
                               st.METHOD_FILES: listing_replies(),
                               cs.METHOD_START: {"error_code": 0}, **(replies or {})})


def timeout() -> PrinterError:
    """The transport's own honest lost-reply error — the reason a start must come back
    ambiguous and never resent."""
    return cs.Cc2TimeoutError("protocol_error", "simulated: the printer never answered",
                              reason="timeout", ambiguous=True)


# --------------------------------------------------------------------------- the private receipt


def make_receipt(**over):
    """The receipt the upload would have written for DEST: the mount file's real evidence — the
    single tool 0, the 140 °C non-extruding preheat recorded as a commanded setpoint and NOT as
    an extrusion temperature, extrusion verified at 260 °C PETG."""
    d = dict(printer_sn=SN, remote_filename=DEST, reported_size_bytes=SIZE,
             content_sha256=SHA, sent_bytes=SIZE, material="PETG",
             nozzle_first_layer_temp=260.0, nozzle_temp=260.0,
             bed_first_layer_temp=70.0, bed_temp=70.0,
             commanded_nozzle_setpoints=(140.0, 260.0), commanded_bed_setpoints=(70.0,),
             extrusion_nozzle_temps=(260.0,), tool_index=0,
             uploaded_at=datetime(2026, 11, 4, 1, 47, tzinfo=UTC))
    d.update(over)
    return Cc2UploadReceipt(**d)


def two_tool_over() -> dict:
    """Receipt kwargs for the two-tool PLA file the multi-tool change parses (the numbers of
    the real penelope-tag fixture): tools 0 and 1, both PLA printing at 210 °C, extrusion
    verified at 210 only, and the vendor's 240 °C range-high flush carried by BOTH tools in
    their own ``flush_nozzle_temps`` (design amendment 9) — not an extrusion temperature and
    not a commanded setpoint. The flat fields describe the first tool, as version 3 requires."""
    tools = tuple(ToolReceipt(tool_index=i, material="PLA",
                              nozzle_first_layer_temp=210.0, nozzle_temp=210.0,
                              extrusion_nozzle_temps=(210.0,),
                              flush_nozzle_temps=(240.0,)) for i in (0, 1))
    return dict(material="PLA", nozzle_first_layer_temp=210.0, nozzle_temp=210.0,
                commanded_nozzle_setpoints=(210.0,),
                extrusion_nozzle_temps=(210.0,), tools=tools)


def pla_roster(*, mx1: int = 250, mx2: int = 250) -> list[dict]:
    """A four-tray canvas where trays 1 and 2 carry PLA (rated 190 to ``mx1``/``mx2``) and
    trays 0 and 3 are empty — the roster a two-tool PLA start maps against."""
    return [tray(0, 0, ftype="", brand="", mn=0, mx=0),
            tray(1, 1, ftype="PLA", brand="Generic", mn=190, mx=mx1),
            tray(2, 2, ftype="PLA", brand="ELEGOO", mn=190, mx=mx2),
            tray(3, 0, ftype="", brand="", mn=0, mx=0)]


@pytest.fixture
def store(tmp_path) -> Cc2ReceiptStore:
    return Cc2ReceiptStore(tmp_path / "receipts")


def save_receipt(store: Cc2ReceiptStore, **over):
    receipt = make_receipt(**over)
    store.save(receipt)
    return receipt


async def begin(store, transport=None, **kw):
    """The start exactly as a tool would call it: bed looked at, one tool mapped to tray 3."""
    transport = transport if transport is not None else healthy()
    kw.setdefault("tool_to_tray", {0: 3})
    result = await cs.start_print(transport, DEST, kw.pop("tool_to_tray"), bed_clear=True,
                                  store=store, **kw)
    return result, transport


def assert_wire_one_start(transport: FakeStartTransport, *, printer_check: bool = False,
                          t: int = 0, canvas_id: int = 0, tray_id: int = 3) -> dict:
    """The one 1020, exactly the measured shape — and nothing else was ever sent."""
    assert len(transport.sent()) == 1, f"expected exactly one start command, got {transport.methods()}"
    params = transport.start_params()
    assert params == {"storage_media": "local", "filename": DEST,
                      "config": {"slot_map": [{"t": t, "canvas_id": canvas_id,
                                               "tray_id": tray_id}],
                                 "printer_check": printer_check}}
    return params


def assert_never_started(transport: FakeStartTransport) -> None:
    """A failed check must be a refusal BEFORE any write method — reads allowed, 1020 never."""
    assert cs.METHOD_START not in transport.methods(), \
        f"1020 was sent despite a failed check: {transport.requests}"


# --------------------------------------------------------------------------- the one good start


async def test_accepted_start_sends_the_measured_1020_once(store):
    save_receipt(store)
    result, tr = await begin(store)
    assert result.status is cs.StartStatus.ACCEPTED and result.accepted
    assert not result.running, "an accepted start is never running until a status says printing"
    assert result.observed is None and result.observed_state is None
    assert_wire_one_start(tr)
    # reads first, in order, then the single write — nothing repeated anywhere
    assert tr.methods() == [st.METHOD_STATUS, st.METHOD_CANVAS, st.METHOD_FILES, cs.METHOD_START]
    assert result.receipt_sha256 == SHA
    # the honesty notes every acceptance carries: a 0 answer validates neither the tray nor
    # the bytes' continued existence
    assert any("tray 0" in n for n in result.notes)
    assert any("cannot be detected" in n for n in result.notes)
    assert all("Multi-tool" not in n for n in result.notes), \
        "the multi-entry slot_map note belongs to multi-tool starts only"
    d = result.as_dict()
    assert d["started_accepted"] is True and d["observed_running"] is False
    assert d["status"] == "accepted" and d["filename"] == DEST
    assert all("flush at" not in n for n in result.notes), "a one-tool file never flushes"
    assert d["tools"] == [{"tool_index": 0, "flush_nozzle_temps_c": []}]


async def test_leveling_off_is_sent_explicitly_as_false_never_omitted(store):
    # the whole reason the key exists on the wire: the firmware remembers the last value, so
    # omission means "as before", and "as before" may be somebody else's ticked box.
    save_receipt(store)
    _, tr = await begin(store)
    config = tr.start_params()["config"]
    assert "printer_check" in config and config["printer_check"] is False


async def test_explicit_leveling_request_sends_true(store):
    save_receipt(store)
    result, tr = await begin(store, printer_check=True)
    assert result.accepted
    assert_wire_one_start(tr, printer_check=True)


async def test_non_bool_leveling_refuses_without_any_call(store):
    save_receipt(store)
    for bad in ("true", 1, 0, None):
        tr = healthy()
        with pytest.raises(cs.Cc2StartError) as ei:
            await begin(store, tr, printer_check=bad)
        assert ei.value.details["reason"] == "printer_check"
        assert tr.requests == []


async def test_present_tray_1_starts_and_active_tray_2_maps_with_its_own_identity(store):
    # the observed semantics: raw 1 present and raw 2 active BOTH feed a start; the Home
    # Assistant helper that called status 2 unavailable is the bug this adapter must not repeat.
    save_receipt(store)
    present_roster = [tray(0, 0, ftype="", brand="", mn=0, mx=0),
                      tray(1, 0, ftype="", brand="", mn=0, mx=0),
                      tray(2, 1, brand="Generic"),      # the PETG starts from the present tray
                      tray(3, 0, ftype="", brand="", mn=0, mx=0)]
    tr = healthy({st.METHOD_CANVAS: canvas_replies(active_tray=-1, trays=present_roster)})
    result, _ = await begin(store, tr, tool_to_tray={0: 2})
    assert result.accepted
    assert_wire_one_start(tr, tray_id=2)


async def test_canvas_id_ride_the_fresh_2005_read_not_the_caller(store):
    # the mapping's identity is what just answered: a unit reported as canvas 1 maps to 1.
    save_receipt(store)
    roster = [tray(2, 1, brand="Generic"), tray(3, 2, brand="ELEGOO")]
    canvas = {"error_code": 0, "canvas_info": {
        "active_canvas_id": 1, "active_tray_id": 3, "auto_refill": False,
        "canvas_list": [{"canvas_id": 1, "connected": 1, "tray_list": roster}]}}
    tr = healthy({st.METHOD_CANVAS: canvas})
    result, _ = await begin(store, tr)
    assert result.accepted
    assert_wire_one_start(tr, canvas_id=1)


async def test_material_match_is_the_words_casefolded(store):
    save_receipt(store)
    tr = healthy({st.METHOD_CANVAS: canvas_replies(
        trays=[tray(0, 0, ftype="", brand="", mn=0, mx=0), tray(1, 0, ftype="", brand="", mn=0, mx=0),
               tray(2, 1, brand="Generic"), tray(3, 2, ftype="petg", brand="elegoo")])})
    result, _ = await begin(store, tr)
    assert result.accepted


async def test_observe_running_uses_the_observed_lowercase_pair(store):
    save_receipt(store)
    # the pre-check read must answer idle (a printing machine refuses before any command);
    # the printing frame arrives on the single observation that follows the accepted start.
    tr = healthy({st.METHOD_STATUS: deque([IDLE_1002, PRINTING_1002])})
    result, _ = await begin(store, tr, observe_running=True)
    assert result.status is cs.StartStatus.ACCEPTED and result.accepted
    assert result.running and result.observed_state == "printing" and result.observed_job == DEST
    assert any("printing" in n for n in result.notes)
    assert tr.methods().count(cs.METHOD_START) == 1
    assert tr.methods().count(st.METHOD_STATUS) == 2   # pre-check read plus ONE observation


async def test_observed_idle_after_accepted_says_idle_not_running(store):
    save_receipt(store)
    result, tr = await begin(store, observe_running=True)
    assert result.accepted and not result.running
    assert result.observed_state == "idle"          # the "" word read as its observed idle pair
    assert tr.methods().count(cs.METHOD_START) == 1


# --------------------------------------------------------------------------- the bed assertion


async def test_bed_clear_must_be_the_explicit_true_assertion(store):
    save_receipt(store)
    tr = healthy()
    with pytest.raises(cs.Cc2StartError) as ei:
        await cs.start_print(tr, DEST, {0: 3}, bed_clear=False, store=store)
    assert ei.value.details["reason"] == "bed_not_clear"
    assert "bed" in ei.value.message and tr.requests == []


async def test_bed_clear_truthy_words_are_not_the_assertion(store):
    save_receipt(store)
    tr = healthy()
    with pytest.raises(cs.Cc2StartError) as ei:
        await cs.start_print(tr, DEST, {0: 3}, bed_clear="yes", store=store)
    assert ei.value.details["reason"] == "bed_not_clear"
    assert tr.requests == []


def test_bed_clear_is_required_not_defaulted(store):
    import inspect
    sig = inspect.signature(cs.start_print)
    assert sig.parameters["bed_clear"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["bed_clear"].default is inspect.Parameter.empty
    assert sig.parameters["printer_check"].default is False, \
        "leveling defaults off because the firmware remembers; true is the explicit-request value"


# --------------------------------------------------------------------------- the receipt gate


async def test_no_receipt_no_start(store):
    tr = healthy()
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr)
    assert ei.value.details["reason"] == "receipt_missing"
    assert "receipt" in ei.value.message and tr.requests == []


async def test_corrupt_receipt_refuses_without_touching_the_printer(store):
    save_receipt(store)
    path = store.path_for(SN, DEST)
    path.write_text('{"version": 2}', encoding="utf-8")   # readable JSON, an object nothing wrote
    tr = healthy()
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr)
    assert ei.value.details["reason"] == "receipt_invalid"
    assert tr.requests == []


async def test_foreign_version_receipt_refuses(store):
    save_receipt(store)
    path = store.path_for(SN, DEST)
    data = copy.deepcopy(__import__("json").loads(path.read_text()))
    data["version"] = 99
    path.write_text(__import__("json").dumps(data), encoding="utf-8")
    tr = healthy()
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr)
    assert ei.value.details["reason"] == "receipt_invalid"
    assert tr.requests == []


async def test_receipt_name_must_match_exactly_case_and_all(store):
    save_receipt(store)
    tr = healthy()
    with pytest.raises(cs.Cc2StartError) as ei:
        await cs.start_print(tr, DEST.upper(), {0: 3}, bed_clear=True, store=store)
    assert ei.value.details["reason"] == "receipt_missing"   # exactness is the point, not a shrug
    assert tr.requests == []


@pytest.mark.parametrize("bad_name", ["", " a.gcode", "a/gcode.gcode", "..", "\x01x.gcode", "a" * 260])
def test_remote_name_must_be_a_plain_name(bad_name, store):
    # the name the printer's listing could never carry never reaches a receipt lookup, let
    # alone a command — the refusal is the receipt store's own plain-name verdict
    with pytest.raises(PrinterError):
        asyncio.run(cs.start_print(FakeStartTransport(), bad_name, {0: 3}, bed_clear=True,
                                   store=store))


# --------------------------------------------------------------------------- the tool mapping


@pytest.mark.parametrize("bad_map", [{}, {0: 3, 1: 3}, None, "tray-3", {True: 3},
                                     {0: True}, {0: -1}, {0: "3"}],
                         ids=["empty", "multi-tool", "none", "string", "bool-key",
                              "bool-tray", "negative", "string-tray"])
async def test_only_the_recorded_single_tool_maps(bad_map, store):
    save_receipt(store)
    tr = healthy()
    with pytest.raises(cs.Cc2StartError) as ei:
        await cs.start_print(tr, DEST, bad_map, bed_clear=True, store=store)
    assert ei.value.details["reason"] == "tool_map"
    assert tr.requests == []


async def test_mapping_must_name_the_recorded_tool(store):
    save_receipt(store, tool_index=1)          # the receipt records tool 1
    tr = healthy()
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr, tool_to_tray={0: 3})
    assert ei.value.details["reason"] == "tool_map_mismatch"
    assert tr.requests == []


# --------------------------------------------------------------------------- the shared lock


async def test_held_lock_refuses_rather_than_queues(store):
    save_receipt(store)
    with store.lock(SN):                        # the same lock an upload takes, held elsewhere
        tr = healthy()
        with pytest.raises(cs.Cc2StartError) as ei:
            await begin(store, tr)
        assert ei.value.details["reason"] == "locked"
        assert tr.requests == []
    save_receipt(store)
    result, tr = await begin(store)             # free again: the start runs, once
    assert result.accepted
    assert len(tr.sent()) == 1


# --------------------------------------------------------------------------- fresh-state refusals


async def test_printing_machine_refuses_the_start(store):
    save_receipt(store)
    tr = healthy({st.METHOD_STATUS: PRINTING_1002})
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr)
    reason = ei.value.details["reason"]
    assert reason == "busy"
    assert "holding" in ei.value.message or "printing" in ei.value.message
    assert_never_started(tr)


async def test_idle_machine_still_holding_a_file_refuses(store):
    save_receipt(store)
    tr = healthy({st.METHOD_STATUS: machine_payload(filename="someone-else.gcode")})
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr)
    assert ei.value.details["reason"] == "busy"
    assert_never_started(tr)


async def test_error_frame_refuses(store):
    save_receipt(store)
    tr = healthy({st.METHOD_STATUS: machine_payload(exception_status=[{"code": 1009}])})
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr)
    assert ei.value.details["reason"] == "busy"
    assert "error" in ei.value.message
    assert_never_started(tr)


async def test_status_read_failure_refuses_before_any_write(store):
    save_receipt(store)
    tr = healthy({st.METHOD_STATUS: timeout()})
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr)
    assert ei.value.details["reason"] == "state_unreadable"
    assert_never_started(tr)


async def test_disconnected_canvas_refuses(store):
    save_receipt(store)
    tr = healthy({st.METHOD_CANVAS: canvas_replies(connected=0)})
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr)
    assert ei.value.details["reason"] == "tray_unavailable"
    assert_never_started(tr)


# --------------------------------------------------------------------------- the listing gate


async def test_file_absent_from_a_complete_listing_refuses(store):
    save_receipt(store)
    tr = healthy({st.METHOD_FILES: listing_replies(file_entry("other-calibration.gcode", 999))})
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr)
    assert ei.value.details["reason"] == "file_missing"
    assert_never_started(tr)


async def test_remote_size_must_equal_the_receipted_size(store):
    save_receipt(store)
    tr = healthy({st.METHOD_FILES: listing_replies(file_entry(size=SIZE + 1))})
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr)
    assert ei.value.details["reason"] == "size_mismatch"
    assert "4839524" in ei.value.message and str(SIZE) in ei.value.message
    assert_never_started(tr)


async def test_partial_listing_is_no_listing_and_no_start(store):
    # a simulated capped reply (100 of 112) whose paging repeats page one: the walk refuses,
    # and a file on an unproven listing can never be vouched for, so 1020 never goes out.
    save_receipt(store)
    tr = healthy({st.METHOD_FILES: listing_replies(truncate=True)})
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr)
    assert ei.value.details["reason"] == "inventory_incomplete"
    assert_never_started(tr)


# --------------------------------------------------------------------------- the tray gate


async def test_empty_tray_refuses(store):
    save_receipt(store)
    tr = healthy({st.METHOD_CANVAS: canvas_replies(
        trays=[tray(3, 0, ftype="", brand="", mn=0, mx=0)])})
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr)
    assert ei.value.details["reason"] == "tray_unavailable"
    assert "empty" in ei.value.message
    assert_never_started(tr)


async def test_unknown_tray_state_refuses(store):
    save_receipt(store)
    tr = healthy({st.METHOD_CANVAS: canvas_replies(trays=[tray(3, 7)])})
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr)
    assert ei.value.details["reason"] == "tray_unavailable"
    assert_never_started(tr)


async def test_tray_the_printer_does_not_have_refuses(store):
    save_receipt(store)
    tr = healthy()
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr, tool_to_tray={0: 5})
    assert ei.value.details["reason"] == "tray_missing"
    assert_never_started(tr)


async def test_tray_reporting_no_material_cannot_be_matched(store):
    save_receipt(store)
    tr = healthy({st.METHOD_CANVAS: canvas_replies(trays=[tray(3, 2, ftype="")])})
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr)
    assert ei.value.details["reason"] == "material_unverified"
    assert_never_started(tr)


async def test_material_mismatch_refuses(store):
    save_receipt(store)
    tr = healthy({st.METHOD_CANVAS: canvas_replies(trays=[tray(3, 2, ftype="PLA")])})
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr)
    assert ei.value.details["reason"] == "material_mismatch"
    assert "PLA" in ei.value.message and "PETG" in ei.value.message
    assert_never_started(tr)


async def test_no_rated_range_refuses(store):
    save_receipt(store)
    tr = healthy({st.METHOD_CANVAS: canvas_replies(trays=[tray(3, 2, mn=0, mx=0)])})
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr)
    assert ei.value.details["reason"] == "range_unverified"
    assert_never_started(tr)


# --------------------------------------------------------------------------- the temperature gate


async def test_the_real_preheat_and_petg_start(store):
    # exactly the mount file's numbers: a 140 °C non-extruding preheat among the commanded
    # setpoints (far below the PETG tray's 230-260 range) with extrusion verified at 260 °C —
    # the preheat must not block the start, because extrusion never ran there.
    save_receipt(store, commanded_nozzle_setpoints=(140.0, 260.0),
                 extrusion_nozzle_temps=(260.0,))
    result, tr = await begin(store)
    assert result.accepted
    assert_wire_one_start(tr)


async def test_extrusion_below_tray_min_refuses(store):
    save_receipt(store, commanded_nozzle_setpoints=(220.0,), extrusion_nozzle_temps=(220.0,))
    tr = healthy()
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr)
    assert ei.value.details["reason"] == "range_violation"
    assert "220" in ei.value.message and "230" in ei.value.message
    assert_never_started(tr)


async def test_extrusion_above_tray_max_refuses(store):
    save_receipt(store, commanded_nozzle_setpoints=(270.0,), extrusion_nozzle_temps=(270.0,))
    tr = healthy()
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr)
    assert ei.value.details["reason"] == "range_violation"
    assert_never_started(tr)


async def test_commanded_nozzle_target_above_tray_max_refuses_even_as_preheat(store):
    # a hold above what the loaded spool is rated for is heat the spool cannot vouch for —
    # preheat or not. The 140 preheat below the minimum stays legal; nothing over the max is.
    save_receipt(store, commanded_nozzle_setpoints=(300.0, 260.0),
                 extrusion_nozzle_temps=(260.0,))
    tr = healthy()
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr)
    assert ei.value.details["reason"] == "commanded_over_tray_max"
    assert "300" in ei.value.message
    assert_never_started(tr)


# --------------------------------------------------------------------------- multi-tool starts


async def test_two_tool_start_sends_the_design_slot_map_exactly(store):
    # design 7: one complete {t, canvas_id, tray_id} entry per tool, ordered by t, each built
    # from the tray that just answered for that tool — even when the caller's dict order says
    # otherwise. These trays are rated to 250, so the 240 °C flush is reported as within.
    save_receipt(store, **two_tool_over())
    tr = healthy({st.METHOD_CANVAS: canvas_replies(active_tray=2, trays=pla_roster())})
    result, _ = await begin(store, tr, tool_to_tray={1: 2, 0: 1})   # caller order reversed
    assert result.status is cs.StartStatus.ACCEPTED and result.accepted
    assert len(tr.sent()) == 1
    params = tr.start_params()
    assert params == {"storage_media": "local", "filename": DEST,
                      "config": {"slot_map": [{"t": 0, "canvas_id": 0, "tray_id": 1},
                                              {"t": 1, "canvas_id": 0, "tray_id": 2}],
                                 "printer_check": False}}
    # every reply of a multi-tool start carries the unverified-two-entry-slot_map note
    assert any("Multi-tool" in n and "never sent" in n for n in result.notes)
    flush = [n for n in result.notes if "flush at" in n]
    assert len(flush) == 2 and all("240 °C (within it)" in n and "250" in n for n in flush)


async def test_missing_tool_in_the_mapping_refuses(store):
    save_receipt(store, **two_tool_over())
    tr = healthy({st.METHOD_CANVAS: canvas_replies(active_tray=2, trays=pla_roster())})
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr, tool_to_tray={0: 1})                # tool 1 has no tray
    assert ei.value.details["reason"] == "tool_map_mismatch"
    assert "missing" in ei.value.message
    assert_never_started(tr)


async def test_extra_tool_in_the_mapping_refuses(store):
    save_receipt(store, **two_tool_over())
    tr = healthy({st.METHOD_CANVAS: canvas_replies(active_tray=2, trays=pla_roster())})
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr, tool_to_tray={0: 1, 1: 2, 2: 3})    # tool 2 is not in the file
    assert ei.value.details["reason"] == "tool_map_mismatch"
    assert_never_started(tr)


async def test_two_tools_on_one_tray_refuse(store):
    save_receipt(store, **two_tool_over())
    tr = healthy({st.METHOD_CANVAS: canvas_replies(active_tray=2, trays=pla_roster())})
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr, tool_to_tray={0: 1, 1: 1})          # one spool, two tools
    assert ei.value.details["reason"] == "tool_map"
    assert_never_started(tr)


async def test_flush_above_the_tray_rating_is_reported_never_refused(store):
    # design amendment 9, the vendor's own numbers: Elegoo's catalog rates both PLA trays
    # 190-230 °C and Elegoo's own change_filament_gcode flushes PLA at its declared range-high
    # 240 °C. Printing runs at 210, inside the rating, so the start goes out — with one note per
    # tool naming the flush and the rating, saying both are the vendor's and nothing was refused.
    save_receipt(store, **two_tool_over())
    tr = healthy({st.METHOD_CANVAS: canvas_replies(
        active_tray=2, trays=pla_roster(mx1=230, mx2=230))})
    result, _ = await begin(store, tr, tool_to_tray={0: 1, 1: 2})
    assert result.status is cs.StartStatus.ACCEPTED and result.accepted
    assert tr.start_params()["config"]["slot_map"] == [
        {"t": 0, "canvas_id": 0, "tray_id": 1}, {"t": 1, "canvas_id": 0, "tray_id": 2}]
    flush = [n for n in result.notes if "flush at" in n]
    assert len(flush) == 2, result.notes
    for tool_index, tray_id, note in zip((0, 1), (1, 2), flush):
        assert f"Tool {tool_index} on tray {tray_id}" in note
        assert "240 °C" in note and "230 °C" in note and "10 °C above" in note
        assert "vendor's own" in note and "slicer profile" in note and "catalog" in note
        assert "not refused" in note
        assert len(note) <= 255, "a tool reply caps each string at 255 characters"
    assert result.flush_nozzle_temps_c == ((0, (240.0,)), (1, (240.0,)))
    assert result.as_dict()["tools"] == [{"tool_index": 0, "flush_nozzle_temps_c": [240.0]},
                                         {"tool_index": 1, "flush_nozzle_temps_c": [240.0]}]


async def test_a_printing_temperature_above_the_tray_rating_still_refuses(store):
    # the flush exception is the flush only: a tool that PRINTS at 240 (an M109-verified
    # extrusion temperature and a commanded setpoint) against a tray rated to 230 refuses.
    over = two_tool_over()
    hot = ToolReceipt(tool_index=1, material="PLA", nozzle_first_layer_temp=240.0,
                      nozzle_temp=240.0, extrusion_nozzle_temps=(240.0,),
                      flush_nozzle_temps=(240.0,))
    # the flat union carries the hot tool's 240 too: a receipt whose tools say more than its
    # file-wide list is refused as inconsistent before any start (receipt rule), so this test
    # keeps the two in step and tests the START's range rule, not the receipt's
    over.update(tools=(over["tools"][0], hot), commanded_nozzle_setpoints=(210.0, 240.0),
                extrusion_nozzle_temps=(210.0, 240.0))
    save_receipt(store, **over)
    tr = healthy({st.METHOD_CANVAS: canvas_replies(
        active_tray=2, trays=pla_roster(mx1=250, mx2=230))})
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr, tool_to_tray={0: 1, 1: 2})
    assert ei.value.details["reason"] == "range_violation"
    assert "240" in ei.value.message and "230" in ei.value.message
    assert_never_started(tr)


async def test_a_commanded_setpoint_above_the_tray_rating_still_refuses_beside_a_flush(store):
    # a non-extruding hold at 240 is not a flush: commanded setpoints over the rated maximum
    # keep refusing even when the receipt also carries a 240 °C flush that is only reported.
    over = two_tool_over()
    over.update(commanded_nozzle_setpoints=(210.0, 240.0))
    save_receipt(store, **over)
    tr = healthy({st.METHOD_CANVAS: canvas_replies(
        active_tray=2, trays=pla_roster(mx1=230, mx2=230))})
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr, tool_to_tray={0: 1, 1: 2})
    assert ei.value.details["reason"] == "commanded_over_tray_max"
    assert "240" in ei.value.message and "230" in ei.value.message
    assert_never_started(tr)


async def test_per_tool_material_mismatch_refuses_the_two_tool_start(store):
    # the checks are per entry: tray 2 reporting PETG under tool 1's PLA refuses even though
    # tray 1 matches tool 0 exactly.
    save_receipt(store, **two_tool_over())
    roster = pla_roster()
    roster[2] = tray(2, 2, ftype="PETG", brand="ELEGOO", mn=190, mx=250)
    tr = healthy({st.METHOD_CANVAS: canvas_replies(active_tray=2, trays=roster)})
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr, tool_to_tray={0: 1, 1: 2})
    assert ei.value.details["reason"] == "material_mismatch"
    assert "PLA" in ei.value.message and "PETG" in ei.value.message
    assert_never_started(tr)


# --------------------------------------------------------------------------- the answer: 0, busy, lost


async def test_busy_answer_is_refused_not_retried(store):
    save_receipt(store)
    tr = healthy({cs.METHOD_START: {"error_code": 1009}})
    result, _ = await begin(store, tr)
    assert result.status is cs.StartStatus.REFUSED and not result.accepted
    assert result.error_code == 1009
    assert "occupied" in " ".join(result.notes) or "1009" in " ".join(result.notes)
    assert len(tr.sent()) == 1          # busy is the printer's answer: nothing gets resent
    assert tr.methods().count(st.METHOD_STATUS) == 1   # and no observation follows a clean no


@pytest.mark.parametrize("answer", [{}, {"error_code": True}, {"error_code": "0"},
                                    {"error_code": 0.0}, {"error_code": None}],
                         ids=["empty", "bool-zero", "string-zero", "float-zero", "null-zero"])
async def test_answers_without_an_explicit_integer_zero_are_ambiguous_not_retried(answer, store):
    save_receipt(store)
    tr = healthy({cs.METHOD_START: answer})
    result, _ = await begin(store, tr, read_only_status=False)
    assert result.status is cs.StartStatus.AMBIGUOUS and not result.accepted
    assert not result.running
    assert len(tr.sent()) == 1
    assert tr.methods().count(cs.METHOD_START) == 1


async def test_lost_reply_ambiguous_one_status_never_a_second_start(store):
    save_receipt(store)
    tr = healthy({cs.METHOD_START: timeout()})
    result, _ = await begin(store, tr)
    assert result.status is cs.StartStatus.AMBIGUOUS and not result.accepted and not result.running
    assert result.observed_state == "idle"
    assert result.observed is not None
    assert tr.methods().count(cs.METHOD_START) == 1, "a lost acknowledgment never becomes a resend"
    assert tr.methods().count(st.METHOD_STATUS) == 2  # the pre-check read plus ONE read-only 1002
    assert any("not resent" in n or "was not resent" in n for n in result.notes)
    assert any("does not mean the start failed" in n for n in result.notes)


async def test_lost_reply_with_observed_printing_is_running_on_the_wire_words(store):
    save_receipt(store)
    tr = healthy({cs.METHOD_START: timeout(),
                    st.METHOD_STATUS: deque([IDLE_1002, PRINTING_1002])})
    result, _ = await begin(store, tr)
    assert result.status is cs.StartStatus.AMBIGUOUS   # the answer stayed lost: no fake "accepted"
    assert result.running and result.observed_state == "printing"
    assert result.observed_job == DEST                 # naming exactly this file: the whole bar
    assert len(tr.sent()) == 1


async def test_observed_printing_of_another_file_is_not_running(store):
    save_receipt(store)
    other = copy.deepcopy(PRINTING_1002)
    other["print_status"]["filename"] = "someone-else.gcode"
    tr = healthy({cs.METHOD_START: timeout(),
                    st.METHOD_STATUS: deque([IDLE_1002, other])})
    result, _ = await begin(store, tr)
    assert result.status is cs.StartStatus.AMBIGUOUS and not result.running
    assert result.observed_state == "printing" and result.observed_job == "someone-else.gcode"


async def test_read_only_status_can_be_declined_and_still_never_restarts(store):
    save_receipt(store)
    tr = healthy({cs.METHOD_START: timeout()})
    result, _ = await begin(store, tr, read_only_status=False)
    assert result.status is cs.StartStatus.AMBIGUOUS and result.observed is None
    assert tr.methods() == [st.METHOD_STATUS, st.METHOD_CANVAS, st.METHOD_FILES, cs.METHOD_START]
    assert any("not requested" in n or "No read-only status was requested" in n
               for n in result.notes)


async def test_status_read_that_also_fails_stays_an_honest_unknown(store):
    save_receipt(store)
    tr = healthy({cs.METHOD_START: timeout(),
                    st.METHOD_STATUS: deque([IDLE_1002, timeout()])})
    result, _ = await begin(store, tr)
    assert result.status is cs.StartStatus.AMBIGUOUS and result.observed is None
    assert any("unknown" in n for n in result.notes)
    assert tr.methods().count(cs.METHOD_START) == 1


async def test_dead_session_reports_ambiguous_and_sends_nothing_else(store):
    save_receipt(store)
    dead = PrinterError("not_reachable", "simulated: the session ended")
    tr = healthy({cs.METHOD_START: dead})
    result, _ = await begin(store, tr)
    assert result.status is cs.StartStatus.AMBIGUOUS and not result.running
    assert tr.methods().count(cs.METHOD_START) == 1
    assert tr.methods().count(st.METHOD_STATUS) == 1   # a dead session is not asked even once more
    assert any("nothing was resent" in n for n in result.notes)


async def test_session_dead_before_the_command_stays_dead(store):
    save_receipt(store)
    dead = PrinterError("not_reachable", "simulated: the session never made it")
    tr = healthy({st.METHOD_STATUS: dead})
    with pytest.raises(cs.Cc2StartError) as ei:
        await begin(store, tr)
    assert ei.value.details["reason"] == "state_unreadable"
    assert_never_started(tr)


# --------------------------------------------------------------------------- the bed warning, passed along


async def test_bed_warning_rides_the_result_never_blocks_or_blesses(store):
    save_receipt(store)
    warning = "Bed type is not recommended for printing with this filament."
    result, tr = await begin(store, bed_warning=warning)
    assert result.accepted and result.bed_warning == warning
    joined = " ".join(result.notes)
    assert "valid slice is not proof" in joined
    assert all("Bed type" not in n for n in result.as_dict()["notes"]), \
        "the caller's words ride the bed_warning field, they are not rewritten into notes"
    assert_wire_one_start(tr)  # the warning changed nothing on the wire


# --------------------------------------------------------------------------- privacy and isolation


async def test_no_address_no_token_no_env_touches(store, monkeypatch, tmp_path):
    save_receipt(store)
    nowhere = tmp_path / "receipts-must-stay-uncreated"
    monkeypatch.setenv("ORCA_CC2_RECEIPTS_DIR", str(nowhere))  # explicit store outranks the env
    result, tr = await begin(store)
    assert result.accepted
    assert not nowhere.exists(), "the default receipt dir must never be consulted when passed"
    texts = [repr(result), str(result.as_dict())]
    for method, params in tr.requests:
        texts.append(f"{method} {params}")
    blob = "\n".join(texts)
    assert HOST not in blob and "10.88.88.190" not in blob
    assert FAKE_TOKEN not in blob
    assert not any(p for _, p in tr.sent() if "token" in str(p).lower())


async def test_errors_carry_the_standard_shape(store):
    tr = healthy()
    with pytest.raises(PrinterError) as ei:
        await begin(store, tr)          # no receipt yet — the closest refusal
    body = ei.value.as_dict()
    assert body["error"] == "protocol_error" and "message" in body
    assert body["reason"] == "receipt_missing"
    assert cs.Cc2StartError("protocol_error", "x", reason="busy").details["reason"] == "busy"
