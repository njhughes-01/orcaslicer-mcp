"""CC2 printer state and file inventory against scripted replies: status 1002, CANVAS 2005,
file list 1044. The envelopes here are the shapes actually observed on the real Centauri Carbon 2
(firmware 02.01.00.00) — the 1002 idle reply is verbatim from the direct read, printing and
paused frames use the lowercase wire fields the printer names (print_status.state a string:
"" idle, "printing" live; filename/uuid/current_layer lowercase), the 2005 canvas_info shape is
the one the Home Assistant CC2 client parses field-for-field, and the 1044 request is the one
shape ever verified on this printer, {"storage_media": "local", "path": "/"} — whose measured
answer was ALL 54 of its files in one reply. NO paged 1044 reply has ever been seen on this
machine, and the sources do not agree on paging: Elegoo's own LAN web client sends
{storage_media, dir?, offset, limit} while the protocol doc's lone example shows page/page_size,
so files_transport models a capped reply and every failure mode a paging printer has (ignoring
offset, overlapping pages, refusing the paged ask, stalling, never reporting a total) as the
HYPOTHETICAL firmwares they are — none of them a capture, and none of them evidence that the
printer does it. Nothing in this file is live-tested and nothing claims to be.
Tray semantics are the design's observed ones: 0 empty, 1 present, 2 active — both loaded
states feed a start, with material and range checks reserved for the start itself. Nothing here
opens a socket: the transport is a scripted fake whose replies arrive by id."""
from __future__ import annotations

import contextlib
import copy

import pytest

from orcaslicer_mcp.printer import cc2_state as st
from orcaslicer_mcp.printer.errors import PrinterError

# --------------------------------------------------------------------------- observed wire frames

OBSERVED_IDLE_1002 = {  # verbatim, direct read 2026-11-04 — the locked shape of an idle CC2
    "error_code": 0,
    "machine_status": {"exception_status": [], "progress": 0, "status": 1, "sub_status": 0,
                       "sub_status_reason_code": 0},
    "print_status": {"bed_mesh_detect": True, "current_layer": 0, "enable": True,
                     "filament_detect": False, "filename": "", "print_duration": 0,
                     "remaining_time_sec": 0, "state": "", "total_duration": 0, "uuid": ""},
    "extruder": {"filament_detect_enable": 1, "filament_detected": 1, "target": 0,
                 "temperature": 111},
    "heater_bed": {"target": 0, "temperature": 75},
}


def printing_payload() -> dict:
    """A 1002 answer during a live print: machine status 2, print_status.state "printing",
    filename set — the pair the task message names as the observed printing shape. Layer,
    progress and temperatures follow the direct-read paused print's real ranges so no field is
    invented; the state and machine code are the observed printing values."""
    return {
        "error_code": 0,
        "machine_status": {"exception_status": [], "progress": 21, "status": 2,
                           "sub_status": 2075, "sub_status_reason_code": 0},
        "print_status": {"bed_mesh_detect": True, "current_layer": 51,
                         "enable": True, "filament_detect": False,
                         "filename": "denbjj-part-flat-petg-2026-11-03.gcode",
                         "print_duration": 573, "remaining_time_sec": 2096,
                         "state": "printing", "total_duration": 2669,
                         "uuid": "80baf1df-e748-4717-adf1-b9487f2c8f32"},
        "extruder": {"filament_detect_enable": 1, "filament_detected": 1,
                     "target": 260, "temperature": 259.6},
        "heater_bed": {"target": 70, "temperature": 69.9},
    }


def machine_payload(*, machine=(1, 0), state="", filename="", current_layer=0,
                    total_layer=None, progress=0, uuid="", exception_status=None,
                    reason_code=0, state_message=None,
                    nozzle=111.0, nozzle_target=0.0, bed=75.0, bed_target=0.0,
                    box=None, filament_detected=1, filament_detect_enable=1,
                    gcode=None, led_status=None) -> dict:
    """A 1002 result in the firmware's own shape (lowercase, sparse). Absent blocks (box,
    gcode_move_inf, led) mirror real replies, which carry only some of them."""
    print_status: dict = {"current_layer": current_layer, "filename": filename,
                          "print_duration": 0, "remaining_time_sec": 0, "state": state,
                          "total_duration": 0, "uuid": uuid}
    if total_layer is not None:
        print_status["total_layer"] = total_layer
    if state_message is not None:
        print_status["message"] = state_message
    payload = {
        "error_code": 0,
        "machine_status": {"exception_status": [] if exception_status is None else exception_status,
                           "progress": progress, "status": machine[0], "sub_status": machine[1],
                           "sub_status_reason_code": reason_code},
        "print_status": print_status,
        "extruder": {"filament_detect_enable": filament_detect_enable,
                     "filament_detected": filament_detected,
                     "target": nozzle_target, "temperature": nozzle},
        "heater_bed": {"target": bed_target, "temperature": bed},
    }
    if box is not None:
        payload["ztemperature_sensor"] = {"temperature": box}
    if gcode is not None:
        payload["gcode_move_inf"] = gcode
    if led_status is not None:
        payload["led"] = {"status": led_status}
    return payload


def machine_envelope(mutate=None, **kw) -> dict:
    payload = machine_payload(**kw)
    if mutate:
        mutate(payload)
    return payload


def tray(tray_id, status, *, ftype="PETG", brand="ELEGOO", color="00B0F0", mn=230, mx=260, **kw):
    entry = {"tray_id": tray_id, "status": status, "filament_type": ftype, "brand": brand,
             "filament_name": f"{brand} {ftype}".strip(), "filament_color": color,
             "min_nozzle_temp": mn, "max_nozzle_temp": mx}
    entry.update(kw)
    return entry


def canvas_payload(tray_list, *, connected=1, active_tray=-1, canvas_id=0, active_canvas=0,
                   auto_refill=False):
    """A 2005 result as the Home Assistant CC2 client parses it: snake_case under canvas_info,
    one connected unit."""
    return {"error_code": 0, "canvas_info": {
        "active_canvas_id": active_canvas, "active_tray_id": active_tray,
        "auto_refill": auto_refill,
        "canvas_list": [{"canvas_id": canvas_id, "connected": connected,
                         "tray_list": list(tray_list)}]}}


