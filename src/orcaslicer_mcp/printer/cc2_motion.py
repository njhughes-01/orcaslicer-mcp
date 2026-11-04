"""CC2 manual homing (method 1026) and single-axis manual movement (method 1027) as an internal
candidate: the preview/``confirm`` interface the other CC2 controls already keep, one command at
most, reported as accepted (an explicit ``error_code`` 0), refused (a nonzero code, kept) or
ambiguous (no answer, an unreadable one, or a dead session), and a lost answer never resent. The
acceptance vocabulary, the decode bar and the note wording are ``cc2_control``'s own, imported
rather than reworded.

**What the sources say, and where each fact comes from.** Both request shapes and both waits are
read out of Elegoo's own LAN control (``webapp/lan/index.html``, the page the printer serves
itself), not inferred:

* **1026 HOME_AXES.** The client's home call is literally
  ``{method: XYZHomeControl, params: {homed_axes: axes.toLowerCase()}}`` awaited with ``5e3*10`` —
  fifty seconds — and its four buttons pass ``"XYZ"``, ``"X"``, ``"Y"``, ``"Z"``, so the strings this
  protocol is shown meaning are ``"xyz"``, ``"x"``, ``"y"``, ``"z"``. The protocol doc's methods
  table agrees on the field and the example (``{"homed_axes": "xyz"}``), and its status table lists
  the *report* domain of that same word as ``""``, ``"x"``, ``"xy"``, ``"xyz"``.
* **1027 MOVE_AXES.** Its move call is
  ``{method: XYZMoveControl, params: {axes: axes.toLowerCase(), distance: step * dir}}`` awaited with
  ``5e3*5`` — twenty-five seconds — where ``axes`` is one axis, ``step`` is the control's own step
  dropdown (the four choices 0.1, 1, 10, 30 mm, default 10) and ``dir`` is +1 for an up button, -1
  for a down button, **with Z inverted by the client itself** (``axis === "Z" && (dir = -dir)``).
  So the numbers the vendor's own arrows put on the wire are: X up ``+step``/X down ``-step``,
  Y up ``+step``/Y down ``-step``, Z up ``-step``/Z down ``+step``. ``vendor_button_distance``
  reproduces that table and nothing else.
* **What no source says, and which this module therefore does not say either: which way a positive
  ``distance`` travels.** Not the doc (which gives the field and one example, ``{"axes": "z",
  "distance": 10.0}``), not the client (which only ever multiplies a step by a button), not the
  firmware package. It is a closed middleware between the two and it is not in any source we hold.
  That is why the argument here is the signed wire number itself, why the button table is offered
  beside it, and why no parameter of this module is named ``up`` or ``down`` or ``raise``: naming a
  direction would be this project inventing a coordinate system.
* **Two limits, both the vendor client's own, neither invented here.** ``|distance|`` may not leave
  the 0.1–30 mm envelope its step dropdown can produce — no source shows a manual move outside it,
  and refusing beyond it is cheaper than guessing a machine's travel. And this module asserts nothing
  about bed geometry or travel bounds: the numbers in the firmware package's ``printer.cfg``
  (X -2.1..256.5, Y -2.1..270.0, Z -10..258.0) belong to a firmware package, not to a read of this
  machine, and a 30 mm relative move needs neither to be described.
* **Two gates are the vendor client's own behaviour, not additions.** The whole XYZ pad is dead
  unless the printer is idle (``xyzDisabled = computed(() => !isIdle)``), and a move is refused
  client-side when the printer's own homing report does not name the axis (its toast is literally
  ``homingBeforeMove``). The idle check is ``cc2_state``'s ``ready_for_job`` and the report is read
  from the 1002 the call already takes. A homing report that cannot be read is no reading: it
  refuses nothing, because the doc's example nests the field under ``toolhead`` while its own
  mapping table and the LAN client both spell it ``tool_head``, and guessing ``""`` here would block
  every legitimate move on a naming difference.
* **Neither method carries a speed, feedrate or acceleration** (the doc's table and both client
  frames carry only the fields above), so a manual move runs at whatever the firmware decides.

``bed_clear`` exists because a manual move is the one control on this server whose hazard is
physical rather than logical: a relative move toward the bed, with the nozzle near the bed, is a
crash, and no field of 1002 proves a clear bed — the printer being idle says nothing about what is
on the mat. So a confirmed move requires the caller's exact ``bed_clear=True`` assertion (the rule
``cc2_start`` holds a job start to), and an accepted move says in its own note that the assertion
was the caller's and cannot be checked here.

One command, never a second one: the transport's rule, and the reason the confirmed path reads the
1002 it needs for its gates *before* it sends, so a refused call has asked the printer nothing it
could mistake for a command. After an accepted or unanswered command there is exactly one read-only
1002 for the observation; after a refusal on record there is none, and after a dead session there is
none it could carry.

**Not exposed.** ``server.py`` declares no tool for these two methods, ``CC2_TOOL_NAMES``,
``_TOOL_ANNOTATIONS`` and ``_OPEN_WORLD_TOOLS`` do not name one, and no README or manifest mentions
it — a test holds that line. Manual motion is the first thing on this printer that can mark a
machine up, and until a home and a small move have been run against the real machine with Nathan
watching, this stays an internal candidate that no client is offered. Privacy: like its siblings
this takes no address, token or environment variable — the transport is the pinned session — and no
note carries credentials or a raw reply."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, NoReturn

from . import cc2_control as job
from . import cc2_state as st
from .cc2_control import ControlStatus  # the same three-way answer, one vocabulary
from .cc2_transport import Cc2TimeoutError, PrinterError

METHOD_HOME_AXES = 1026    # HOME_AXES — {"homed_axes": "xyz" | "x" | "y" | "z"}
METHOD_MOVE_AXES = 1027    # MOVE_AXES — {"axes": "x" | "y" | "z", "distance": <mm>}

# action -> the one method it sends, and how long its answer is waited for: the LAN client's own
# windows (5e3*10 for home, 5e3*5 for a move), not chosen here.
_METHODS: dict[str, int] = {"home": METHOD_HOME_AXES, "move": METHOD_MOVE_AXES}
MOTION_WAIT_S: dict[str, float] = {"home": 50.0, "move": 25.0}

# The axes strings Elegoo's own buttons send, lowercased as the client lowercases them. "xy" is
# refused on purpose: it appears in the doc only as a possible *report* of what is already homed,
# and no first-party caller has ever been read sending it as a request.
HOME_AXES = ("x", "y", "z", "xyz")
MOVE_AXES = ("x", "y", "z")          # one axis per request in every source

# The step dropdown's own four choices and the envelope they make. |distance| outside it refuses.
VENDOR_STEPS_MM = (0.1, 1.0, 10.0, 30.0)
STEP_MIN_MM, STEP_MAX_MM = 0.1, 30.0

# The client's button -> wire-number table, read off its own arrows (onXUp passes +1, onXDown -1,
# onZUp +1, onZDown -1) and its own Z inversion. It is a transcription, not an interpretation:
# nothing here claims that any of these numbers means "up" once it reaches the machine.
BUTTON_SIGN: dict[tuple[str, str], int] = {
    ("x", "up"): 1, ("x", "down"): -1,
    ("y", "up"): 1, ("y", "down"): -1,
    ("z", "up"): -1, ("z", "down"): 1,
}

# The two key spellings the sources disagree about (the doc's example vs the doc's own mapping table
# and the LAN client). Both are tried; neither is asserted.
_HOME_KEYS = ("tool_head", "toolhead")

# ---- notes, each inside the direct tools' reply cap (job.NOTE_CAP) ----

SIGN_NOTE = ("No source says which way a positive 1027 distance travels. These numbers copy "
             "Elegoo's own control: X/Y up +step and down -step, and its client inverts Z, so its "
             "Z-up button sends -step.")

BED_NOTE = ("This move went because the caller asserted bed_clear=True. No CC2 status field proves "
            "a clear bed — idle is not clear — so that assertion is the caller's, not a fact this "
            "call could check.")

_ACCEPT_TAILS = {
    "home": ("Accepted is the printer's answer to the command; the observed homing report is one "
             "separate read-only look and never part of acceptance. Neither motion method carries a "
             "speed, so homing runs at the firmware's own feedrate."),
    "move": ("Accepted is the printer's answer to the command; the observed position is one "
             "separate read-only look and never the evidence. What the sign means is the note "
             "beside this one, never a guess of this module's."),
}

# The preview's own how: the job controls' PREVIEW_HOW plus what a motion call additionally asks for.
_PREVIEW_HOW = {
    "home": ("Nothing was sent to the printer. Show this preview for the user's go-ahead unless "
             "they already authorized it, then call again with confirm=true. One command per "
             "confirmation, and the printer must be idle — its own control will not offer motion "
             "otherwise."),
    "move": ("Nothing was sent. Show this preview — distance and sign — for the user's go-ahead, "
             "then call again with confirm=true and bed_clear=True: a manual move is the one "
             "control here that can mark up a machine, so the bed assertion is required, not "
             "advised."),
}

# Each gate reads in two parts: a short reason that always fits a reply, and the printer's own words
# beside it in their own field. They are never concatenated and cut, because the half of a truncated
# sentence a model acts on is the half that was lost.
_NOT_IDLE = ("Elegoo's own control offers no manual motion unless the printer is idle, and it is "
             "not")
_UNHOMED = ("the printer's own homing report does not name that axis, and its control refuses the "
            "same move")
_GATE_TAIL = ". Nothing was sent."

# The candidate's own honesty mark, the one its siblings carry: False until a home and a small move
# have been run against the real machine with somebody watching it, and said out loud in every reply.
UNVERIFIED = ("Neither 1026 nor 1027 has been sent to a CC2 by this project: the two shapes, the "
              "two waits and both gates are read off Elegoo's own LAN control, and nothing here has "
              "been seen to move this machine.")


@dataclass(frozen=True)
class Cc2MotionResult:
    """The whole outcome of one motion command. ``kind`` is "home" or "move", ``sent`` the exact
    params that left, ``status`` what the printer answered and ``accepted`` true only for its
    explicit 0. ``observed_*`` is one read-only 1002 taken after an accepted or unanswered command —
    never after a refusal on record, never on a dead session: ``observed_homed_axes`` is that report
    exactly as it arrived (None when it cannot be read), ``observed_position`` the position block if
    the frame carried one, and the two status numbers as integers interpreted by nothing. ``notes``
    carry what must be said out loud; ``as_dict()`` is the shape a tool reports."""
    kind: str
    method: int
    sent: dict
    status: ControlStatus
    waited_s: float
    accepted: bool = False
    error_code: int | None = None
    observed: dict | None = None
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "method": self.method,
            "sent": dict(self.sent),
            "status": str(self.status),
            "accepted": self.accepted,
            "error_code": self.error_code,
            "waited_s": self.waited_s,
            "verified_on_this_printer": False,
            "unverified": UNVERIFIED,
            **(self.observed or {}),
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- the argument bar


def _refuse(message: str, hint: str, reason: str) -> NoReturn:
    """Every refusal this module can make before a request exists, in one shape: it names what was
    refused, says nothing was sent, and gives the values the vendor's own control uses."""
    raise PrinterError("protocol_error", message, hint=hint, reason=reason)


