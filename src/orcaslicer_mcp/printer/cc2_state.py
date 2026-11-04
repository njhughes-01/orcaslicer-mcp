"""What the CC2 actually said, read fail-closed: machine status (1002), CANVAS filament (2005),
and a file inventory whose completeness was proven at the reply level (1044). The transport
moves JSON and says truthfully what happened; this module decides what it means — and refuses to
mean anything at all when the words don't hold.

Protocol facts are the ones observed on the real printer (firmware 02.01.00.00) and documented
in the sources this adapter was researched against, all local: the observed 1002 idle reply
(machine_status.status 1 / sub_status 0 with print_status.state the empty string; extruder and
heater_bed as scalar ``temperature`` plus ``target``), the observed printing reply (status 2,
print_status.state "printing", filename set), the protocol doc's print_status table (lowercase
``filename``, ``uuid``, ``current_layer``, ``total_layer``, ``progress``, ``state`` — a string:
standby, printing, paused, complete, cancelled, error) and machine-code table (0 initializing,
1 idle, 2 printing … 14 emergency stop, 15 power-loss recovery), the 2005 ``canvas_info`` shape
the Home Assistant CC2 client parses field-for-field (snake_case units and trays), and the 1044
request exactly as the upstream integration and the protocol doc send it: ``{"storage_media":
"local", "path": "/"}`` — no paging parameters at all, the one 1044 shape ever observed to answer
this printer (fw 02.01.00.00 returned all 54 files in one such reply). For paging there are two
candidate shapes in the sources and neither has been observed answered here: Elegoo's own LAN web
client sends ``dir``/``offset``/``limit``, while the protocol doc's one example shows
``page``/``page_size`` — a spelling first-party software for this device never uses, so the walk
asks with the client's shape and trusts only its replies. Nothing here invents a field the
printer has never been seen to send: the uppercase ``print_status.Status``/``Filename`` one
earlier draft required were another library's *mapped* names, and real replies answered them
never.

Two rules hold everywhere. Fields a safety decision stands on (the machine status pair, the
print state string, a tray's identity, a file's name and size) are required, and absent-or-
malformed refuses the reply — an unreadable reply is a refusal, never a guess, and an active
print is never read as idle. Fields that only inform (temperatures, layers, colours, rated
ranges) read as None when absent or malformed, with the raw reply kept; the rated nozzle range
in particular is a start-time check, and this module only hands it along when it reads cleanly.
And ``error_code``: absent means success, as the observed success answers carry no code or carry
0, and any value other than 0 is the printer declining on record — never read as success.
Nothing here sends anything twice: a lost reply is the transport's honest timeout, not a cue to
ask again and risk a second read of a moving machine."""
from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from .errors import PrinterError

METHOD_STATUS = 1002
METHOD_CANVAS = 2005
METHOD_FILES = 1044

FILE_REQUEST_LIMIT = 100  # the page size OUR paged asks ask for; ours, not the printer's — see
                          # list_files: no source on record establishes a page size this firmware
                          # imposes, so a short reply is only read as the end of the listing when
                          # the printer's own total says so (the official client's own default
                          # limit is 20 and the protocol doc's example sends 50)
MAX_FILE_PAGES = 64       # generous for this printer, finite so a stalling walk cannot loop
_HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}")
_FILENAME_MAX = 255

# The machine codes the protocol doc enumerates for this firmware (0-15). A number off the list
# reports itself as unknown, with its digits, rather than borrowing a neighbour's meaning.
_MACHINE_WORDS = {
    0: "initializing", 1: "idle", 2: "printing", 3: "filament operating",
    4: "filament operating (alt)", 5: "auto-leveling", 6: "PID calibrating",
    7: "resonance testing", 8: "self-checking", 9: "updating", 10: "homing",
    11: "file transferring", 12: "video composing", 13: "extruder operating",
    14: "emergency stop", 15: "power-loss recovery",
}

# print_status.state, exactly as the protocol doc lists the Klipper states, plus the empty
# string this printer actually sends when idle (observed 2026-11-04, machine status 1).
_PRINT_STATE_WORDS = {
    "": "idle", "standby": "standby", "printing": "printing", "paused": "paused",
    "complete": "complete", "cancelled": "cancelled", "error": "error",
}


class Cc2DataError(PrinterError):
    """A reply that cannot be read: absent, malformed or refused (a nonzero ``error_code``).
    It always names its method — callers quote it, tests prove nothing was guessed from it."""


class Cc2IncompleteDataError(Cc2DataError):
    """Data the printer says is one size and sent another — above all a file listing whose
    later pages could not be proven. Callers distinguish this from a plain malformed reply
    because it may be worth asking again later (a human decision, never an automatic retry)."""


class TrayState(Enum):
    """What one CANVAS tray says about its filament. The four members are the whole truth:
    raw status numbers outside 0/1/2 become UNKNOWN with their number kept, never a guess."""
    EMPTY = "empty"        # raw 0 — no filament
    PRESENT = "present"    # raw 1 — loaded; the state the design spells "present"
    ACTIVE = "active"      # raw 2 — loaded and the active spool; observed feeding a completed print
    UNKNOWN = "unknown"    # any other raw value, kept in raw_state and trusted with nothing


class PrintState(Enum):
    """``print_status.state`` read fail-closed: the documented Klipper words plus the empty
    string observed for idle. Any other string is UNKNOWN with the word kept — which blocks a
    start and is never, on its own authority, a reason to believe the bed is clear."""
    IDLE = "idle"              # "" — the observed idle value
    STANDBY = "standby"        # documented
    PRINTING = "printing"      # observed live with machine status 2 and a filename
    PAUSED = "paused"          # documented
    COMPLETE = "complete"      # documented
    CANCELLED = "cancelled"    # documented
    ERROR = "error"            # documented; ``message`` carries the words
    UNKNOWN = "unknown"        # any other string, kept in raw_state and trusted with nothing