def observed_canvas() -> list[dict]:
    """The observed four-tray roster: A1/A2 empty, A3 Generic PETG loaded (status 1, the state
    the Home Assistant client itself marks enabled), A4 ELEGOO PETG active (status 2, the state
    an earlier Home Assistant build wrongly called unavailable)."""
    return [tray(0, 0, ftype="", brand="", color="000000", mn=0, mx=0),
            tray(1, 0, ftype="", brand="", color="000000", mn=0, mx=0),
            tray(2, 1, brand="Generic", color="7F7E83"),
            tray(3, 2, brand="ELEGOO", color="00B0F0")]


def canvas_envelope(tray_list=None, mutate=None, **kw) -> dict:
    payload = canvas_payload(observed_canvas() if tray_list is None else tray_list, **kw)
    if mutate:
        mutate(payload)
    return payload


def test_multiple_canvas_units_are_not_treated_as_one_connected_unit():
    payload = canvas_payload([tray(0, 1)])
    payload["canvas_info"]["canvas_list"].append(
        {"canvas_id": 1, "connected": 0, "tray_list": [tray(1, 1)]})
    with pytest.raises(st.Cc2DataError):
        st.Cc2CanvasStatus.from_result(payload)


SKULLBOWL = {  # one 1044 entry exactly as the protocol doc records it (fw 02.01.00.00)
    "filename": "ECC2_0.4_skullbowl_Elegoo Rapid PLA+ _0.2_1h26m.gcode",
    "size": 12484158, "create_time": 1789680958, "layer": 251, "print_time": 5137,
    "total_filament_used": 34.92, "last_print_time": 0, "total_print_times": 0,
    "type": "file", "color_map": [{"t": 0, "color": "#7F7E83", "name": "PLA"}],
}
PETG = {"filename": "denbjj-part-flat-petg-2026-11-03.gcode", "size": 4839523,
        "create_time": 1793757000, "layer": 240, "print_time": 2669,
        "total_filament_used": 12.5, "type": "file",
        "color_map": [{"t": 0, "color": "#000000", "name": "PETG"}]}


# --------------------------------------------------------------------------- scripted transport


class Scripted:
    """Stands in for a registered Cc2Transport session exactly as the real one behaves: every
    request is recorded and answered from a map of method -> result object | Exception |
    callable(params), and the call returns that result object itself — the transport hands its
    caller the reply's ``result`` and nothing else. A method with no entry answers an empty
    result, which every parser here must refuse rather than guess from."""

    def __init__(self, replies=None):
        self.replies = dict(replies or {})
        self.requests: list[tuple[int, dict]] = []

    async def request(self, method: int, params: dict | None = None, *, timeout_s=None) -> dict:
        self.requests.append((method, params or {}))
        reply = self.replies.get(method, {})
        if callable(reply):
            reply = reply(params or {})
        if isinstance(reply, BaseException):
            raise reply
        return copy.deepcopy(reply)

    def methods(self) -> list[int]:
        return [m for m, _ in self.requests]

    def file_params(self) -> list[dict]:
        return [p for m, p in self.requests if m == st.METHOD_FILES]


def files_transport(files: list[dict], *, probe="full", paging="honest",
                    total_when="always", extra=None):
    """A 1044 transport that models exactly ONE printer behaviour at a time.

    One reply in here is a capture: the un-paged probe of a firmware 02.01.00.00 printer, which
    answered ``{"storage_media": "local", "path": "/"}`` with ALL 54 of its files in one reply
    (``probe="full"`` — the default, and the measured behaviour). Everything else is a
    HYPOTHETICAL firmware, modelled on purpose: no paged 1044 answer has ever been observed on
    this machine, and Elegoo's LAN client (``dir``/``offset``/``limit``) and the protocol doc's
    lone example (``page``/``page_size``) do not agree, so the walk has to survive whichever
    printer it turns out to be facing.

    probe:      "full"       the measured answer — everything, in one reply
                "capped:N"   a reply that stops at N entries while its total counts more: the
                             truncation a paging walk exists for, never observed on this printer
                "censored:K" a reply that quietly omits the last K entries
    paging:     how the official ``{dir, offset, limit}`` request answers —
                "honest"     honours offset and limit
                "ignore"     ignores offset: the head of the list, every single time
                "overlap"    each page starts three entries before the previous one ended
                "shifted"    skips one entry at every offset, so a file goes unlisted
                "short"      answers one entry however many were asked for
                "refuse"     declines the paged request with an error_code
                "drift"      lets its total move mid-walk
    total_when: which replies carry ``total`` — "always", "paged" (only the paged asks report it)
                or "never" (the protocol doc's example reply carries none).
    extra:      dict, or callable(result, params) applied to every result, for one-off lies.
    """
    def reply(params):
        paged = "limit" in params
        if paged and paging == "refuse":
            return {"error_code": 3001}
        if paged:
            limit, offset = params["limit"], params["offset"]
            if paging == "ignore":
                offset = 0
            elif paging == "overlap":
                offset = max(0, offset - 3)
            elif paging == "shifted":
                offset += 1
            rows = files[offset:offset + limit]
            if paging == "short" and len(rows) > 1:
                rows = rows[:1]
        elif probe.startswith("capped:"):
            rows = files[:int(probe.split(":", 1)[1])]
        elif probe.startswith("censored:"):
            rows = files[:max(0, len(files) - int(probe.split(":", 1)[1]))]
        else:
            rows = list(files)
        result: dict = {"error_code": 0, "file_list": copy.deepcopy(rows)}
        if total_when == "always" or (total_when == "paged" and paged):
            result["total"] = len(files)
        if callable(extra):
            extra(result, params)
        elif extra:
            result.update(extra)
        return result
    return Scripted({st.METHOD_FILES: reply})


def full_files(n: int, width: int = 5) -> list[dict]:
    return [dict(SKULLBOWL, filename=f"batch-{i:0{width}d}.gcode", size=1000 + i)
            for i in range(n)]


# --------------------------------------------------------------------------- machine status (1002)


async def test_the_observed_idle_frame_is_idle_field_for_field():
    # verbatim direct read — this reply must parse, not be rejected for lacking fields the
    # printer has never sent (there is no print_status.Status and no Filename on this wire).
    m = st.Cc2MachineStatus.from_result(copy.deepcopy(OBSERVED_IDLE_1002))
    assert m.machine_status == 1 and m.sub_status == 0
    assert m.print_state is st.PrintState.IDLE and m.raw_print_state == ""
    assert m.job_filename is None and m.task_uuid is None and m.active_job is None
    assert m.error is None and m.is_idle
    ok, why = m.ready_for_job()
    assert ok and why is None
    assert (m.nozzle_temp, m.nozzle_target) == (111.0, 0.0)     # a warm idle hotend, still idle
    assert (m.bed_temp, m.bed_target) == (75.0, 0.0)
    assert m.box_temp is None                                    # block absent on this reply
    assert m.filament_detected == 1 and m.filament_detect_enable == 1
    assert m.current_layer == 0 and m.total_layer is None
    assert m.progress == 0 and m.sub_status_reason_code == 0
    assert m.exception_codes == () and m.position is None