def parse_home_axes(value: Any) -> str:
    """The 1026 axes string exactly: one of the four the vendor's buttons send, in any case
    (``"XYZ"`` reads, because that is the literal its Home-All button passes and the client is the
    one that lowercases it). Anything else refuses — an empty string, an ``"e"`` this protocol has
    never been shown homing, a repeated or comma-joined set, a list, a number, and ``"xy"``, which
    the doc lists only as a report of what is already homed and no caller has been read asking for."""
    if isinstance(value, str):
        word = value.strip().lower()
        if word in HOME_AXES:
            return word
    _refuse(f"Homing axes must be one of {list(HOME_AXES)} — the strings Elegoo's own control "
            f"sends; {str(value)[:40]!r} is not one of them. Nothing was sent.",
            "Pass 'xyz' for all three axes, or a single 'x', 'y' or 'z'. The protocol names no "
            "extruder homing and no other combination, so those refuse rather than guess at what "
            "the middleware would do with them.", reason="home_axes")


def parse_move_axis(value: Any) -> str:
    """The 1027 axis: exactly one of ``"x"``, ``"y"``, ``"z"``, in any case. One axis per request is
    every source's shape (the client sends its arrow's axis alone, the doc's example names ``"z"``
    alone), so a multi-axis move refuses rather than becoming two commands nobody asked for."""
    if isinstance(value, str):
        word = value.strip().lower()
        if word in MOVE_AXES:
            return word
    _refuse(f"A manual move commands one axis — 'x', 'y' or 'z'; {str(value)[:40]!r} is not one "
            "axis. Nothing was sent.",
            "Send one axis per move, the way the vendor control's arrows do. A diagonal is two "
            "moves, and two moves are two decisions a person should see.", reason="move_axis")