# --------------------------------------------------------------------------- field readers


def _required(value: Any, parent: dict, name: str, method: int, label: str,
              reason: str = "absent") -> Any:
    """The value of a required field. A reply that carries it wrong is malformed (the caller's
    type check says so); a reply that doesn't carry it at all is absent — both refuse the reply,
    under words that say which, and every message names its method."""
    if name not in parent:
        raise Cc2DataError("protocol_error",
                           f"The printer's {method} reply carries no {label} — refusing to act "
                           "without it rather than assuming a state.", reason=f"{reason}_absent")
    return value


def _int(value: Any, parent: dict, name: str, method: int, label: str,
         reason: str = "status") -> int:
    v = _required(value, parent, name, method, label, reason)
    # a bool is an int in Python and it is not a status code
    if isinstance(v, bool) or not isinstance(v, int):
        raise Cc2DataError("protocol_error",
                           f"The printer's {method} reply {label} is malformed "
                           f"({type(v).__name__}), so its state can't be trusted.",
                           reason="status_malformed")
    return v


def _int_or_none(value: Any) -> int | None:
    return None if isinstance(value, bool) or not isinstance(value, int) else value


def _nonneg_int_or_none(value: Any) -> int | None:
    n = _int_or_none(value)
    return n if n is not None and n >= 0 else None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _temps(container: Any, target_key: str = "target") -> tuple[float | None, float | None]:
    """(actual, target) from one heater block in the only form this firmware is known to send:
    a scalar ``temperature`` plus a separate ``target`` (both observed, both documented). An
    absent or unparseable block is (None, None) — no temperature is ever invented."""
    if not isinstance(container, dict):
        return None, None
    return _num(container.get("temperature")), _num(container.get(target_key))


def _check_envelope(result: dict, method: int) -> int:
    """``error_code``: absent means success (the observed success answers carry no code), a
    non-integer value is malformed, and any nonzero value is the printer declining on record —
    the same error_code 1003 that once rejected float temperatures is never a green light."""
    if "error_code" not in result:
        return 0
    code = result["error_code"]
    if isinstance(code, bool) or not isinstance(code, int):
        raise Cc2DataError("protocol_error",
                           f"The printer's {method} reply carries an error code that cannot be "
                           f"read ({type(code).__name__}).", method=method,
                           reason="envelope_malformed")
    if code != 0:
        raise Cc2DataError("protocol_error",
                           f"The printer declined method {method} (error_code {code}).",
                           method=method, error_code=code, reason="declined")
    return code


# --------------------------------------------------------------------------- status (1002)