async def test_an_active_print_is_never_read_as_idle():
    m = st.Cc2MachineStatus.from_result(printing_payload())
    assert m.print_state is st.PrintState.PRINTING and not m.is_idle
    assert m.active_job == "denbjj-part-flat-petg-2026-11-03.gcode"
    assert m.task_uuid == "80baf1df-e748-4717-adf1-b9487f2c8f32"
    assert (m.current_layer, m.total_layer, m.progress) == (51, None, 21)
    ok, why = m.ready_for_job()
    assert not ok and "denbjj-part-flat-petg-2026-11-03.gcode" in why and "printing" in why


async def test_a_paused_job_is_a_job_not_an_error_not_idle():
    # the 2026-11-04 direct read: machine 2/2502, layer and progress zeroed by the firmware,
    # bed still hot — paused carries no exception code and no reason code.
    m = st.Cc2MachineStatus.from_result(machine_payload(
        machine=(2, 2502), state="paused", filename="denbjj-part-flat-petg-2026-11-03.gcode",
        current_layer=0, total_layer=240, progress=0,
        uuid="80baf1df-e748-4717-adf1-b9487f2c8f32", nozzle=25.0, bed=69.0, bed_target=70.0))
    assert m.print_state is st.PrintState.PAUSED and m.error is None
    assert m.active_job == "denbjj-part-flat-petg-2026-11-03.gcode"
    ok, why = m.ready_for_job()
    assert not ok and "2502" in why  # not ready, reported by its own numbers


async def test_an_unknown_state_word_blocks_and_keeps_its_word():
    m = st.Cc2MachineStatus.from_result(machine_payload(machine=(2, 4321), state="levitating"))
    assert m.print_state is st.PrintState.UNKNOWN
    ok, why = m.ready_for_job()
    assert not ok and "levitating" in why
    named = st.Cc2MachineStatus.from_result(
        machine_payload(machine=(2, 4321), state="levitating", filename="part.gcode"))
    ok, why = named.ready_for_job()
    assert not ok and "levitating" in why and "part.gcode" in why


async def test_a_busy_print_state_that_names_no_file_is_uncertainty_not_clear():
    m = st.Cc2MachineStatus.from_result(machine_payload(machine=(2, 2401), state="paused",
                                                       filename=""))
    ok, why = m.ready_for_job()
    assert not ok and why is not None and "paused" in why
    # the stranger shape: machine claims idle while its print state still holds a nameless job
    idle_host = st.Cc2MachineStatus.from_result(machine_payload(machine=(1, 0), state="paused",
                                                               filename=""))
    ok, why = idle_host.ready_for_job()
    assert not ok and "named no file" in why


@pytest.mark.parametrize("code,word", [(0, "initializing"), (2, "printing"),
                                       (3, "filament"), (14, "emergency")])
async def test_other_machine_codes_are_documented_not_ready(code, word):
    m = st.Cc2MachineStatus.from_result(machine_payload(machine=(code, 0)))
    assert not m.is_idle
    ok, why = m.ready_for_job()
    assert not ok and word in why.lower()
    assert not (ok and why is None)  # paused mid-spool-swap must never borrow idle's meaning


async def test_an_unmapped_machine_status_reports_its_number():
    m = st.Cc2MachineStatus.from_result(machine_payload(machine=(42, 4321)))
    ok, why = m.ready_for_job()
    assert not ok and "42" in why and "4321" in why


async def test_machine_errors_block_a_start():
    exc = st.Cc2MachineStatus.from_result(
        machine_payload(exception_status=[{"code": 1102}, 7]))
    assert exc.error is not None and "1102" in exc.error and "7" in exc.error
    ok, why = exc.ready_for_job()
    assert not ok and "error" in why.lower()

    estop = st.Cc2MachineStatus.from_result(machine_payload(machine=(14, 0)))
    assert estop.error is not None and "14" in estop.error
    ok, why = estop.ready_for_job()
    assert not ok

    state_err = st.Cc2MachineStatus.from_result(
        machine_payload(machine=(2, 0), state="error", state_message="filament runout",
                        filename="part.gcode"))
    assert state_err.error is not None and "filament runout" in state_err.error
    ok, why = state_err.ready_for_job()
    assert not ok and "error" in why.lower()


async def test_a_hot_idle_printer_is_still_idle():
    m = st.Cc2MachineStatus.from_result(machine_payload(nozzle=210.0, bed=60.0))
    assert m.is_idle and m.nozzle_temp == 210.0


async def test_documented_frame_extras_read_in_when_present():
    m = st.Cc2MachineStatus.from_result(machine_payload(
        box=21.0, led_status=1, reason_code=4244,
        gcode={"x": 88.148, "y": 139.946, "z": 1.6, "speed": 9019, "speed_mode": 1}))
    assert m.position == (88.148, 139.946, 1.6)
    assert m.box_temp == 21.0 and m.box_target is None
    assert m.led_status == 1 and m.sub_status_reason_code == 4244 and m.speed_mode == 1


async def test_total_layer_and_state_message_are_read_when_the_printer_names_them():
    m = st.Cc2MachineStatus.from_result(machine_payload(total_layer=240, progress=21))
    assert m.total_layer == 240 and m.progress == 21


@pytest.mark.parametrize("mutate,why", [
    (lambda p: p.pop("machine_status"), "absent machine block"),
    (lambda p: p["machine_status"].pop("status"), "absent status"),
    (lambda p: p["machine_status"].update(status=True), "malformed status"),
    (lambda p: p["machine_status"].update(status="1"), "string status"),
    (lambda p: p["machine_status"].update(status=None), "null status"),
    (lambda p: p["machine_status"].pop("sub_status"), "absent sub-state"),
    (lambda p: p.pop("print_status"), "absent print block"),
    (lambda p: p["print_status"].pop("state"), "absent print state string"),
    (lambda p: p["print_status"].update(state=5), "non-string print state"),
    (lambda p: p["print_status"].update(state=None), "null print state"),
    (lambda p: p["machine_status"].update(exception_status="oops"), "malformed error list"),
])
async def test_a_status_we_cannot_read_is_refused_not_guessed(mutate, why):
    with pytest.raises(st.Cc2DataError) as e:
        st.Cc2MachineStatus.from_result(machine_envelope(mutate=mutate))
    assert e.value.code == "protocol_error"
    assert "1002" in e.value.message