def parse_distance(value: Any) -> float:
    """The signed 1027 distance, accepted only inside the vendor control's own envelope. A real
    number (or plain numeric text), never a bool, never a bool-shaped anything, finite, not zero,
    and with a magnitude between 0.1 and 30 mm — the dropdown's smallest and largest choice. Beyond
    that no source has ever put a 1027, so this refuses instead of inventing a travel limit; the
    sign is the wire's own sign (``SIGN_NOTE``), which is why no direction word is accepted here."""
    number: float | None = None
    if isinstance(value, bool):
        number = None
    elif isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            number = None
    if number is None or not math.isfinite(number):
        _refuse(f"The move distance must be a plain number of millimetres; "
                f"{str(value)[:40]!r} is not one. Nothing was sent.",
                "Give the signed distance the wire takes, e.g. -10.0 — see the vendor button table "
                "in this module rather than assuming a direction.", reason="move_distance")
    if number == 0:
        _refuse("A distance of zero asks the printer to move nowhere, which is not a move any "
                "source sends. Nothing was sent.",
                "Use the smallest step the vendor control offers, 0.1 mm, when that is what is "
                "wanted.", reason="move_distance")
    if not STEP_MIN_MM <= abs(number) <= STEP_MAX_MM:
        _refuse(f"A manual move must stay inside the vendor control's own "
                f"{STEP_MIN_MM}-{STEP_MAX_MM} mm per command; {str(value)[:40]} is outside "
                "it. Nothing was sent.",
                f"The control's step dropdown offers {list(VENDOR_STEPS_MM)} mm and its arrows send "
                f"one step per press. A longer travel is several decisions, not one command, and "
                f"no source shows this printer taking a longer single move.",
                reason="move_distance_range")
    return number