@dataclass(frozen=True)
class Cc2MachineStatus:
    """The machine as method 1002 answers it on the wire — ``machine_status.status`` and
    ``.sub_status`` as integers, ``print_status.state`` as the string the firmware names its own
    job state with. ``raw`` is the reply as it arrived (kept for reporting and future fields —
    it is whatever the firmware sends, so read it on purpose, not by dumping)."""
    machine_status: int
    sub_status: int
    print_state: PrintState
    raw_print_state: str                # the wire string, "" included, kept whatever it read as
    job_filename: str | None = None
    task_uuid: str | None = None
    current_layer: int | None = None
    total_layer: int | None = None
    progress: int | None = None
    sub_status_reason_code: int | None = None
    state_message: str | None = None    # print_status.message, the error words when state=error
    exception_codes: tuple[int, ...] = ()
    exception_raw: tuple = ()
    nozzle_temp: float | None = None
    nozzle_target: float | None = None
    bed_temp: float | None = None
    bed_target: float | None = None
    box_temp: float | None = None
    box_target: float | None = None
    filament_detected: int | None = None
    filament_detect_enable: int | None = None
    position: tuple[float, float, float] | None = None
    speed_mode: int | None = None
    led_status: int | None = None
    raw: dict = field(default_factory=dict, compare=False)

    @classmethod
    def from_result(cls, result: Any) -> "Cc2MachineStatus":
        """Parse a 1002 result exactly as this firmware answers it. The state trio —
        machine_status.status, .sub_status and print_status.state — is what every safety
        decision stands on, so any of them absent or malformed refuses the whole reply;
        a status we cannot read is never idle."""
        if not isinstance(result, dict):
            raise Cc2DataError("protocol_error",
                               f"The printer's {METHOD_STATUS} reply was not an object at all.",
                               reason="status_malformed")
        _check_envelope(result, METHOD_STATUS)
        ms = _required(result.get("machine_status"), result, "machine_status", METHOD_STATUS,
                       "machine state block", "status")
        if not isinstance(ms, dict):
            raise Cc2DataError("protocol_error",
                               f"The printer's {METHOD_STATUS} machine state block is malformed "
                               f"({type(ms).__name__}), so no state can be trusted.",
                               reason="status_malformed")
        ps = _required(result.get("print_status"), result, "print_status", METHOD_STATUS,
                       "print state block", "status")
        if not isinstance(ps, dict):
            raise Cc2DataError("protocol_error",
                               f"The printer's {METHOD_STATUS} print state block is malformed "
                               f"({type(ps).__name__}), so no state can be trusted.",
                               reason="status_malformed")
        status = _int(ms.get("status"), ms, "status", METHOD_STATUS, "machine state")
        sub = _int(ms.get("sub_status"), ms, "sub_status", METHOD_STATUS, "sub-state")
        state_raw = _required(ps.get("state"), ps, "state", METHOD_STATUS, "print state string")
        if not isinstance(state_raw, str):
            raise Cc2DataError("protocol_error",
                               f"The printer's {METHOD_STATUS} print state string is malformed "
                               f"({type(state_raw).__name__}), so the job state can't be trusted.",
                               reason="status_malformed")
        print_state = PrintState(_PRINT_STATE_WORDS[state_raw]) if state_raw in _PRINT_STATE_WORDS \
            else PrintState.UNKNOWN

        if not isinstance(ps.get("filename"), str):
            raise Cc2DataError("protocol_error", "The printer did not provide a valid job filename.",
                               reason="status_malformed")
        exc = ms.get("exception_status")
        if not isinstance(exc, list):
            raise Cc2DataError("protocol_error",
                               f"The printer's {METHOD_STATUS} error list is malformed, so "
                               "'no errors' could never be proven.", reason="status_malformed")
        codes: list[int] = []
        for item in exc:
            code = item.get("code") if isinstance(item, dict) else item
            c = _int_or_none(code)
            if c is None:
                raise Cc2DataError("protocol_error", "The printer returned an unreadable error entry.",
                                   reason="status_malformed")
            if c:
                codes.append(c)

        ext = result.get("extruder")
        bed = result.get("heater_bed")
        box = result.get("ztemperature_sensor")
        nozzle, nozzle_t = _temps(ext)
        bed_actual, bed_target = _temps(bed)
        box_actual, box_target = _temps(box)
        # the documented frame names the position block gcode_move_inf; the integration has also
        # been read parsing gcode_move — accept either, and absence simply means no position.
        gm = result.get("gcode_move_inf")
        if not isinstance(gm, dict):
            gm = result.get("gcode_move") if isinstance(result.get("gcode_move"), dict) else {}
        x, y, z = (_num(gm.get(k)) for k in ("x", "y", "z"))
        position = None if None in (x, y, z) else (x, y, z)
        led = result.get("led") if isinstance(result.get("led"), dict) else {}
        progress = _nonneg_int_or_none(ps.get("progress"))
        if progress is None:
            progress = _nonneg_int_or_none(ms.get("progress"))  # documented in both blocks
        return cls(
            machine_status=status, sub_status=sub, print_state=print_state, raw_print_state=state_raw,
            job_filename=_str_or_none(ps.get("filename")), task_uuid=_str_or_none(ps.get("uuid")),
            current_layer=_nonneg_int_or_none(ps.get("current_layer")),
            total_layer=_nonneg_int_or_none(ps.get("total_layer")),
            progress=progress,
            sub_status_reason_code=_int_or_none(ms.get("sub_status_reason_code")),
            state_message=_str_or_none(ps.get("message")),
            exception_codes=tuple(codes), exception_raw=tuple(exc),
            nozzle_temp=nozzle, nozzle_target=nozzle_t,
            bed_temp=bed_actual, bed_target=bed_target,
            box_temp=box_actual, box_target=box_target,
            filament_detected=_int_or_none(ext.get("filament_detected")) if isinstance(ext, dict) else None,
            filament_detect_enable=(_int_or_none(ext.get("filament_detect_enable"))
                                    if isinstance(ext, dict) else None),
            position=position, speed_mode=_int_or_none(gm.get("speed_mode")),
            led_status=_int_or_none(led.get("status")),
            raw=dict(result))

    @property
    def is_idle(self) -> bool:
        """Idle in the only sense a start needs: the printer itself says ready."""
        ok, _ = self.ready_for_job()
        return ok

    @property
    def error(self) -> str | None:
        """The error story in short words — an exception code, the emergency-stop machine code,
        or print state 'error' with its message. This firmware reports nothing else: an empty
        list and reason code 0 is the whole of it, so 'None' here means 'the printer reports no
        error', never 'there is surely none'. (A paused sub-state is not an error; it is a job.)"""
        codes = set(self.exception_codes)
        if self.machine_status == 14:
            return "the printer reports emergency stop (machine status 14)"
        if self.print_state is PrintState.ERROR:
            words = f": {self.state_message}" if self.state_message else " with no message reported"
            return f"the printer's print state is error{words}" + \
                   (f" (codes {', '.join(str(c) for c in sorted(codes))})" if codes else "")
        if not codes:
            return None
        named = ", ".join(str(c) for c in sorted(codes))
        return f"the printer reports error code(s) {named}"

    @property
    def active_job(self) -> str | None:
        """The file the machine is holding, if any. Any print state short of the idle pair (or a
        state string this adapter doesn't know) means a job may still own its file even when the
        filename field is zeroed — so an active-looking state with no name is an uncertainty the
        ready check reports, not a clear one."""
        return self.job_filename

    def ready_for_job(self) -> tuple[bool, str | None]:
        """May a job be started, as far as this reply can prove? Only machine status 1 (idle),
        sub-state 0, the idle print state, no error of any kind, and no file held by any state
        — the design's 'idle, no error, no other active file'. 0 is 'initializing', not idle;
        paused 2/2502 is a job, not a clear bed; and a state this adapter doesn't know reports
        itself as unknown, numbers and word included, rather than borrowing a neighbour's
        meaning."""
        word = _MACHINE_WORDS.get(self.machine_status)
        if word is None:
            return False, (f"machine status {self.machine_status} with sub-state "
                           f"{self.sub_status} is not a state this adapter knows, so nothing can "
                           "be claimed about the printer")
        if (why := self.error) is not None:
            return False, f"the printer reports an error: {why}"
        if self.machine_status != 1:
            head = (f"the printer is not idle (machine status {self.machine_status}, "
                    f"{word}, sub-state {self.sub_status}")
            if self.print_state not in (PrintState.IDLE, PrintState.STANDBY):
                head += f", print state {self.raw_print_state!r}"
            head += ")"
            if self.job_filename is not None:
                head += f": it is holding a job: {self.job_filename}"
            return False, head
        if self.print_state not in (PrintState.IDLE, PrintState.STANDBY):
            if self.print_state is PrintState.UNKNOWN:
                held = (f" while still holding {self.job_filename}" if self.job_filename else "")
                return False, (f"the printer reports print state {self.raw_print_state!r}, which "
                               f"this adapter does not know{held}, so nothing can be claimed "
                               "about the bed")
            if self.job_filename is None:
                return False, (f"the printer is not idle: print state {self.raw_print_state!r} "
                               "still holds a job but named no file, so the bed cannot be "
                               "assumed clear")
            return False, (f"the printer is not idle: it is holding a job "
                           f"({self.job_filename}, state {self.raw_print_state!r})")
        if self.sub_status != 0:
            return False, (f"the printer is not idle (machine status {self.machine_status}, "
                           f"sub-state {self.sub_status})")
        if self.job_filename is not None:
            return False, (f"the printer is idle but still holds a file: {self.job_filename}")
        return True, None


