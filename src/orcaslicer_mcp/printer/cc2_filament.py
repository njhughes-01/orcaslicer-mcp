"""INTERNAL CANDIDATE for the four CC2 filament-path commands — not a tool, not exposed, not claimed
to work. Nothing imports this module: ``server.py`` declares no filament tool, ``CC2_TOOL_NAMES``,
``_TOOL_ANNOTATIONS`` and ``_OPEN_WORLD_TOOLS`` do not name one, and no manifest or README mentions
it; ``tests/test_cc2_filament.py`` fails if that posture changes. It exists so a controlled live
test, with Nathan and an idle printer present, has one already-checked frame to send rather than
something invented at the console.

WHAT THE FOUR METHODS ARE, and every fact's source. The protocol doc this adapter was first
researched against names none of the four anywhere (its method reference runs 1020-1031, 1038, 1042
-1048, 1057-1058 and 2004, and gives 2005 only as a read), so all four shapes come from one
first-party place: the
LAN control the printer serves itself (``webapp/lan/index.html``, 3.6 MB, read here directly, not
summarised). Its enum names them ``Feed=1024``, ``Retreat=1025``, ``LoadFilamenting=2001``,
``UnloadFilamenting=2002``, and its four call sites are unambiguous:

* **1024 / 1025 — the toolhead.** ``onFeedClick`` builds ``{method: Feed}`` and ``onUnfeedClick``
  ``{method: Retreat}``: **no params key at all**, and both are awaited with ``1e4*6*5`` — 300 s.
  Its store names them ``loadFilament``/``unloadFilament`` and its failure toasts are
  ``feedFailed``/``UnloadFilamentFailed``. What they physically do is not written anywhere in any
  source: no length, no speed, no temperature and no filament type is sent, so the operation is the
  firmware's own, and the closest reading available is the sub-status family both the client and the
  Home Assistant integration define — 1061 extruder loading, 1062 extruder unloading, 1063 and 1064
  their completions. This module reports those numbers when it sees them and interprets nothing.
* **2001 / 2002 — a CANVAS tray.** ``loadFilamentCanvas(canvas_id, tray_id)`` sends
  ``params: {canvas_id: <n>, tray_id: <n>}`` and the unload the same pair, both awaited with the
  same 300 s. Their identifiers are the printer's own: ``canvas_info.canvas_list[].canvas_id`` and
  its ``tray_list[].tray_id`` (the doc's 2005 sample and the client's slot-map builder
  ``canvasId: unit.canvas_id || 0`` both read them from there), so this module reads them from one
  2005 and refuses any pair the printer never named. The client itself passes a literal ``0`` for
  the canvas because its panel is built for one unit; copying that literal would be hardcoding an
  identifier the printer offers.
* **The one deviation from the client, recorded.** For 1024/1025 the client sends no ``params`` key
  while this transport's envelope always carries one, empty (``cc2_transport.request`` publishes
  ``{"id", "method", "params"}`` with ``params or {}``). That envelope is what this firmware has
  actually answered for every other parameterless method on this server (1001, 1002, 1021-1023, 2005,
  all live), so an empty object is the shape with evidence behind it here; whether this printer cares
  about a missing key versus an empty one is one of the things the live test watches.
* **The gates are the vendor control's own, and only those.** The toolhead pair's two buttons are
  disabled by ``!(isIdle || isPrintStopped)`` — machine status 1, or status 2 with sub-status 2504
  (stopped) — with no temperature condition anywhere in that component. The CANVAS panel's feed and
  unload buttons add ``!(isIdle || isMainHoming || isPaused || isFilamentInterruption)`` — status 1,
  10, or status 2 with sub-status 2502 (paused) or 2505 (filament interruption) — and need a
  connected unit and a tray that is neither ``status 0`` (empty) nor unrecognised. Since none of
  those six states can be a drive already in progress (the load/unload step codes 1150-1158 and
  1160-1166 ride status 2, and 3/4 are the loading/unloading statuses themselves), no second
  "already driving" rule is added: the step codes are reported, never gated on. Nothing else is
  imposed — no temperature ceiling, no purge requirement, no bed-clear assertion — because no source
  states one for these four, and the plan bars CC2-specific restrictions that are not required. Three
  tray rules are applied to the **load** direction only, and all three for the same reason: an empty
  spool, an unrecognisable one, or the spool the printer already reports in its path (its own
  ``loaded`` state, raw 2, which is what the control calls ``LoadedSlotId`` and offers an unload and
  not a second load) is not something to thread into a toolhead, while pulling such a spool back out
  is exactly what the printer's filament-interruption state exists for — after a run-out the tray
  worth removing is the one whose own status has just changed. So a load of those three refuses and
  an unload of any tray the printer names goes ahead and lets the printer answer for itself.
* **What the preview therefore says instead of a rule.** The client's own step names for a CANVAS
  load are heating, an insert step, cutting, drawing, pushing and flushing, so a load ends by
  putting molten filament out of a nozzle whose position nobody reports here, and the insert step
  may need a hand at the machine. That is said out loud beside the printer's actual nozzle
  temperature and the fact that no source says this command heats anything by itself, and the person
  decides — exactly as the vendor UI, which offers the buttons cold.

THE HONESTY CONTRACT is the job controls', imported rather than reworded (``cc2_control``): accepted
(an explicit integer ``error_code`` 0), refused (a nonzero code, kept) or ambiguous (no answer, an
unreadable one, a dead session); one command, never a second one; and for these four the 300 s window
is the client's own precisely because the answer can arrive only when the mechanism has finished, so
an expiry is reported as "the machine may still be mid-operation", never as a failure and never as a
refusal. The read-back is the vendor's own follow-up and nothing wider: one read-only 1002 for the
toolhead pair, one read-only 2005 for a tray (its ``loadFilamentCanvas(...) && refresh()`` re-reads
exactly that), never after a refusal on record and never on a dead session. Because the client never
waits for a tray to *change*, an unchanged tray after an accepted command is reported as an unchanged
reading and never as a failure. ``verified_on_this_printer`` is false in every reply: no CC2 has ever
answered 1024, 1025, 2001 or 2002 on this project. Privacy: no address, token or environment
variable crosses here — the transport is the pinned session — and no note carries a credential or a
raw reply. Every authored string fits the direct tools' reply cap (``cc2_control.NOTE_CAP``)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NoReturn

from . import cc2_control as job
from . import cc2_state as st
from .cc2_control import ControlStatus  # the same three-way answer, one vocabulary
from .cc2_transport import Cc2TimeoutError, PrinterError

METHOD_FEED = 1024             # Feed — the toolhead takes filament in. No params in any source.
METHOD_RETREAT = 1025          # Retreat — the toolhead gives it back. No params in any source.
METHOD_CANVAS_LOAD = 2001      # LoadFilamenting — {"canvas_id": n, "tray_id": n}
METHOD_CANVAS_UNLOAD = 2002    # UnloadFilamenting — the same pair

# verb -> the one method it sends. The whole vocabulary this module speaks.
_METHODS: dict[str, int] = {"toolhead_load": METHOD_FEED, "toolhead_retreat": METHOD_RETREAT,
                            "canvas_load": METHOD_CANVAS_LOAD, "canvas_unload": METHOD_CANVAS_UNLOAD}
CANVAS_VERBS = ("canvas_load", "canvas_unload")

# The client's own window for all four (``1e4*6*5`` at every call site), not chosen here. A short
# window is what turns a command the printer took into a report of an answer that never arrived.
FILAMENT_WAIT_S = 300.0

# The sub-status numbers the client's and the integration's enums name for these operations. They
# are reported, commanded by nothing, and gate nothing: see the docstring for why no rule hangs on
# them. Extruder (1024/1025) and CANVAS-drive (2001/2002) families kept apart because they mean
# different mechanisms.
EXTRUDER_SUBSTATUSES = {1061: "extruder loading", 1062: "extruder unloading",
                        1063: "extruder load complete", 1064: "extruder unload complete"}
CANVAS_DRIVE_SUBSTATUSES = {
    1150: "canvas load started", 1151: "canvas load heating nozzle",
    1152: "canvas load waiting for manual insert", 1153: "canvas load cutting filament",
    1154: "canvas load drawing filament", 1155: "canvas load pushing filament",
    1156: "canvas load flushing nozzle", 1157: "canvas load complete", 1158: "canvas load failed",
    1160: "canvas unload started", 1161: "canvas unload heating nozzle",
    1162: "canvas unload checking filament", 1163: "canvas unload cutting filament",
    1164: "canvas unload drawing filament", 1165: "canvas unload complete",
    1166: "canvas unload failed"}

# Machine states the vendor control offers each family in (its own disabled-expression, inverted).
_TOOLHEAD_STATES = {(1, None), (2, 2504)}                  # idle, or printing-and-stopped
_CANVAS_STATES = {(1, None), (10, None), (2, 2502), (2, 2505)}   # + homing, paused, interruption

UNVERIFIED = ("No CC2 has ever answered 1024, 1025, 2001 or 2002 on this project. The frames, the "
              "300 s windows and the gates are read off the LAN control the printer serves itself; "
              "what the firmware physically does with them has never been observed here.")

# Said beside every preview and every accepted call: what these commands are known NOT to carry.
NO_TEMPERATURE_RULE = ("No source states a temperature condition for this command and the vendor "
                       "control offers it cold, so none is imposed here. Whether the machine heats "
                       "by itself is unobserved; the nozzle reading below is the printer's own.")

FLUSH_NOTE = ("The client's own CANVAS load step names include heating, an insert step, cutting, "
              "drawing, pushing and flushing, so a load ends by putting molten filament out of the "
              "nozzle and may need a hand at the machine. Where that nozzle is, nothing here says.")

_ACCEPT_TAILS = {
    "toolhead_load": ("What left the answer is unobserved: the frame carries no length, speed or "
                      "temperature, so the motion is the firmware's own. The observed sub-status is "
                      "one separate read-only look and never part of acceptance."),
    "toolhead_retreat": ("As above for the retreat: no parameter of this frame says how far or how "
                         "fast, and the observed sub-status is a separate look, never the evidence."),
    "canvas_load": (FLUSH_NOTE),
    "canvas_unload": ("The tray's own reading afterwards is one read-only CANVAS look, the same one "
                      "Elegoo's control refreshes on success. An unchanged reading is an unchanged "
                      "reading, not a failure, and never earns a second command."),
}

# Said when the answer's window expired. This is the resume lesson again: for a mechanism-driven
# command a long window expiring is an expected outcome, and the machine may still be working.
LONG_OP_NOTE = ("For these commands the vendor control itself waits 300 s, because the answer can "
                "arrive only once the mechanism has finished: an expired window means the printer "
                "may still be mid-operation right now, and nothing was resent.")

# Said after an accepted tray command, so the observation is never mistaken for proof of the effect.
TRAY_OBSERVE_TAIL = ("The observed tray reading is one separate read-only 2005 and is never part of "
                     "acceptance. A tray that has not moved may simply still be mid-operation.")

OBSERVE_MISSING = ("The one read-only look this call allows itself produced no reading: it may have "
                   "been refused, answered in a shape this adapter cannot read, or the session died "
                   "under it. Nothing was resent, and the state stays unknown.")

_PREVIEW_HOW = {
    "toolhead_load": ("Nothing was sent to the printer. Show this preview for the user's go-ahead "
                      "unless they already authorized this action, then call again with confirm=true "
                      "— and have somebody at the machine, since filament comes out of the nozzle."),
    "toolhead_retreat": ("Nothing was sent. Show this preview for the user's go-ahead, then call "
                         "again with confirm=true. One command per confirmation."),
    "canvas_load": ("Nothing was sent. Show this preview — the exact canvas and tray the printer "
                    "named — for the user's go-ahead, then call again with confirm=true. The "
                    "sequence heats, cuts, pushes and flushes and may need a hand at the machine."),
    "canvas_unload": ("Nothing was sent. Show this preview — the exact canvas and tray — for the "
                      "user's go-ahead, then call again with confirm=true."),
}


@dataclass(frozen=True)
class Cc2FilamentResult:
    """The whole outcome of one filament command. ``verb`` is one of the four and ``method`` the one
    frame it sent; ``sent`` is the exact params that left (``{}`` for the toolhead pair, the
    printer's own id pair for a tray). ``status`` is what the printer answered and ``accepted`` is
    true only for its explicit 0; ``error_code`` keeps a refusal's number. ``observed`` is the one
    read-only look allowed after an accepted or unanswered command — never after a refusal on record,
    never on a dead session — the 1002 fields for a toolhead verb and the tray reading for a canvas
    verb, each named apart from acceptance. ``verified_on_this_printer`` is false and stays false
    until a machine says otherwise. ``notes`` carry what must be said out loud and ``as_dict()`` is
    the shape a tool would report."""
    verb: str
    method: int
    sent: dict
    status: ControlStatus
    waited_s: float
    accepted: bool = False
    error_code: int | None = None
    observed: dict | None = None
    verified_on_this_printer: bool = False
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "action": self.verb, "method": self.method, "sent": dict(self.sent),
            "status": str(self.status), "accepted": self.accepted, "error_code": self.error_code,
            "waited_s": self.waited_s, "verified_on_this_printer": False, "unverified": UNVERIFIED,
            **(self.observed or {}), "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- the argument bar


def _refuse(message: str, hint: str, reason: str) -> NoReturn:
    """Every refusal this module can make before a request exists, in one shape: what was refused,
    that nothing was sent, and where the real values come from."""
    raise PrinterError("protocol_error", message, hint=hint, reason=reason)


def _plain_id(value: Any, name: str) -> int:
    """One CANVAS identifier: a non-negative whole number as the printer's own 2005 reply reports it.
    A bool is an int in Python and never an identifier; text, floats, negatives and everything else
    refuse, because a tray id is an address and a wrong address is a different spool."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _refuse(f"{name} must be a whole number the printer itself reported, not "
                f"{str(value)[:40]!r}. Nothing was sent.",
                "Read the printer's CANVAS listing (cc2_status) and use the canvas_id and tray_id "
                "it prints. The numbers are the printer's own and start at 0; this module never "
                "guesses one and never hardcodes one.", reason="canvas_id")
    return value