async def test_a_non_object_result_is_a_protocol_error():
    with pytest.raises(st.Cc2DataError):
        st.Cc2MachineStatus.from_result("not an object")
    with pytest.raises(st.Cc2DataError):
        st.Cc2CanvasStatus.from_result([1, 2, 3])
    with pytest.raises(st.Cc2DataError):
        await st.list_files(Scripted({st.METHOD_FILES: []}))


async def test_missing_or_malformed_temperature_survives_as_none_never_a_guessed_number():
    m = st.Cc2MachineStatus.from_result(machine_envelope(mutate=lambda p: p.pop("heater_bed")))
    assert m.bed_temp is None and m.bed_target is None
    assert m.nozzle_temp == 111.0  # the rest of the envelope still stands
    m = st.Cc2MachineStatus.from_result(machine_envelope(mutate=lambda p: p.update(
        extruder={"temperature": "hot"})))
    assert m.nozzle_temp is None and m.nozzle_target is None
    assert m.filament_detected is None  # reporting fields, read or absent, never invented


# --------------------------------------------------------------------------- envelope rules


async def test_a_reported_error_code_is_never_read_as_success():
    for parse, method in [
        (lambda: st.Cc2MachineStatus.from_result(
            machine_envelope(mutate=lambda p: p.update(error_code=1003))), 1002),
        (lambda: st.Cc2CanvasStatus.from_result(
            canvas_envelope(mutate=lambda p: p.update(error_code=2))), 2005),
    ]:
        with pytest.raises(st.Cc2DataError) as e:
            parse()
        assert e.value.details["error_code"] != 0 and e.value.code == "protocol_error"
        assert e.value.details["method"] == method


async def test_a_malformed_error_code_is_malformed():
    with pytest.raises(st.Cc2DataError):
        st.Cc2MachineStatus.from_result(machine_envelope(mutate=lambda p: p.update(error_code="0")))


async def test_no_request_is_ever_retried():
    t = Scripted({st.METHOD_STATUS: st.Cc2DataError("protocol_error", "x")})
    with pytest.raises(PrinterError):
        await st.fetch_machine_status(t)
    assert t.methods() == [st.METHOD_STATUS]  # one ask, answered once: the transport's rule stands


async def test_transport_errors_pass_through_untouched():
    class Gone(Exception):
        pass
    t = Scripted({st.METHOD_STATUS: Gone("session lost")})
    with pytest.raises(Gone):
        await st.fetch_machine_status(t)


# --------------------------------------------------------------------------- CANVAS (2005)


async def test_canvas_reads_the_observed_connected_unit():
    canvas = st.Cc2CanvasStatus.from_result(canvas_envelope())
    assert canvas.connected and canvas.active_tray_id == -1 and not canvas.auto_refill
    assert canvas.tray_count == 4
    a3, a4 = canvas.get_tray(2), canvas.get_tray(3)
    assert a3.state is st.TrayState.PRESENT and a3.material == "PETG" and a3.brand == "Generic"
    assert a4.state is st.TrayState.ACTIVE and a4.material == "PETG"
    assert (a4.min_nozzle_temp, a4.max_nozzle_temp) == (230, 260)
    assert a4.color == "#00B0F0"  # the printer sends the hex bare; we show it with its #
    assert canvas.active_tray is None  # -1 means none active — the printer's own answer


async def test_canvas_accepts_the_bare_payload_form():
    payload = canvas_envelope()["canvas_info"]  # no canvas_info wrapper at all
    canvas = st.Cc2CanvasStatus.from_result(payload)
    assert canvas.tray_count == 4


async def test_canvas_marks_which_tray_it_named_active():
    canvas = st.Cc2CanvasStatus.from_result(canvas_envelope(active_tray=3))
    assert canvas.active_tray is canvas.get_tray(3)
    assert canvas.get_tray(3).active and not canvas.get_tray(2).active


async def test_a_disconnected_canvas_blocks_every_safety_decision():
    canvas = st.Cc2CanvasStatus.from_result(canvas_envelope(connected=0))
    loaded = canvas.get_tray(3)
    assert loaded.state is st.TrayState.ACTIVE  # the tray data reads fine...
    assert st.supply_decision(loaded, canvas=canvas) is not None  # ...but nothing can be proven


async def test_both_loaded_tray_states_may_run_a_job():
    # design.md spells the tray set "0 empty, 1 present, 2 active": the start admits either
    # loaded state, and the Home Assistant CC2 client itself marks status 1 enabled. The
    # material and rated-range fit is the start's own check, re-read there, not this one.
    canvas = st.Cc2CanvasStatus.from_result(canvas_envelope())
    present, active = canvas.get_tray(2), canvas.get_tray(3)
    assert present.state is st.TrayState.PRESENT and active.state is st.TrayState.ACTIVE
    assert present.may_run_job and active.may_run_job
    assert st.supply_decision(present, canvas=canvas) is None
    assert st.supply_decision(active, canvas=canvas) is None


async def test_tray_semantics_zero_one_two_and_unknown_raws_kept():
    raws = [-1, 0, 1, 2, 3, 7, 42]  # tray id i carries raw state raws[i]: id 1 is the empty one
    canvas = st.Cc2CanvasStatus.from_result(
        canvas_payload([tray(i, v) for i, v in enumerate(raws)]))
    by_state: dict[st.TrayState, list[int]] = {}
    for i in range(len(raws)):
        t = canvas.get_tray(i)
        assert t.raw_state == raws[i]
        by_state.setdefault(t.state, []).append(t.raw_state)
    assert by_state[st.TrayState.EMPTY] == [0]
    assert by_state[st.TrayState.PRESENT] == [1]
    assert by_state[st.TrayState.ACTIVE] == [2]
    assert set(by_state[st.TrayState.UNKNOWN]) == {-1, 3, 7, 42}
    states = [canvas.get_tray(i).state for i in range(len(raws))]
    assert set(states) == {st.TrayState.EMPTY, st.TrayState.PRESENT, st.TrayState.ACTIVE,
                           st.TrayState.UNKNOWN}
    assert not canvas.get_tray(1).may_run_job              # raw 0: empty feeds nothing
    assert canvas.get_tray(2).may_run_job                  # raw 1: present may, per the design
    assert canvas.get_tray(3).may_run_job                  # raw 2: active may
    for unknown_id in (0, 4, 5, 6):                        # raws -1, 3, 7, 42
        assert not canvas.get_tray(unknown_id).may_run_job  # unknown raws never may
    why = st.supply_decision(canvas.get_tray(5), canvas=canvas)
    assert why is not None and "7" in why                  # unknown reports its own number