# --------------------------------------------------------------------------- CANVAS (2005)


@dataclass(frozen=True)
class Cc2Tray:
    """One CANVAS tray: what it is, how hot it may run, and how sure we are. ``raw_state`` keeps
    the firmware's own number; ``state`` is this adapter's four-word reading of it. The rated
    range is read along and nothing more: whether a range suits a job is the start's check, and
    a range that doesn't read cleanly simply isn't there (the raw entry keeps the original)."""
    tray_id: int
    state: TrayState
    raw_state: int
    material: str | None = None
    brand: str | None = None
    name: str | None = None
    color: str | None = None            # normalised to #RRGGBB; the printer sends the hex bare
    min_nozzle_temp: int | None = None
    max_nozzle_temp: int | None = None
    active: bool = False                # the canvas itself named this tray; not a substitute for state
    canvas_id: int = 0
    raw: dict = field(default_factory=dict, compare=False)

    @property
    def may_run_job(self) -> bool:
        """Could this tray feed a print? Present (raw 1) or active (raw 2) — the two loaded
        states the design admits, and the Home Assistant client itself marks status 1 enabled.
        Whether the material and rated range suit the actual job is decided at start, which
        re-reads the canvas; empty (0) and unknown feed nothing, ever."""
        return self.state in (TrayState.PRESENT, TrayState.ACTIVE)


def _placeholder(value: str | None) -> str | None:
    """The firmware marks unset strings with placeholders ('—', '?'); they mean absent."""
    if not value or value.startswith("—") or value == "?":
        return None
    return value


def _parse_tray(entry: Any, canvas_id: int) -> Cc2Tray:
    if not isinstance(entry, dict):
        raise Cc2DataError("protocol_error",
                           f"A CANVAS tray in the printer's {METHOD_CANVAS} reply is malformed "
                           f"({type(entry).__name__}) — silently skipping it would understate "
                           "what the printer holds.", reason="canvas_malformed")
    tray_id = _int(entry.get("tray_id"), entry, "tray_id", METHOD_CANVAS, "tray id", "canvas")
    if tray_id < 0:
        raise Cc2DataError("protocol_error",
                           f"A CANVAS tray in the printer's {METHOD_CANVAS} reply reported a "
                           f"negative tray id ({tray_id}), which is not an addressable tray.",
                           reason="canvas_malformed")
    raw_state = _int(entry.get("status"), entry, "status", METHOD_CANVAS,
                     f"tray {tray_id} status", "canvas")
    if raw_state in (0, 1, 2):
        state = {0: TrayState.EMPTY, 1: TrayState.PRESENT, 2: TrayState.ACTIVE}[raw_state]
    else:
        state = TrayState.UNKNOWN  # kept with its number; it decides nothing and blocks what it's asked
    mn = _nonneg_int_or_none(entry.get("min_nozzle_temp"))
    mx = _nonneg_int_or_none(entry.get("max_nozzle_temp"))
    if (mn, mx) == (0, 0):  # 0/0 on this firmware is the placeholder for "no rating" — absent,
        mn = mx = None      # useless however it is read: nothing can ever fall inside 0..0
    elif mn is None or mx is None:
        mn = mx = None      # half a range is no range; the start demands a whole one or refuses
    elif mn > mx:
        mn = mx = None      # a range that contradicts itself rates nothing; raw keeps the lie
    color = _str_or_none(entry.get("filament_color"))
    if color is not None:
        if not color.startswith("#"):
            color = f"#{color}"
        if not _HEX_RE.fullmatch(color):
            raise Cc2DataError("protocol_error",
                               f"CANVAS tray {tray_id} in the printer's {METHOD_CANVAS} reply "
                               f"reported colour {color!r}, which is not a colour — refusing to "
                               "half-read the tray it describes.",
                               reason="canvas_malformed")
    return Cc2Tray(
        tray_id=tray_id, state=state, raw_state=raw_state,
        material=_placeholder(_str_or_none(entry.get("filament_type"))),
        brand=_placeholder(_str_or_none(entry.get("brand"))),
        name=_placeholder(_str_or_none(entry.get("filament_name"))),
        color=color, min_nozzle_temp=mn, max_nozzle_temp=mx, canvas_id=canvas_id,
        raw=dict(entry))