def params_for(verb: str, canvas_id: Any = None, tray_id: Any = None) -> dict:
    """The params for one verb: nothing at all for the toolhead pair (no source sends a parameter),
    the printer-reported id pair for a tray. An id given to a toolhead verb is refused rather than
    dropped, because a caller that believes it addressed a tray and sent a bare 1024 would be wrong
    about what moved."""
    if verb not in _METHODS:
        raise ValueError(f"unknown CC2 filament verb {verb!r} — this module speaks only "
                         f"{sorted(_METHODS)}")
    if verb not in CANVAS_VERBS:
        if canvas_id is not None or tray_id is not None:
            _refuse(f"{verb} takes no canvas or tray: 1024 and 1025 move the toolhead itself and "
                    "carry no parameters in any source. Nothing was sent.",
                    "For a CANVAS tray use canvas_load or canvas_unload with the ids the printer's "
                    "own 2005 reply reports. For the toolhead, pass no ids.", reason="verb_ids")
        return {}
    return {"canvas_id": _plain_id(canvas_id, "canvas_id"), "tray_id": _plain_id(tray_id, "tray_id")}


def checked_params(verb: str, params: Any) -> dict:
    """The second lock the sibling controls keep: the params that may leave are exactly the frame
    for that verb. The tools build their own with ``params_for``, so this fires on a caller that
    hand-rolled a frame — and a 1024 carrying a length, or a 2001 carrying a filament colour or a
    slot map, never reaches a printer."""
    if not isinstance(params, dict):
        _refuse(f"CC2 {verb} params must be an object of the protocol's own fields, not "
                f"{type(params).__name__}. Nothing was sent.",
                "Use params_for() to build the frame.", reason="filament_frame")
    if verb not in CANVAS_VERBS:
        if params:
            _refuse(f"CC2 {verb} sends no parameters; {sorted(str(k) for k in params)[:4]} is not "
                    "an empty frame. Nothing was sent.",
                    "The toolhead commands are sent bare, exactly as Elegoo's own control sends "
                    "them.", reason="filament_frame")
        return {}
    if set(params) != {"canvas_id", "tray_id"}:
        _refuse(f"CC2 {verb} params are exactly {{'canvas_id': n, 'tray_id': n}}; got "
                f"{sorted(str(k) for k in params)[:6]}. Nothing was sent.",
                "Two ids, both the printer's own, and nothing else: a tray command that also "
                "carried filament info would be a second command nobody asked for.",
                reason="filament_frame")
    return params_for(verb, params["canvas_id"], params["tray_id"])