async def test_supply_decision_ladder():
    canvas = st.Cc2CanvasStatus.from_result(canvas_envelope())
    assert st.supply_decision(canvas.get_tray(2), canvas=canvas) is None     # present: filament here
    assert st.supply_decision(canvas.get_tray(3), canvas=canvas) is None     # active: likewise
    assert st.supply_decision(canvas.get_tray(0), canvas=canvas) is not None  # empty: no filament
    away = st.Cc2CanvasStatus.from_result(canvas_payload([tray(9, 2)]))
    assert st.supply_decision(canvas.get_tray(7), canvas=canvas) is not None  # no such tray
    assert st.supply_decision(canvas.get_tray(3), canvas=away) is not None    # not on that canvas
    assert st.supply_decision(None, canvas=canvas) is not None                # nothing was given
    assert st.supply_decision(canvas.get_tray(3), canvas=canvas, canvas_connected=False) \
        is not None  # the caller says the canvas is dark


async def test_supply_decision_ignores_another_canvas_with_the_same_tray_id():
    other = st.Cc2CanvasStatus.from_result(canvas_payload([tray(3, 1)], canvas_id=1))
    mine = st.Cc2CanvasStatus.from_result(canvas_payload([tray(3, 1)], canvas_id=0))
    assert st.supply_decision(mine.get_tray(3), canvas=mine) is None
    assert st.supply_decision(mine.get_tray(3), canvas=other) is not None


async def test_material_and_range_are_handed_along_not_checked_here():
    # all material/range checks are reserved for the start: a loaded tray reads with its
    # range when the range reads, and with None when it doesn't — parse neither refuses a
    # clean absence nor believes a contradiction.
    canvas = st.Cc2CanvasStatus.from_result(canvas_payload([
        tray(1, 1, mn=None, mx=None),                       # present, unrated: still present
        tray(2, 1, mn=300, mx=200),                         # a range that contradicts itself
        tray(3, 2, mn=230, mx="hot"),                       # half a range is no range
    ]))
    assert (canvas.get_tray(1).min_nozzle_temp, canvas.get_tray(1).max_nozzle_temp) == (None, None)
    assert canvas.get_tray(1).may_run_job
    assert st.supply_decision(canvas.get_tray(1), canvas=canvas) is None  # range is the start's problem
    assert (canvas.get_tray(2).min_nozzle_temp, canvas.get_tray(2).max_nozzle_temp) == (None, None)
    assert (canvas.get_tray(3).min_nozzle_temp, canvas.get_tray(3).max_nozzle_temp) == (None, None)
    assert canvas.get_tray(3).raw["max_nozzle_temp"] == "hot"  # the raw reply keeps what it said


async def test_an_empty_tray_without_a_temp_range_is_still_an_empty_tray():
    canvas = st.Cc2CanvasStatus.from_result(canvas_payload([
        tray(0, 0, mn=0, mx=0, ftype="", brand="", color="000000")]))
    t = canvas.get_tray(0)
    assert t.state is st.TrayState.EMPTY and t.min_nozzle_temp is None
    assert not t.may_run_job


async def test_firmware_placeholder_strings_read_as_absent():
    canvas = st.Cc2CanvasStatus.from_result(canvas_payload([
        tray(0, 0, brand="—", color="000000", ftype=""), tray(1, 1, ftype="?")]))
    assert canvas.get_tray(0).brand is None and canvas.get_tray(0).material is None
    assert canvas.get_tray(1).material is None


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("canvas_info"),
    lambda p: p["canvas_info"].pop("canvas_list"),
    lambda p: p["canvas_info"].update(canvas_list=[]),
    lambda p: p["canvas_info"].update(canvas_list=["not a unit"]),
    lambda p: p["canvas_info"]["canvas_list"][0].update(connected="yes"),
    lambda p: p["canvas_info"]["canvas_list"][0].update(connected=True),
    lambda p: p["canvas_info"]["canvas_list"][0].pop("tray_list"),
    lambda p: p["canvas_info"]["canvas_list"][0].update(tray_list=["not a tray"]),
    lambda p: p["canvas_info"]["canvas_list"][0]["tray_list"][0].pop("tray_id"),
    lambda p: p["canvas_info"]["canvas_list"][0]["tray_list"][3].update(tray_id=True),
    lambda p: p["canvas_info"]["canvas_list"][0]["tray_list"][3].update(status="loaded"),
    lambda p: p["canvas_info"]["canvas_list"][0]["tray_list"][3].update(status=None),
    lambda p: p["canvas_info"]["canvas_list"][0]["tray_list"][3].update(status=True),
    lambda p: p["canvas_info"]["canvas_list"][0]["tray_list"][3].update(tray_id=-2),
    lambda p: p["canvas_info"]["canvas_list"][0]["tray_list"][3].update(filament_color="violet"),
    lambda p: p["canvas_info"].update(auto_refill="maybe"),
    lambda p: p["canvas_info"].update(active_tray_id="3"),
    lambda p: p["canvas_info"].update(active_tray_id=True),
])
async def test_canvas_data_we_cannot_trust_is_refused_wholesale(mutate):
    with pytest.raises(st.Cc2DataError) as e:
        st.Cc2CanvasStatus.from_result(canvas_envelope(mutate=mutate))
    assert e.value.code == "protocol_error" and "2005" in e.value.message


async def test_a_silently_dropped_tray_would_lie_so_the_reply_is_refused():
    # the unit carries four trays; a parser that skipped a malformed one would report the
    # printer as having three. The count must match or nothing is claimed.
    def pad_three(p):
        p["canvas_info"]["canvas_list"][0]["tray_list"] = [tray(0, 0), tray(1, 1), "gone"]
    with pytest.raises(st.Cc2DataError):
        st.Cc2CanvasStatus.from_result(canvas_envelope(mutate=pad_three))


async def test_canvas_auto_refill_and_active_tray_are_strict():
    canvas = st.Cc2CanvasStatus.from_result(canvas_envelope(auto_refill=1))
    assert canvas.auto_refill is True  # the observed value, seen true on the direct read