@dataclass(frozen=True)
class Cc2CanvasStatus:
    """The CANVAS multi-material system as 2005 answers it. Every tray parsed or the reply is
    refused: a listing that silently dropped an unreadable tray would tell the printer there is
    less filament than it claims."""
    connected: bool
    active_canvas_id: int
    active_tray_id: int                  # -1: the printer's own word for "none active"
    auto_refill: bool
    trays: tuple[Cc2Tray, ...] = ()
    raw: dict = field(default_factory=dict, compare=False)

    @classmethod
    def from_result(cls, result: Any) -> "Cc2CanvasStatus":
        if not isinstance(result, dict):
            raise Cc2DataError("protocol_error",
                               f"The printer's {METHOD_CANVAS} reply was not an object at all.",
                               reason="canvas_malformed")
        _check_envelope(result, METHOD_CANVAS)
        info = result.get("canvas_info")
        if info is None:                 # the documented shape puts everything under canvas_info;
            info = result                # accept the bare inner object too, never invent one
        if not isinstance(info, dict):
            raise Cc2DataError("protocol_error",
                               f"The printer's {METHOD_CANVAS} canvas info is malformed, so no "
                               "tray can be trusted.", reason="canvas_malformed")
        units = _required(info.get("canvas_list"), info, "canvas_list", METHOD_CANVAS,
                          "canvas unit list", "canvas")
        if not isinstance(units, list) or not units:
            raise Cc2DataError("protocol_error",
                               f"The printer's {METHOD_CANVAS} reply reports no canvas units — "
                               "with nothing to check against, no tray can be called available.",
                               reason="canvas_incomplete")
        if len(units) != 1:
            raise Cc2DataError("protocol_error", "Multiple CANVAS units are not supported safely.",
                               reason="canvas_unsupported")
        active_canvas = _int(info.get("active_canvas_id", 0), info, "active_canvas_id",
                             METHOD_CANVAS, "active canvas id", "canvas")
        active_tray = _int(info.get("active_tray_id", -1), info, "active_tray_id",
                           METHOD_CANVAS, "active tray id", "canvas")
        refill = info.get("auto_refill", False)
        if isinstance(refill, bool):
            auto_refill = refill
        elif refill in (0, 1):
            auto_refill = bool(refill)
        else:
            raise Cc2DataError("protocol_error",
                               f"The printer's {METHOD_CANVAS} auto-refill flag arrived as "
                               f"{refill!r}, which is neither yes nor no.",
                               reason="canvas_malformed")
        parsed_trays: list[Cc2Tray] = []
        connected_any = False
        for unit in units:
            if not isinstance(unit, dict):
                raise Cc2DataError("protocol_error",
                                   f"A canvas unit in the printer's {METHOD_CANVAS} reply is "
                                   f"malformed ({type(unit).__name__}).", reason="canvas_malformed")
            cid = _int(unit.get("canvas_id", 0), unit, "canvas_id", METHOD_CANVAS, "canvas id", "canvas")
            raw_conn = _required(unit.get("connected"), unit, "connected", METHOD_CANVAS,
                                 "canvas connection state", "canvas")
            if raw_conn not in (0, 1) or isinstance(raw_conn, bool):
                raise Cc2DataError("protocol_error",
                                   f"A canvas unit in the printer's {METHOD_CANVAS} reply "
                                   f"reported connection state {raw_conn!r}, which is neither "
                                   "connected nor disconnected.", reason="canvas_malformed")
            connected_any = connected_any or raw_conn == 1
            tray_list = _required(unit.get("tray_list"), unit, "tray_list", METHOD_CANVAS,
                                  "tray list", "canvas")
            if not isinstance(tray_list, list) or not tray_list:
                raise Cc2DataError("protocol_error",
                                   f"Canvas unit {cid} reports no tray list at all, so the "
                                   "printer's filament cannot be accounted for.",
                                   reason="canvas_incomplete")
            for entry in tray_list:
                parsed_trays.append(_parse_tray(entry, cid))
        # parse order kept: A1..A4 as the printer itself lists them; ``active`` marks the tray
        # the canvas named, which never overrides what the tray's own state says
        trays = tuple(
            replace(t, active=True)
            if active_tray >= 0 and t.tray_id == active_tray and t.canvas_id == active_canvas
            else t
            for t in parsed_trays)
        return cls(connected=connected_any, active_canvas_id=active_canvas,
                   active_tray_id=active_tray, auto_refill=auto_refill, trays=trays,
                   raw=dict(result))

    @property
    def tray_count(self) -> int:
        return len(self.trays)

    def get_tray(self, tray_id: int) -> Cc2Tray | None:
        for t in self.trays:
            if t.tray_id == tray_id:
                return t
        return None

    @property
    def active_tray(self) -> Cc2Tray | None:
        """The tray the canvas itself calls active (``active_tray_id``; -1 is none). Reported
        state stays the authority for a start — this field never overrides it."""
        if self.active_tray_id < 0:
            return None
        return self.get_tray(self.active_tray_id)


def supply_decision(tray: Cc2Tray | None, *, canvas: Cc2CanvasStatus,
                    canvas_connected: bool = True) -> str | None:
    """Why this tray may not feed a print, or None when it may. The states a start may reach
    for are the loaded ones — present (raw 1) and active (raw 2), the design's pair; empty (0)
    is nothing and an unknown number is never promoted to either. Material, colour and rated
    range are NOT checked here: the start re-reads the canvas and checks every commanded
    temperature against the spool range there, so this decision is only 'is there filament
    here at all, and is the canvas it sits in really speaking'. A canvas the caller reports
    disconnected, or one the printer reports disconnected, or a tray that is not on the canvas
    considered, blocks whatever the tray itself says — provenance over vibes. A None tray
    (looked up and not found) refuses as much as a tray with nothing in it."""
    if not canvas_connected:
        return "the canvas is reported disconnected, so no tray's state can be relied on"
    if tray is None:
        return "no such tray was given — a safety decision needs a tray the printer actually has"
    if not canvas.connected:
        return ("the printer's canvas is not connected, so no tray on it can be called "
                "available for a job")
    if (found := canvas.get_tray(tray.tray_id)) is None or found.canvas_id != tray.canvas_id:
        return (f"tray {tray.tray_id} is not on the canvas considered "
                "(a tray id means nothing across canvas units)")
    if tray.state is TrayState.UNKNOWN:
        return (f"tray {tray.tray_id} reports state {tray.raw_state}, which is not a state this "
                "adapter knows, so nothing can be claimed about its filament")
    if tray.state is TrayState.EMPTY:
        return f"tray {tray.tray_id} reports empty: there is no filament there to print with"
    return None