def method_for(verb: str) -> int:
    if verb not in _METHODS:
        raise ValueError(f"unknown CC2 filament verb {verb!r} — this module speaks only "
                         f"{sorted(_METHODS)}")
    return _METHODS[verb]


# --------------------------------------------------------------------------- the printer's own ids


def find_tray(canvas: st.Cc2CanvasStatus, canvas_id: int, tray_id: int) -> st.Cc2Tray | None:
    """The tray the printer named by that exact pair, or None. A tray id means nothing across canvas
    units (the state module says the same for a job), so both halves have to appear together in the
    printer's own reply — and a printer that reports two CANVAS units is not addressable here at all,
    because ``cc2_state`` refuses that reply rather than half-reading it."""
    for tray in canvas.trays:
        if tray.tray_id == tray_id and tray.canvas_id == canvas_id:
            return tray
    return None


def unit_is_connected(canvas: st.Cc2CanvasStatus, canvas_id: int) -> bool | None:
    """Is *that* unit connected? True/False from its own entry, None when the reply says nothing
    about it. The panel offers its buttons per unit; the whole-canvas ``connected`` is the union and
    is not what a tray's own unit reported."""
    info = canvas.raw.get("canvas_info")
    if not isinstance(info, dict):
        info = canvas.raw            # the same tolerance cc2_state applies to a bare inner object
    for entry in info.get("canvas_list") or []:
        if isinstance(entry, dict) and entry.get("canvas_id") == canvas_id:
            raw = entry.get("connected")
            if raw is True or raw == 1:
                return True
            if raw is False or raw == 0:
                return False
            return None
    return None