# --------------------------------------------------------------------------- file inventory (1044)


PROBE = {"storage_media": "local", "path": "/"}   # the only 1044 request ever observed to answer


def paged(offset: int, limit: int = st.FILE_REQUEST_LIMIT) -> dict:
    """One paged ask, spelled exactly as Elegoo's own LAN web client spells it: the storage, the
    root as ``dir``, and ``offset``/``limit``. Not ``page``/``page_size`` — see the shape test."""
    return {"storage_media": "local", "dir": "/", "offset": offset, "limit": limit}


async def test_the_probe_is_the_measured_un_paged_shape_and_it_costs_one_request():
    # When the un-paged reply certifies itself that is the whole read: no page, no page_size,
    # no offset, no limit. Paging parameters only ever appear in a request that needed them.
    t = files_transport(full_files(54))
    inv = await st.list_files(t)
    assert inv.count == 54 and inv.total == 54 and inv.pages == 1
    assert t.file_params() == [PROBE]


async def test_the_measured_54_file_one_reply_listing_is_complete_as_it_arrives():
    # fw 02.01.00.00, protocol doc: "without page/page_size the printer returned all 54 files in
    # one response". That reply is the listing — every name in it, in the printer's own order.
    files = full_files(54)
    inv = await st.list_files(files_transport(files))
    assert [f.filename for f in inv.files] == [f["filename"] for f in files]
    assert inv.count == 54 and inv.pages == 1


async def test_a_probe_without_a_total_is_certified_by_the_official_paged_ask():
    # The protocol doc's example 1044 reply carries no total, so "no total on the un-paged reply"
    # is an expected shape rather than a defect. Elegoo's paged request does report one, and when
    # it does with every un-paged name present, that is a proven listing — for one extra read.
    t = files_transport(full_files(54), total_when="paged")
    inv = await st.list_files(t)
    assert inv.count == 54 and inv.total == 54 and inv.pages == 1
    assert t.file_params() == [PROBE, paged(0)]


async def test_a_capped_probe_walks_the_official_offset_limit_shape():
    # A HYPOTHETICAL firmware whose un-paged reply stops at 100 entries while its total counts
    # 112 — never observed on this printer, and the only thing that justifies paging at all. The
    # walk re-reads from offset 0 instead of splicing two request families into one listing.
    files = full_files(112)
    t = files_transport(files, probe="capped:100")
    inv = await st.list_files(t)
    assert inv.count == 112 and inv.total == 112 and inv.pages == 2
    assert [f.filename for f in inv.files] == [f["filename"] for f in files]
    assert t.file_params() == [PROBE, paged(0), paged(100)]