# --------------------------------------------------------------------------- files (1044)


@dataclass(frozen=True)
class Cc2File:
    """One gcode on the printer. ``filename`` is a proven-safe single name — never empty, never
    path-ish, never control — and ``size`` is the printer's own byte count; everything else is
    reporting noise that reads as None when it can't be read."""
    filename: str
    size: int
    layers: int | None = None
    print_seconds: int | None = None
    filament_grams: float | None = None
    created: datetime | None = None
    colors: tuple[str, ...] = ()
    tools: tuple[int, ...] = ()
    raw: dict = field(default_factory=dict, compare=False)


def _parse_entry(entry: Any) -> Cc2File:
    if not isinstance(entry, dict):
        raise Cc2DataError("protocol_error",
                           f"A file entry in the printer's listing is malformed "
                           f"({type(entry).__name__}); a listing with a hole in it is "
                           "not a listing.", reason="inventory_malformed")
    name = entry.get("filename")
    bad = None
    if not isinstance(name, str) or not name or name != name.strip():
        bad = "is not a plain name"
    elif "/" in name or "\\" in name or len(name) > _FILENAME_MAX:
        bad = "is not a single plain name"          # traversal and directory entries die here
    elif any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        bad = "carries control characters"
    if bad:
        raise Cc2IncompleteDataError("protocol_error",
                                     f"A file entry in the printer's listing {bad} "
                                     f"({name!r:.60}), so the listing cannot be trusted whole.",
                                     reason="inventory_malformed")
    kind = entry.get("type")
    if kind is not None and kind != "file":
        raise Cc2DataError("protocol_error",
                           f"{name!r} is reported as type {kind!r}, not a file, in the "
                           "middle of a file listing.", reason="inventory_malformed")
    size = entry.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise Cc2IncompleteDataError("protocol_error",
                                     f"{name!r} carries no usable size, and upload verification "
                                     "means comparing the printer's own byte count.",
                                     reason="inventory_malformed")
    created = None
    ts = entry.get("create_time")
    if isinstance(ts, int) and not isinstance(ts, bool):
        with contextlib.suppress(ValueError, OSError, OverflowError):  # far out of range: noise
            created = datetime.fromtimestamp(ts, tz=UTC)
    grams = entry.get("total_filament_used")
    if isinstance(grams, bool) or not isinstance(grams, (int, float)):
        grams = None
    colors: list[str] = []
    tools: list[int] = []
    cm = entry.get("color_map")
    if cm is not None and not isinstance(cm, list):
        raise Cc2IncompleteDataError("protocol_error",
                                     f"{name!r} carries a colour map that isn't a list.",
                                     reason="inventory_malformed")
    for item in cm or []:
        if not isinstance(item, dict):
            raise Cc2IncompleteDataError("protocol_error",
                                         f"{name!r} carries a colour map entry that isn't an object.",
                                         reason="inventory_malformed")
        t = item.get("t")
        if not isinstance(t, bool) and isinstance(t, int) and t >= 0:
            tools.append(t)  # a tool index we don't understand is noise, not a contradiction
        color = item.get("color")
        if isinstance(color, str):
            if not color.startswith("#"):
                color = f"#{color}"
            if not _HEX_RE.fullmatch(color):
                raise Cc2DataError("protocol_error",
                                   f"{name!r} reported colour {color!r}, which is not a colour — "
                                   "refusing to half-read the file it describes.",
                                   reason="inventory_malformed")
            colors.append(color)
    return Cc2File(filename=name, size=size,
                   layers=_nonneg_int_or_none(entry.get("layer")),
                   print_seconds=_nonneg_int_or_none(entry.get("print_time")),
                   filament_grams=None if grams is None else float(grams),
                   created=created, colors=tuple(colors), tools=tuple(tools),
                   raw=dict(entry))


@dataclass(frozen=True)
class Cc2FileInventory:
    """A file listing the printer proved complete: every entry of every page that answers, the
    count matching the total the printer reported, and every entry parsed. Anything less
    raised instead.
    ``total`` is always the printer's own number — a listing it never counted is never certified,
    so there is no certified count to substitute for a missing total. ``pages`` counts the listing
    replies the certified inventory was actually ASSEMBLED FROM: one when the un-paged reply
    certified itself, otherwise the paged asks, because the un-paged probe's entries are dropped
    rather than merged into a paged listing (see ``list_files``). It is not a count of the requests
    the read sent."""
    storage: str
    pages: int
    total: int
    files: tuple[Cc2File, ...] = ()

    @property
    def count(self) -> int:
        return len(self.files)

    def find_file(self, exact_name: str) -> Cc2File | None:
        """Look a name up exactly as the printer wrote it — case and all, because upload
        verification compares to the byte, not to a vibe."""
        for f in self.files:
            if f.filename == exact_name:
                return f
        return None