def vendor_loaded_tray(canvas: st.Cc2CanvasStatus) -> st.Cc2Tray | None:
    """Elegoo's own ``LoadedSlotId``, computed its way: the first tray, in the printer's own order,
    whose reported status is exactly 2 (its enum member ``loaded``, the state this adapter calls
    ACTIVE). Its control uses that reading for both buttons — the tray that *is* it offers unload and
    not load, the ones that are not it offer load and not unload — which is why this module reports
    it in every preview rather than gating on it. Copied as a hard rule it would refuse the one
    unload the filament-interruption state exists for: when a spool runs out, its own status is what
    changed, so the tray worth pulling out is precisely the one that may no longer read 2. The
    printer's own ``error_code`` decides what it will accept, and the reading rides along so a person
    can see they are about to load a tray the printer already calls loaded."""
    for tray in canvas.trays:
        if tray.raw_state == 2:
            return tray
    return None


def sub_status_word(sub_status: int) -> str | None:
    """What the client's or the integration's enum calls that sub-status, for these operations only,
    or None. A word from an enum, not an interpretation: no source documents the behaviour."""
    return EXTRUDER_SUBSTATUSES.get(sub_status) or CANVAS_DRIVE_SUBSTATUSES.get(sub_status)


# --------------------------------------------------------------------------- the gates