def vendor_button_distance(axis: Any, direction: Any, step_mm: Any) -> float:
    """The exact number Elegoo's own control would put on the wire for that arrow at that step —
    its arrows multiply the step by +1 (up) or -1 (down) and its client then inverts Z. So
    ``("z", "up", 10.0)`` is ``-10.0`` and ``("z", "down", 10.0)`` is ``+10.0``. This is a
    transcription of a button press and is offered because it is the one distance whose meaning, at
    the vendor's own UI, is not in doubt; it says nothing about which way the machine travels."""
    key = (parse_move_axis(axis), direction.strip().lower() if isinstance(direction, str) else None)
    if key not in BUTTON_SIGN:
        _refuse(f"Manual direction must be 'up' or 'down' beside axis {key[0]!r}; "
                f"{str(direction)[:40]!r} is neither. Nothing was sent.",
                "The control has two arrows per axis. Ask for one of them.", reason="move_direction")
    return BUTTON_SIGN[key] * parse_distance(step_mm)


def home_params(axes: Any) -> dict:
    """The 1026 params: the one field, lowercased as the client lowercases it."""
    return {"homed_axes": parse_home_axes(axes)}


def move_params(axes: Any, distance: Any) -> dict:
    """The 1027 params: the two fields the client writes, and nothing else — no speed (the method
    has none), no second axis, no absolute target coordinate."""
    return {"axes": parse_move_axis(axes), "distance": parse_distance(distance)}