def _read_files_page(result: Any) -> tuple[list, int | None]:
    """One 1044 reply, gate by gate: object, envelope, file_list, total. A reply is parsed whether
    or not it carries a total — the protocol doc's example answer carries none — but absence is
    tolerated at the PARSE level only: ``total`` is what a complete listing is certified against,
    so a reply without one is read and never certified, while a total that IS present must read as
    a plain non-negative int or the reply is malformed."""
    if not isinstance(result, dict):
        raise Cc2DataError("protocol_error",
                           f"The printer's {METHOD_FILES} reply was not an object at all.",
                           reason="inventory_malformed")
    _check_envelope(result, METHOD_FILES)
    entries = _required(result.get("file_list"), result, "file_list", METHOD_FILES, "file list",
                        "inventory")
    if not isinstance(entries, list):
        raise Cc2DataError("protocol_error",
                           f"The printer's {METHOD_FILES} file list is malformed "
                           f"({type(entries).__name__}).", reason="inventory_malformed")
    total: int | None = None
    if "total" in result:
        t = result["total"]
        if isinstance(t, bool) or not isinstance(t, int) or t < 0:
            raise Cc2DataError("protocol_error",
                               f"The printer's {METHOD_FILES} reply reports its file total as "
                               f"{t!r}, which is not a count, so no listing can be certified "
                               "against it.", reason="inventory_malformed")
        total = t
    return entries, total


def _read_rows(entries: list, seen: dict[str, Cc2File], *, where: str) -> list[Cc2File]:
    """Every entry of one 1044 reply, parsed and added to ``seen`` — refusing the whole read the
    moment a name arrives twice. Twice in one reply is a listing that does not add up; twice
    across replies is pages that overlap, which is what a printer that ignores ``offset`` answers
    with. Nothing is silently de-duplicated: Elegoo's own LAN client merges pages by filename, and
    a merge is exactly how an overlap becomes invisible and a short listing starts passing for a
    complete one."""
    rows: list[Cc2File] = []
    for entry in entries:
        f = _parse_entry(entry)
        if f.filename in seen:
            raise Cc2IncompleteDataError(
                "protocol_error",
                f"{f.filename!r} arrived twice in the printer's file listing ({where}), so its "
                "pages overlap and no complete listing can be proven from them — nothing is "
                "merged here and nothing is re-asked.", reason="pagination_overlap")
        seen[f.filename] = f
        rows.append(f)
    return rows


async def list_files(transport: Any, storage: str = "local", *,
                     timeout_s: float | None = None,
                     max_pages: int = MAX_FILE_PAGES) -> Cc2FileInventory:
    """Every gcode the printer holds, or a refusal — never a first page passed off as a listing.

    The first ask is the request exactly as the protocol doc and the upstream integration send it,
    the only 1044 request shape ever observed to answer this printer: ``{"storage_media": "local",
    "path": "/"}`` — no paging parameters, and on firmware 02.01.00.00 it answered all 54 files in
    one reply. A reply that carries its own total and delivers exactly that many distinct names is
    certified as it stands, for exactly one request.

    A reply that cannot certify itself — short of its total, or reporting no total at all — is
    completed by paging, and paging asks with the parameters Elegoo's own LAN web client sends:
    ``{"storage_media": "local", "dir": "/", "offset": n, "limit": FILE_REQUEST_LIMIT}``. The
    protocol doc's single example shows ``page``/``page_size``, but that example is the whole of
    the evidence for those names, first-party software for this device has never been seen to send
    them, and no paged request of either spelling has ever been observed answered on this printer.
    So the walk trusts its replies and nothing else:

    * ``total`` is the printer's own count of the WHOLE listing — the reading Elegoo's client
      applies (it reads ``total`` and accumulates entries against it) and the only reading that
      can certify anything. A listing is certified when the distinct names collected land exactly
      on it, and never without it: a firmware that answers with no total anywhere gets an honest
      incomplete refusal, because there is nothing there to certify against.
    * paged asks start at ``offset`` 0 and the probe's entries are dropped rather than merged in.
      The probe carried no paging parameters, so its semantics and the paged family's are not
      known to be the same thing, and interleaving two request families is how an invented
      semantics gets into a listing. The probe is still used: every file it named must turn up in
      the paged result, or the two shapes disagree about what the printer holds and nothing here
      is reported at all.
    * a name that arrives twice — in one reply or again on a later page — refuses the read. That
      is the gate a printer which ignores ``offset`` walks into: its second page is its first one
      over again.
    * a page shorter than ``FILE_REQUEST_LIMIT`` proves the listing ended only when the printer's
      total says it did. A short page short of the total, an empty page, a total that moves
      mid-walk, more names than the total, and a walk that runs out of ``max_pages`` each refuse
      with their own numbers.
    * one ask per offset, ever. This function never retries and never repeats an ask, so a
      stalling or repeating walk ends in a refusal that names what it saw — not in a second try at
      a request whose answer already contradicted the first, and not in a loop.

    Nothing here claims a live-tested second page: completeness is a property of the replies, and
    absent the proof the answer is 'incomplete', never '100 of 112 and off you go'."""
    if storage != "local":
        raise ValueError(
            f"storage {storage!r} has no verified 1044 request shape: only 'local' at the root "
            "has ever been asked and answered. The removable volumes appear in the sources under "
            "two different parameter spellings and were never observed answered at all, so asking "
            "anyway would be inventing a request.")
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")

    # ---- 1. the un-paged probe: the one 1044 request on record that answers this printer in full
    probe = await transport.request(METHOD_FILES, {"storage_media": "local", "path": "/"},
                                    timeout_s=timeout_s)
    entries, total = _read_files_page(probe)
    # the throwaway ``seen`` map is deliberate: a name twice inside the un-paged reply is the
    # printer contradicting itself, and that refuses the read before any paging is considered
    probe_rows = _read_rows(entries, {}, where="in one un-paged reply")
    probe_names = {f.filename for f in probe_rows}
    if total is None:
        why_first = (f"the un-paged reply named {len(probe_rows)} files and reported no total to "
                     "certify against")
    elif len(probe_rows) == total:
        return Cc2FileInventory(storage="local", pages=1, total=total, files=tuple(probe_rows))
    else:
        if len(probe_rows) > total:
            raise Cc2IncompleteDataError(
                "protocol_error",
                f"The printer's {METHOD_FILES} reply sent {len(probe_rows)} files while reporting "
                f"only {total} — the listing does not add up.", reason="pagination_mismatch")
        why_first = (f"the un-paged reply delivered {len(probe_rows)} of the {total} files it "
                     "counts")

    # ---- 2. paging with the official client's own parameters: the replies certify it, or nothing
    by_name: dict[str, Cc2File] = {}
    ordered: list[Cc2File] = []
    pages = 0
    certified: int | None = None
    blocker = ""
    while True:
        if pages >= max_pages:
            blocker = (f"the walk stopped at its own bound of {max_pages} listing pages with "
                       f"{len(ordered)}" + (f" of {total}" if total is not None else "") +
                       " files, so later pages were never proven")
            break
        offset = len(ordered)
        result = await transport.request(
            METHOD_FILES,
            {"storage_media": "local", "dir": "/", "offset": offset, "limit": FILE_REQUEST_LIMIT},
            timeout_s=timeout_s)
        pages += 1
        entries, page_total = _read_files_page(result)
        if page_total is not None:
            if total is None:
                total = page_total
            elif page_total != total:
                raise Cc2IncompleteDataError(
                    "protocol_error",
                    f"The printer's file total moved from {total} to {page_total} mid-walk — the "
                    "listing changed under the read and cannot be certified.",
                    reason="pagination_mismatch")
        rows = _read_rows(entries, by_name, where=f"at offset {offset}, page {pages}")
        ordered.extend(rows)
        if total is not None and len(ordered) > total:
            raise Cc2IncompleteDataError(
                "protocol_error",
                f"The listing walk collected {len(ordered)} files against a total of {total} — "
                "the listing does not add up.", reason="pagination_mismatch")
        if total is not None and len(ordered) == total:
            certified = total  # the printer's own count and its own distinct names, agreeing
            break
        if not rows:
            blocker = (f"a page at offset {offset} came back empty " +
                       (f"while the printer counts {total} files and only {len(ordered)} had "
                        "arrived" if total is not None else
                        "and no reply has reported a total at all") + " — the walk stalled")
            break
        if len(rows) < FILE_REQUEST_LIMIT:
            if total is None:
                repeating = probe_names == set(by_name)
                blocker = (f"a page at offset {offset} answered {len(rows)} of the "
                           f"{FILE_REQUEST_LIMIT} entries asked for while no 1044 reply, paged or "
                           "un-paged, has reported a total — a short page only proves a listing "
                           "ended when the printer's own count says it did" +
                           (", and the paged reply repeated the un-paged one entry for entry, "
                            "which is what a printer that ignores offset/limit answers"
                            if repeating else ""))
                break
            blocker = (f"the walk ended at {len(ordered)} files with a short page at offset "
                       f"{offset} while the printer still reports {total} total — later pages "
                       "were never delivered")
            break
        # a full page, still short of the total: ask the next offset — a new ask, never a repeat
    if certified is None:
        raise Cc2IncompleteDataError(
            "protocol_error",
            f"File listing incomplete: {why_first}; {blocker}, so no listing is claimed. "
            "A partial one is not a listing.", reason="pagination_incomplete")
    lost = probe_names - set(by_name)
    if lost:
        raise Cc2IncompleteDataError(
            "protocol_error",
            f"The un-paged 1044 reply named {len(probe_rows)} files while the paged walk delivered "
            f"{certified} and cannot account for {len(lost)} of them (e.g. {sorted(lost)[0]!r}): "
            "the two request shapes disagree about what the printer holds, so neither is a "
            "listing.", reason="pagination_mismatch")
    return Cc2FileInventory(storage="local", pages=pages, total=certified, files=tuple(ordered))