def _state_allowed(machine: st.Cc2MachineStatus, verb: str) -> bool:
    """One of the states the vendor control offers this family in, exactly: (status 1) or
    (status 2, sub 2504) for the toolhead pair, plus (status 10) and (status 2, sub 2502 or 2505)
    for a tray. Every other number, including a state this adapter does not know, is not on the
    vendor's list — which is a refusal of the command, not a claim about the machine."""
    want = _CANVAS_STATES if verb in CANVAS_VERBS else _TOOLHEAD_STATES
    for status, sub in want:
        if machine.machine_status == status and (sub is None or machine.sub_status == sub):
            return True
    return False


def _gate(machine: st.Cc2MachineStatus, verb: str) -> tuple[str, str] | None:
    """Why a confirmed call of this exact command would be refused now on state alone, as (short
    reason, the printer's own words), or None. The rule is the vendor control's own; nothing is
    added — no temperature, no purge, no bed assertion, because no source states one here."""
    if _state_allowed(machine, verb):
        return None
    return (("Elegoo's own control offers these commands only when the printer is idle, homing, "
             "paused or interrupted at a filament, and it reports none of those"),
            f"machine status {machine.machine_status}, sub-status {machine.sub_status}")


def _tray_gate(canvas: st.Cc2CanvasStatus, verb: str, canvas_id: int, tray_id: int
               ) -> tuple[str, str] | None:
    """The tray half of the vendor panel's gates: the pair must be one the printer reported, that
    unit must be the one it says is connected, and the tray may not be one the panel would grey out
    — empty (status 0), a state no adapter can read, or nothing recognisable in it."""
    tray = find_tray(canvas, canvas_id, tray_id)
    if tray is None:
        return ("the printer's own CANVAS reply does not name that canvas and tray",
                (f"asked canvas_id {canvas_id} tray_id {tray_id}; it names "
                 + ", ".join(f"{t.canvas_id}/{t.tray_id}" for t in canvas.trays)))
    if unit_is_connected(canvas, canvas_id) is not True:
        return ("that CANVAS unit does not report itself connected", f"canvas_id {canvas_id}")
    if verb == "canvas_load" and tray.state is st.TrayState.EMPTY:
        return ("the printer reports that tray empty, so there is nothing there to load",
                f"canvas_id {canvas_id} tray_id {tray_id} status {tray.raw_state}")
    if tray.state is st.TrayState.UNKNOWN:
        return (("that tray reports a state this adapter does not know, so nothing can be claimed "
                 "about its filament"), f"raw status {tray.raw_state}")
    if verb == "canvas_load" and tray.color is None and tray.name is None:
        return (("the printer reports no colour and no name for that tray, which is what its own "
                 "control shows as an unrecognised spool — and it greys the same button"),
                f"canvas_id {canvas_id} tray_id {tray_id}")
    loaded = vendor_loaded_tray(canvas)
    if (verb == "canvas_load" and loaded is not None and loaded.tray_id == tray_id
            and loaded.canvas_id == canvas_id):
        return (("the printer reports that tray as the one already in its path, and its own control "
                 "offers that tray an unload, never a second load"),
                f"canvas_id {canvas_id} tray_id {tray_id} status {tray.raw_state}")
    return None