def checked_params(kind: str, params: Any) -> dict:
    """The second lock the sibling controls keep: the params a motion command may leave with,
    refused before a request exists if they are not exactly the documented frame. The tools build
    theirs with ``home_params``/``move_params``, so this fires only on a caller that hand-rolled a
    frame — and a 1027 carrying a speed, or a 1026 carrying an axis list, never reaches a printer."""
    if not isinstance(params, dict):
        raise PrinterError("protocol_error",
                           f"CC2 {kind} params must be an object of the protocol's own fields, "
                           f"not {type(params).__name__}. Nothing was sent.", reason="motion_frame")
    if kind == "home":
        if set(params) != {"homed_axes"}:
            raise PrinterError("protocol_error",
                               f"CC2 home params are exactly {{'homed_axes': ...}}; got "
                               f"{sorted(str(k) for k in params)[:6]}. Nothing was sent.",
                               reason="motion_frame")
        return home_params(params["homed_axes"])
    if set(params) != {"axes", "distance"}:
        raise PrinterError("protocol_error",
                           f"CC2 move params are exactly {{'axes': ..., 'distance': ...}}; got "
                           f"{sorted(str(k) for k in params)[:6]}. Nothing was sent.",
                           reason="motion_frame")
    return move_params(params["axes"], params["distance"])


# --------------------------------------------------------------------------- the two readings


def homed_axes_now(machine: st.Cc2MachineStatus) -> str | None:
    """The printer's own homing report, or None when it cannot be read. Both spellings the sources
    disagree over are tried (``tool_head`` per the doc's mapping table and the LAN client,
    ``toolhead`` per the doc's example), and an absent, non-object or non-string field is no reading
    at all — never ``""``, which would read as 'nothing is homed' and refuse honest moves on a
    naming difference."""
    for key in _HOME_KEYS:
        block = machine.raw.get(key) if isinstance(machine.raw, dict) else None
        if isinstance(block, dict) and isinstance(block.get("homed_axes"), str):
            return block["homed_axes"]
    return None


def axis_is_homed(machine: st.Cc2MachineStatus, axis: str) -> bool | None:
    """True/False on a readable report, None when there is no report to read. ``None`` refuses
    nothing: an unreadable field is not evidence that an axis is unhomed."""
    homed = homed_axes_now(machine)
    return None if homed is None else axis in homed.lower()


def _observed_fields(machine: st.Cc2MachineStatus) -> dict:
    return {"observed_homed_axes": homed_axes_now(machine),
            "observed_position": list(machine.position) if machine.position else None,
            "machine_status": machine.machine_status,
            "sub_status": machine.sub_status}


def _reading(machine: st.Cc2MachineStatus, axis: str | None) -> dict:
    """What the printer says now, for the preview. ``idle_reason`` is the state reader's own words,
    whole: they are its authored strings, and a reason cut mid-filename is not a reason. The gate's
    own short sentence lives in ``would_refuse``, beside these fields, never spliced out of them."""
    ok, why = machine.ready_for_job()
    out = {"printer_idle": ok, "idle_reason": why,
           "homed_axes_now": homed_axes_now(machine),
           "position_now": list(machine.position) if machine.position else None}
    if axis is not None:
        out["axis_homed"] = axis_is_homed(machine, axis)
    return out


# --------------------------------------------------------------------------- the gates


def _gate(machine: st.Cc2MachineStatus, kind: str, axis: str | None
          ) -> tuple[str, str] | None:
    """Why a confirmed call of this exact command would be refused now, as (short reason, the
    printer's own words), or None. Both gates are the vendor control's own: it offers no motion
    unless the printer is idle, and it will not move an axis its own homing report does not name.
    Nothing here adds a third opinion."""
    ok, why = machine.ready_for_job()
    if not ok:
        return _NOT_IDLE, str(why)
    if kind == "move" and axis_is_homed(machine, axis or "") is False:
        return _UNHOMED, f"homed_axes={str(homed_axes_now(machine))!r}"
    return None


def _refusal(reason: str, why: str, hint: str, printer_says: str | None = None) -> PrinterError:
    """A refusal that sent nothing. The short reason is always in the message; the printer's own
    words ride in their own field, whole, because the cap and the sentence both matter and a refusal
    whose reason was truncated is a refusal the caller cannot act on."""
    details = {} if printer_says is None else {"printer_says": printer_says}
    return PrinterError("protocol_error", f"Manual motion refused: {why}{_GATE_TAIL}", hint=hint,
                        reason=reason, **details)