@pytest.mark.parametrize("n", [11, 19, 20, 21, 25])
async def test_walk_boundaries_land_exactly_on_the_total(monkeypatch, n):
    monkeypatch.setattr(st, "FILE_REQUEST_LIMIT", 10)   # small, so the arithmetic is legible
    t = files_transport(full_files(n), probe="capped:1")
    inv = await st.list_files(t)
    assert inv.count == n and inv.total == n and inv.pages == -(-n // 10)
    assert t.file_params() == [PROBE] + [paged(o, 10) for o in range(0, n, 10)]


async def test_page_page_size_are_never_asked_for():
    # The doc example showing them is the whole of their evidence, and Elegoo's own software for
    # this device sends offset/limit instead. Asking a real printer with the shape first-party
    # software never sends would be testing an invention against someone's hardware.
    for scenario in (dict(), dict(probe="capped:2"), dict(paging="ignore"),
                     dict(total_when="never"), dict(paging="refuse"), dict(paging="overlap")):
        t = files_transport(full_files(20), **scenario)
        with contextlib.suppress(st.Cc2DataError):
            await st.list_files(t)
        keys = {k for params in t.file_params() for k in params}
        assert keys <= {"storage_media", "path", "dir", "offset", "limit"}, t.file_params()


async def test_a_printer_that_ignores_offset_is_caught_by_the_duplicate_gate():
    # The failure the walk must never paper over: a firmware that accepts offset and answers the
    # head of the list anyway. Its second page repeats its first, and one name arriving twice
    # refuses the whole read — nothing is merged, and nothing is asked a second time.
    t = files_transport(full_files(112), probe="capped:100", paging="ignore")
    with pytest.raises(st.Cc2IncompleteDataError) as e:
        await st.list_files(t)
    assert e.value.details["reason"] == "pagination_overlap"
    assert "batch-00000.gcode" in e.value.message        # the head of the list, handed back twice
    assert t.file_params() == [PROBE, paged(0), paged(100)]  # found out, not fought


async def test_pages_that_overlap_are_refused_not_merged():
    # Elegoo's client merges pages by filename; a merge is exactly how an overlap becomes
    # invisible. Here the second page starts three entries early and the walk says so instead.
    t = files_transport(full_files(112), probe="capped:100", paging="overlap")
    with pytest.raises(st.Cc2IncompleteDataError) as e:
        await st.list_files(t)
    assert e.value.details["reason"] == "pagination_overlap"
    assert len(t.file_params()) == 3                     # probe + two pages, then the refusal


async def test_a_paged_family_that_loses_a_file_the_probe_named_is_refused():
    # Certifying against the printer's own total is only honest while the two request shapes
    # agree about what the printer holds. A firmware that skips an entry at every offset reaches a
    # perfectly consistent count while quietly not listing a file the un-paged reply had named.
    def counted_by_what_it_lists(result, params):
        if "limit" in params:
            result["total"] = len(result["file_list"])
    t = files_transport(full_files(6), paging="shifted", total_when="never",
                        extra=counted_by_what_it_lists)
    with pytest.raises(st.Cc2IncompleteDataError) as e:
        await st.list_files(t)
    assert e.value.details["reason"] == "pagination_mismatch"
    assert "batch-00000.gcode" in e.value.message        # the file that went unlisted


async def test_a_paged_ask_the_printer_refuses_refuses_the_listing():
    t = files_transport(full_files(112), probe="capped:100", paging="refuse")
    with pytest.raises(st.Cc2DataError) as e:
        await st.list_files(t)
    assert e.value.details["error_code"] == 3001   # the printer's refusal, not our invention
    assert t.file_params() == [PROBE, paged(0)]    # refused once, and asked exactly once


async def test_a_short_page_that_leaves_the_total_short_is_incomplete_not_creative():
    # A page shorter than it was allowed to be means the printer ran out — which contradicts a
    # total it has not delivered. Two of its numbers cannot both be true, so nothing is claimed.
    t = files_transport(full_files(112), probe="capped:100", paging="short")
    with pytest.raises(st.Cc2IncompleteDataError) as e:
        await st.list_files(t)
    assert "incomplete" in e.value.details["reason"]
    assert "112" in e.value.message


async def test_a_total_that_moves_mid_walk_is_refused():
    def drift(result, params):
        if "limit" in params and params["offset"] > 0:
            result["total"] = 999            # the printer's count moved under the read
    t = files_transport(full_files(250), probe="capped:100", extra=drift)
    with pytest.raises(st.Cc2IncompleteDataError) as e:
        await st.list_files(t)
    assert "moved" in e.value.message


async def test_a_walk_that_never_runs_out_stops_at_its_bound_and_says_so():
    t = files_transport(full_files(10 ** 4), probe="capped:1")
    with pytest.raises(st.Cc2IncompleteDataError) as e:
        await st.list_files(t, max_pages=5)
    assert len(t.file_params()) == 6         # probe + 5 pages: a bounded walk, then the refusal
    assert "bound of 5" in e.value.message


async def test_an_empty_page_that_never_reached_the_total_is_a_stall_not_an_end():
    def vanished(result, params):
        if "limit" in params:
            result["file_list"] = []         # the paged ask answers nothing at all
    t = files_transport(full_files(112), probe="capped:100", extra=vanished)
    with pytest.raises(st.Cc2IncompleteDataError) as e:
        await st.list_files(t)
    assert "stalled" in e.value.message and "112" in e.value.message
    assert len(t.file_params()) == 2         # and it stopped after the empty page, once


@pytest.mark.parametrize("scenario", [
    dict(probe="capped:100", paging="ignore"),
    dict(probe="capped:100", paging="overlap"),
    dict(probe="capped:100", paging="short"),
    dict(probe="capped:100", paging="refuse"),
    dict(probe="capped:100", paging="shifted"),
    dict(total_when="never"),
    dict(paging="shifted", total_when="never"),
    dict(total_when="paged"),
])
async def test_nothing_is_ever_asked_twice_whatever_the_printer_does(scenario):
    # No automatic retry in ANY branch: every 1044 ask of a read is distinct and offsets only ever
    # advance. A lost or self-contradictory reply is a refusal, never a second chance at it —
    # which is also why a printer that ignores offset walks into the duplicate gate immediately.
    t = files_transport(full_files(112), **scenario)
    with contextlib.suppress(st.Cc2DataError):
        await st.list_files(t)
    asks = t.file_params()
    assert asks, "the read asked nothing at all"
    signatures = [tuple(sorted(params.items())) for params in asks]
    assert len(set(signatures)) == len(signatures), f"the same ask went out twice: {asks}"
    offsets = [p["offset"] for p in asks if "offset" in p]
    assert offsets == sorted(set(offsets)), asks


async def test_a_listing_nobody_counted_is_refused_naming_the_exact_blocker():
    # The honest end of the road: a firmware that reports no total anywhere AND repeats itself
    # under offset/limit cannot have its completeness proven from its replies. This is the case
    # this module must refuse, and the refusal says which fact is missing rather than guessing.
    t = files_transport(full_files(54), total_when="never", paging="ignore")
    with pytest.raises(st.Cc2IncompleteDataError) as e:
        await st.list_files(t)
    assert "no 1044 reply" in e.value.message and "total" in e.value.message
    assert "ignores offset/limit" in e.value.message
    assert "no listing" in e.value.message.lower()


async def test_a_reply_that_stops_answering_total_honestly_still_answers():
    t = Scripted({st.METHOD_FILES: {"file_list": [PETG]}})   # one entry, no total: unprovable
    with pytest.raises(st.Cc2IncompleteDataError):
        await st.list_files(t)

    over = Scripted({st.METHOD_FILES: {"error_code": 0, "file_list": [PETG, SKULLBOWL],
                                       "total": 1}})
    with pytest.raises(st.Cc2DataError):  # two entries against a total of one: does not add up
        await st.list_files(over)


async def test_empty_printer_is_a_complete_empty_inventory():
    t = files_transport([])
    inv = await st.list_files(t)
    assert inv.files == () and inv.count == 0 and inv.total == 0 and inv.pages == 1
    assert t.file_params() == [PROBE]


async def test_an_empty_reply_without_a_total_still_needs_the_printers_number():
    # An empty un-paged reply proves nothing on its own — an empty printer and a refusing one
    # look identical. It becomes a certified empty listing when the printer counts zero files.
    t = files_transport([], total_when="paged")
    inv = await st.list_files(t)
    assert inv.files == () and inv.count == 0 and inv.total == 0 and inv.pages == 1
    assert t.file_params() == [PROBE, paged(0)]


async def test_a_matching_total_needs_no_walk():
    t = files_transport([SKULLBOWL, PETG])
    inv = await st.list_files(t)
    assert [f.filename for f in inv.files] == [SKULLBOWL["filename"], PETG["filename"]]
    assert inv.total == 2 and inv.pages == 1 and len(t.file_params()) == 1
    skull = inv.files[0]
    assert skull.size == SKULLBOWL["size"] and skull.layers == 251 and skull.print_seconds == 5137
    assert skull.filament_grams == pytest.approx(34.92)
    assert skull.tools == (0,) and skull.colors == ("#7F7E83",)
    assert str(skull.created).startswith("2026-")
    assert inv.files[1].size == 4839523


async def test_single_page_duplicate_names_are_not_a_complete_inventory():
    with pytest.raises(st.Cc2IncompleteDataError) as e:
        await st.list_files(files_transport([PETG, PETG]))
    assert e.value.details["reason"] == "pagination_overlap"


async def test_only_the_verified_storage_is_askable():
    # Nobody has ever asked a removable volume for anything: the sources offer two different
    # parameter spellings for them and no reply, so there is no request to send. Asking anyway
    # would be inventing a request shape against someone's printer.
    t = files_transport([])
    with pytest.raises(ValueError):
        await st.list_files(t, storage="u-disk")
    assert t.file_params() == []


@pytest.mark.parametrize("missing", ["exception_status", "filename"])
async def test_missing_safety_state_is_not_idle(missing):
    payload = machine_payload()
    del payload["machine_status" if missing == "exception_status" else "print_status"][missing]
    with pytest.raises(st.Cc2DataError):
        st.Cc2MachineStatus.from_result(payload)


async def test_malformed_exception_entry_is_not_clear():
    with pytest.raises(st.Cc2DataError):
        st.Cc2MachineStatus.from_result(machine_payload(exception_status=[{"unknown": "error"}]))


@pytest.mark.parametrize("mutate", [
    lambda r, p: r.update(total="112"),
    lambda r, p: r.update(total=True),
    lambda r, p: r.update(total=-1),
    lambda r, p: r.pop("file_list"),
    lambda r, p: r.update(file_list="not a list"),
    lambda r, p: r.update(error_code=4001),
])
async def test_malformed_pages_are_refused_not_interpreted(mutate):
    t = files_transport(full_files(7), extra=mutate)
    with pytest.raises(st.Cc2DataError):
        await st.list_files(t)


@pytest.mark.parametrize("entry", [
    "not a dict",
    {"size": 1},
    {"filename": ""},
    {"filename": "  "},
    {"filename": "a/gcode.gcode", "size": 1},
    {"filename": "/abs/gcode.gcode", "size": 1},
    {"filename": "..\\..\\etc", "size": 1},
    {"filename": "nul\x00.gcode", "size": 1},
    {"filename": "ctrl\x07.gcode", "size": 1},
    {"filename": "x" * 300, "size": 1},
    {"filename": "ok.gcode"},
    {"filename": "ok.gcode", "size": "many"},
    {"filename": "ok.gcode", "size": -5},
    {"filename": "ok.gcode", "size": True},
    {"filename": {"sub": "object"}, "size": 1},
])
async def test_one_unusable_entry_refuses_the_whole_inventory(entry):
    # "complete or nothing": a listing with a hole in it is not a listing. The Home Assistant
    # helper drops unparseable entries and reports the rest as the truth; we refuse instead.
    t = Scripted({st.METHOD_FILES: {"error_code": 0, "total": 2,
                                    "file_list": [entry, copy.deepcopy(PETG)]}})
    with pytest.raises(st.Cc2DataError) as e:
        await st.list_files(t)
    assert e.value.code == "protocol_error"


async def test_directories_are_not_files():
    t = Scripted({st.METHOD_FILES: {"error_code": 0, "total": 2,
                                    "file_list": [{"filename": "models", "type": "dir"},
                                                  copy.deepcopy(PETG)]}})
    with pytest.raises(st.Cc2DataError):
        await st.list_files(t)


async def test_report_noise_never_breaks_an_entry_we_can_act_on():
    entry = dict(PETG, create_time=10 ** 20, layer="lots", print_time=None,
                 total_filament_used="a spool", extra_field={"future": True})
    t = Scripted({st.METHOD_FILES: {"error_code": 0, "total": 1, "file_list": [entry]}})
    inv = await st.list_files(t)
    f = inv.files[0]
    assert f.size == 4839523 and f.created is None and f.layers is None
    assert f.print_seconds is None and f.filament_grams is None
    assert f.tools == (0,) and f.colors == ("#000000",)
    assert f.raw["extra_field"] == {"future": True}


async def test_a_bare_hex_colour_is_normalised_and_a_stranger_refused():
    entry = dict(PETG, color_map=[{"t": 0, "color": "000000"}])
    t = Scripted({st.METHOD_FILES: {"error_code": 0, "total": 1, "file_list": [entry]}})
    assert (await st.list_files(t)).files[0].colors == ("#000000",)
    bad = dict(PETG, color_map=[{"t": 0, "color": "not-a-hex"}])
    t = Scripted({st.METHOD_FILES: {"error_code": 0, "total": 1, "file_list": [bad]}})
    with pytest.raises(st.Cc2DataError):
        await st.list_files(t)


async def test_find_file_is_exact_and_says_nothing_it_didnt_see():
    t = files_transport([SKULLBOWL, PETG])
    inv = await st.list_files(t)
    assert inv.find_file(PETG["filename"]).size == 4839523
    assert inv.find_file("denbjj-part-flat-petg-2026-11-03.gcode ") is None
    assert inv.find_file("DENBJJ-PART-FLAT-PETG-2026-11-03.GCODE") is None  # exact match only


# --------------------------------------------------------------------------- combined reads


async def test_cc2_status_reads_machine_and_canvas_together():
    t = Scripted({st.METHOD_STATUS: machine_payload(machine=(2, 2075), state="printing",
                                                    filename="part.gcode"),
                  st.METHOD_CANVAS: canvas_envelope()})
    status = await st.fetch_status(t)
    assert status.machine.active_job == "part.gcode"
    assert status.canvas.get_tray(3).state is st.TrayState.ACTIVE
    assert t.methods() == [st.METHOD_STATUS, st.METHOD_CANVAS]  # asked once each, in one breath


async def test_combined_status_reports_its_uncertainty():
    status = await st.fetch_status(Scripted({st.METHOD_STATUS: copy.deepcopy(OBSERVED_IDLE_1002),
                                             st.METHOD_CANVAS: canvas_envelope()}))
    ok, why = status.ready_for_job()
    assert ok and why is None
    bad = await st.fetch_status(Scripted({st.METHOD_STATUS: printing_payload(),
                                          st.METHOD_CANVAS: canvas_envelope(connected=0)}))
    ok, why = bad.ready_for_job()
    assert not ok and why is not None


async def test_a_failing_half_of_the_combined_read_is_no_silent_half():
    t = Scripted({st.METHOD_STATUS: copy.deepcopy(OBSERVED_IDLE_1002),
                  st.METHOD_CANVAS: st.Cc2DataError("protocol_error", "2005")})
    with pytest.raises(PrinterError):
        await st.fetch_status(t)


async def test_combined_reads_rely_on_the_transport_s_id_correlation_not_their_own_order():
    replies = {st.METHOD_STATUS: printing_payload(), st.METHOD_CANVAS: canvas_envelope()}

    class Slow(Scripted):
        """Answers 1002 late and 2005 first: only id correlation can match them back."""

        async def request(self, method, params=None, *, timeout_s=None):
            if method == st.METHOD_STATUS:
                import asyncio
                await asyncio.sleep(0.02)
            return await super().request(method, params, timeout_s=timeout_s)

    session = Slow(replies)
    status = await st.fetch_status(session)
    assert status.machine.active_job == "denbjj-part-flat-petg-2026-11-03.gcode"
    assert status.canvas.tray_count == 4
    assert sorted(session.methods()) == sorted([st.METHOD_STATUS, st.METHOD_CANVAS])
    assert len(session.requests) == 2  # each asked exactly once, out-of-order answers and all