def _refusal(why: str, printer_says: str | None = None) -> PrinterError:
    """A refusal that sent nothing, in the motion module's shape: the short reason always in the
    message, the printer's own words whole in their own field."""
    details = {} if printer_says is None else {"printer_says": printer_says}
    return PrinterError("protocol_error", f"Filament command refused: {why}. Nothing was sent.",
                        hint="Nothing left the wire, so deciding again after reading the printer is "
                             "a fresh decision, never a resend.", reason="filament_gate", **details)


# --------------------------------------------------------------------------- the readings


def _machine_reading(machine: st.Cc2MachineStatus) -> dict:
    """The 1002 as this module reports it: the two numbers, the sub-status word the vendor enums give
    it for these operations (None when they name nothing), and the nozzle's own pair."""
    sub = machine.sub_status
    return {"machine_status": machine.machine_status, "sub_status": sub,
            "sub_status_word": sub_status_word(sub), "driving_a_filament_operation": sub in
            EXTRUDER_SUBSTATUSES or sub in CANVAS_DRIVE_SUBSTATUSES,
            "nozzle_temp": machine.nozzle_temp, "nozzle_target": machine.nozzle_target,
            "job_filename": machine.job_filename}


def _tray_reading(canvas: st.Cc2CanvasStatus, canvas_id: int, tray_id: int) -> dict:
    """The 2005 as this module reports it, with the addressed tray's own three readings and the two
    identity facts a person needs (the printer's active pair, and the tray Elegoo's rule would
    consider loaded). An absent tray is None, never a invented state."""
    tray = find_tray(canvas, canvas_id, tray_id)
    loaded = vendor_loaded_tray(canvas)
    return {"observed_active_canvas_id": canvas.active_canvas_id,
            "observed_active_tray_id": canvas.active_tray_id,
            "observed_canvas_connected": canvas.connected,
            "observed_tray_state": tray.state.value if tray else None,
            "observed_tray_raw_state": tray.raw_state if tray else None,
            "observed_tray_color": tray.color if tray else None,
            "vendor_loaded_tray_id": loaded.tray_id if loaded else None}