# --------------------------------------------------------------------------- the preview (read-only)


async def read_preview(transport: Any, kind: str, *, axes: Any = None, distance: Any = None,
                       timeout_s: float | None = None) -> dict:
    """What this exact confirmed call would send, and what the printer says right now — the same
    ``confirm_required`` shape the other CC2 controls use. The 1002 below is the only method this
    path sends; nothing about homing or moving leaves the wire without ``confirm=True``, and the
    tests watch the recorded frame. Arguments are checked before anything is read, so a preview of a
    nonsense move asks the printer nothing, and a status read that fails is raised rather than
    papered over — a preview built on nothing would be an invitation to confirm a guess."""
    method, params = _frame(kind, axes, distance)
    machine = await st.fetch_machine_status(transport, timeout_s=timeout_s)
    gate = _gate(machine, kind, params.get("axes"))
    preview = {"will_send": {"method": method, "params": dict(params)},
               "waits_up_to_s": MOTION_WAIT_S[kind],
               "vendor_step_choices_mm": list(VENDOR_STEPS_MM),
               "sign_convention": SIGN_NOTE,
               "bed_clear_required": kind == "move",
               "would_refuse": None if gate is None else gate[0],
               "verified_on_this_printer": False,
               "unverified": UNVERIFIED,
               **_reading(machine, params.get("axes"))}
    return {"confirm_required": True, "action": kind, "preview": preview,
            "how": _PREVIEW_HOW[kind]}


# --------------------------------------------------------------------------- the one command


def _frame(kind: str, axes: Any, distance: Any) -> tuple[int, dict]:
    """The method and the frame for one motion command, or a refusal that never reaches the wire.
    The built frame is held to the same frame lock a hand-rolled one is, so the frame this module
    sends and the frame it refuses are checked by exactly one rule."""
    if kind not in _METHODS:
        raise ValueError(f"unknown CC2 motion kind {kind!r} — this module speaks only "
                         f"{sorted(_METHODS)}")
    params = home_params(axes) if kind == "home" else move_params(axes, distance)
    return _METHODS[kind], checked_params(kind, params)


async def _observe_quietly(transport: Any) -> tuple[dict | None, str | None]:
    """Exactly one read-only 1002, or the honest note that it did not land. The only follow-up a
    sent command may ever cause; the command itself is never reconsidered."""
    try:
        return _observed_fields(await st.fetch_machine_status(transport)), None
    except PrinterError as e:
        why = getattr(e, "message", None) or str(e)
        return None, f"{job.OBSERVE_LOST_NOTE}{why})"