# --------------------------------------------------------------------------- reads and combinations


async def fetch_machine_status(transport: Any, *, timeout_s: float | None = None) -> Cc2MachineStatus:
    """Ask the printer (1002) exactly once and read the answer fail-closed. A lost reply stays
    lost — the transport's timeout arrives untouched and nothing is resent."""
    result = await transport.request(METHOD_STATUS, {}, timeout_s=timeout_s)
    return Cc2MachineStatus.from_result(result)


async def fetch_canvas_status(transport: Any, *, timeout_s: float | None = None) -> Cc2CanvasStatus:
    """Ask the printer (2005) exactly once; every tray parses or this raises. See Cc2CanvasStatus."""
    result = await transport.request(METHOD_CANVAS, {}, timeout_s=timeout_s)
    return Cc2CanvasStatus.from_result(result)


@dataclass(frozen=True)
class Cc2PrinterStatus:
    """Machine and canvas read together — what ``cc2_status`` will show, in one shape."""
    machine: Cc2MachineStatus
    canvas: Cc2CanvasStatus

    def ready_for_job(self) -> tuple[bool, str | None]:
        ok, why = self.machine.ready_for_job()
        if not ok:
            return False, why
        if not self.canvas.connected:
            return False, ("the canvas is not connected, so no tray can be verified for a job "
                           "from this read")
        return True, None


async def fetch_status(transport: Any, *, timeout_s: float | None = None) -> Cc2PrinterStatus:
    """Machine (1002) and canvas (2005) in one call, both required, both failing closed. They
    are issued together and matched by the transport's id correlation — which one answers first
    is irrelevant — and each is asked exactly once. The file inventory deliberately does not
    live here: a status read stays cheap, and a start's re-check asks for it on its own."""
    machine_result, canvas_result = await asyncio.gather(
        transport.request(METHOD_STATUS, {}, timeout_s=timeout_s),
        transport.request(METHOD_CANVAS, {}, timeout_s=timeout_s))
    return Cc2PrinterStatus(machine=Cc2MachineStatus.from_result(machine_result),
                            canvas=Cc2CanvasStatus.from_result(canvas_result))