# --------------------------------------------------------------------------- the preview (read-only)


async def read_preview(transport: Any, verb: str, *, canvas_id: Any = None, tray_id: Any = None,
                       timeout_s: float | None = None) -> dict:
    """What this exact confirmed call would send, and what the printer says right now — the same
    ``confirm_required`` shape the other CC2 controls keep. The reads below are read-only and are all
    this path ever touches: 1002 for a toolhead verb, and the 1002-plus-2005 pair ``cc2_status``
    already issues for a tray verb. Arguments are checked before anything is read, so a preview of a
    nonsense pair asks the printer nothing, and a read that fails is raised rather than papered over:
    a preview built on nothing would be an invitation to confirm a guess."""
    method, params = method_for(verb), params_for(verb, canvas_id, tray_id)
    if verb in CANVAS_VERBS:
        both = await st.fetch_status(transport, timeout_s=timeout_s)
        machine, canvas = both.machine, both.canvas
        gate = _gate(machine, verb) or _tray_gate(canvas, verb, params["canvas_id"],
                                                  params["tray_id"])
        reading: dict = {**_machine_reading(machine), **_tray_reading(canvas, params["canvas_id"],
                                                                      params["tray_id"])}
    else:
        machine = await st.fetch_machine_status(transport, timeout_s=timeout_s)
        gate = _gate(machine, verb)
        reading = _machine_reading(machine)
    return {"confirm_required": True, "action": verb,
            "preview": {"will_send": {"method": method, "params": dict(params)},
                        "waits_up_to_s": FILAMENT_WAIT_S,
                        "would_refuse": None if gate is None else gate[0],
                        "vendor_loaded_tray_note": ("Elegoo's own control offers load and unload by "
                                                    "the first tray reporting status 2; this module "
                                                    "reports that reading and lets the printer "
                                                    "answer for itself."),
                        "no_temperature_condition": NO_TEMPERATURE_RULE,
                        "verified_on_this_printer": False, "unverified": UNVERIFIED, **reading},
            "how": _PREVIEW_HOW[verb]}


# --------------------------------------------------------------------------- the one command


async def _observe_quietly(transport: Any, verb: str) -> tuple[dict | None, str | None]:
    """Exactly one read-only look — 1002 for a toolhead verb, 2005 for a tray, which is the field
    Elegoo's control refreshes on success — or the honest note that it produced nothing. The only
    follow-up a sent command may ever cause; the command itself is never reconsidered."""
    try:
        if verb in CANVAS_VERBS:
            result = await transport.request(st.METHOD_CANVAS, {})
            canvas = st.Cc2CanvasStatus.from_result(result)
            return {"observed_active_canvas_id": canvas.active_canvas_id,
                    "observed_active_tray_id": canvas.active_tray_id,
                    "observed_canvas_connected": canvas.connected,
                    "observed_tray_count": canvas.tray_count,
                    "observed_tray_states": {f"{t.canvas_id}/{t.tray_id}": t.state.value
                                             for t in canvas.trays}}, None
        return _machine_reading(await st.fetch_machine_status(transport)), None
    except PrinterError as e:
        why = getattr(e, "message", None) or str(e)
        return None, f"{OBSERVE_MISSING} Why: {why[:170]}"