async def run_motion(transport: Any, kind: str, *, axes: Any = None, distance: Any = None,
                     confirm: bool = False, bed_clear: bool = False,
                     timeout_s: float | None = None) -> Cc2MotionResult:
    """Home or move once, and report what came back — or report, truthfully, that nothing readable
    came back. Without ``confirm=True`` nothing is sent at all: the call raises the same refusal a
    caller who has not seen the preview deserves, and the preview is what sends the read-only 1002.
    A confirmed call requires ``bed_clear=True`` for a move (a move is the hazard), checks its
    arguments and the two vendor gates against one read-only 1002 first, and only then sends the one
    method with the vendor's own window.

    Nothing is ever repeated. A command whose answer is missing, unreadable, late or lost is
    ambiguous — neither a refusal nor a failure, and never a reason to send again; the only follow-up
    is the single read-only look, and a dead session sends nothing further."""
    method, params = _frame(kind, axes, distance)
    if confirm is not True:
        raise PrinterError(
            "protocol_error",
            f"Manual {kind} needs confirm=True: call with the same arguments and no confirm to see "
            "what would be sent and what the printer says first. Nothing was sent.",
            hint="The preview is the read-only call. Show it to the user for their go-ahead unless "
                 "they already authorized this motion, then repeat with confirm=true.",
            reason="motion_unconfirmed")
    if kind == "move" and bed_clear is not True:
        raise PrinterError(
            "protocol_error",
            "A manual move demands an explicit physical bed-clear assertion: pass bed_clear=True "
            "after looking at the bed. "
            + ("Nothing was sent." if bed_clear is False else
               f"bed_clear={bed_clear!r} is not that assertion; only the bool True is. "
               "Nothing was sent."),
            hint="Nothing on this printer's status says a bed is clear — idle says no job is "
                 "running, which is not the same claim. Look, then assert.",
            reason="bed_not_clear")

    wait_s = MOTION_WAIT_S[kind] if timeout_s is None else timeout_s
    sent = dict(params)

    def base(status: ControlStatus, **over) -> dict:
        d = {"kind": kind, "method": method, "sent": sent, "waited_s": wait_s, "status": status}
        d.update(over)
        return d

    # The gate read. It happens first, on purpose: a refusal that could have been a command would
    # be a second command waiting to happen, so a call that sends nothing must also have asked
    # nothing after it. A gate read that fails refuses rather than sending blind — and since
    # nothing left the wire, asking the printer again later is a fresh decision, never a resend.
    try:
        machine = await st.fetch_machine_status(transport)
    except PrinterError as e:
        why = getattr(e, "message", None) or str(e)
        raise _refusal("motion_gate_read", "the printer's state could not be read to check it "
                                           "against the vendor control's own gates",
                       "Nothing was sent. Read the state deliberately (cc2_status) and decide again "
                       "— the printer has not been asked to move.", why) from None
    if (gate := _gate(machine, kind, params.get("axes"))) is not None:
        why, printer_says = gate
        raise _refusal("motion_gate", why,
                       "Nothing was sent. Home the axis (this same tool, kind 'home') or bring the "
                       "printer back to idle, then preview again."
                       if kind == "move" else
                       "Nothing was sent. The vendor control offers no manual motion except when "
                       "the printer is idle; bring it back to idle and preview again.",
                       printer_says)

    try:
        reply = await transport.request(method, sent, timeout_s=wait_s)
    except Cc2TimeoutError:
        notes = [job.LOST_NOTE.format(verb=kind, wait=wait_s), SIGN_NOTE]
        observed, lost_note = await _observe_quietly(transport)
        if lost_note is not None:
            notes.append(lost_note)
        return Cc2MotionResult(**base(ControlStatus.AMBIGUOUS, observed=observed,
                                      notes=tuple(notes)))
    except PrinterError as e:
        why = getattr(e, "message", None) or str(e)
        return Cc2MotionResult(**base(ControlStatus.AMBIGUOUS,
                                      notes=(job.DEAD_NOTE.format(verb=kind) + why,)))

    # ``error_code``: the job controls' bar, applied to a motion write. Only an explicit integer
    # answers; a missing, bool, float or text code is an ambiguity the caller resolves by reading
    # state deliberately, never by sending the motion again.
    if not isinstance(reply, dict) or "error_code" not in reply:
        detail = "carried no error code at all"
        return Cc2MotionResult(**base(ControlStatus.AMBIGUOUS,
                                      notes=(job.UNREADABLE_NOTE.format(verb=kind, detail=detail),)))
    code = reply["error_code"]
    if not job.plain_int(code):
        detail = f"carried an error code that cannot be read ({type(code).__name__})"
        return Cc2MotionResult(**base(ControlStatus.AMBIGUOUS,
                                      notes=(job.UNREADABLE_NOTE.format(verb=kind, detail=detail),)))
    if code != 0:
        return Cc2MotionResult(**base(
            ControlStatus.REFUSED, error_code=code,
            notes=(job.refusal_note(kind, code), job.NO_OBSERVE_AFTER_REFUSAL)))

    observed, lost_note = await _observe_quietly(transport)
    notes = [job.ACCEPT_BASE.format(verb=kind), _ACCEPT_TAILS[kind]]
    if kind == "move":
        notes += [SIGN_NOTE, BED_NOTE]
    if lost_note is not None:
        notes.append(lost_note)
    return Cc2MotionResult(**base(ControlStatus.ACCEPTED, accepted=True, observed=observed,
                                  notes=tuple(notes)))