async def run_filament(transport: Any, verb: str, *, canvas_id: Any = None, tray_id: Any = None,
                       confirm: bool = False, timeout_s: float | None = None
                       ) -> Cc2FilamentResult:
    """Send the one filament method once and report what came back — or report, truthfully, that
    nothing readable came back. Without ``confirm=True`` nothing is sent at all and the call raises:
    the preview is the read-only call. A confirmed call checks its arguments, then the vendor
    control's own gates against the printer's live reply, and only then sends, with the client's own
    300 s window.

    Nothing is ever repeated. A missing, unreadable, late or lost answer is ambiguous — neither a
    refusal nor a failure, and never a reason to send again; the only follow-up is the single
    read-only look, and a dead session sends nothing further. An accepted command says what
    acceptance means (the printer did not refuse) and never claims filament moved."""
    method, params = method_for(verb), params_for(verb, canvas_id, tray_id)
    if confirm is not True:
        raise PrinterError("protocol_error",
                           "A filament command needs confirm=True: call with the same arguments and "
                           "no confirm to see what would be sent and what the printer says first. "
                           "Nothing was sent.",
                           hint="The preview is the read-only call. Show it for the user's go-ahead "
                                "unless they already authorized this action, then repeat with "
                                "confirm=true. One command per confirmation.",
                           reason="filament_unconfirmed")
    wait_s = FILAMENT_WAIT_S if timeout_s is None else timeout_s
    sent = dict(params)

    def base(status: ControlStatus, **over) -> dict:
        d = {"verb": verb, "method": method, "sent": sent, "waited_s": wait_s, "status": status}
        d.update(over)
        return d

    # The gate read happens first, on purpose: a call that sends nothing must also have asked nothing
    # after it, so a refusal leaves no command half-formed and no read to be stale about.
    try:
        if verb in CANVAS_VERBS:
            both = await st.fetch_status(transport)
            machine, canvas = both.machine, both.canvas
            gate = _gate(machine, verb) or _tray_gate(canvas, verb, params["canvas_id"],
                                                      params["tray_id"])
        else:
            machine = await st.fetch_machine_status(transport)
            gate = _gate(machine, verb)
    except PrinterError as e:
        why = getattr(e, "message", None) or str(e)
        raise PrinterError("protocol_error",
                           "Filament command refused: the printer's state could not be read to "
                           "check it against the vendor control's own gates. Nothing was sent.",
                           hint="Read the state deliberately (cc2_status) and decide again — the "
                                "printer has not been asked to move any filament.",
                           reason="filament_gate_read", printer_says=why[:200]) from None
    if gate is not None:
        why, printer_says = gate
        raise _refusal(why, printer_says)

    try:
        reply = await transport.request(method, sent, timeout_s=wait_s)
    except Cc2TimeoutError:
        notes = [job.LOST_NOTE.format(verb=verb, wait=wait_s), LONG_OP_NOTE]
        observed, lost_note = await _observe_quietly(transport, verb)
        if lost_note is not None:
            notes.append(lost_note)
        if verb in CANVAS_VERBS:
            notes.append(TRAY_OBSERVE_TAIL)
        return Cc2FilamentResult(**base(ControlStatus.AMBIGUOUS, observed=observed,
                                        notes=tuple(notes)))
    except PrinterError as e:
        why = getattr(e, "message", None) or str(e)
        return Cc2FilamentResult(**base(ControlStatus.AMBIGUOUS,
                                        notes=(job.DEAD_NOTE.format(verb=verb) + why,)))

    # ``error_code``: the job controls' bar, applied to a filament write. Only an explicit integer
    # answers; a missing, bool, float or text code is an ambiguity the caller resolves by reading
    # state deliberately, never by sending the mechanism another command.
    if not isinstance(reply, dict) or "error_code" not in reply:
        detail = "carried no error code at all"
        return Cc2FilamentResult(**base(ControlStatus.AMBIGUOUS,
                                      notes=(job.UNREADABLE_NOTE.format(verb=verb, detail=detail),)))
    code = reply["error_code"]
    if not job.plain_int(code):
        detail = f"carried an error code that cannot be read ({type(code).__name__})"
        return Cc2FilamentResult(**base(ControlStatus.AMBIGUOUS,
                                      notes=(job.UNREADABLE_NOTE.format(verb=verb, detail=detail),)))
    if code != 0:
        return Cc2FilamentResult(**base(
            ControlStatus.REFUSED, error_code=code,
            notes=(job.refusal_note(verb, code), job.NO_OBSERVE_AFTER_REFUSAL)))

    observed, lost_note = await _observe_quietly(transport, verb)
    notes = [job.ACCEPT_BASE.format(verb=verb), _ACCEPT_TAILS[verb]]
    if verb in CANVAS_VERBS:
        notes.append(TRAY_OBSERVE_TAIL)
    else:
        notes.append(NO_TEMPERATURE_RULE)
    if lost_note is not None:
        notes.append(lost_note)
    return Cc2FilamentResult(**base(ControlStatus.ACCEPTED, accepted=True, observed=observed,
                                    notes=tuple(notes)))
