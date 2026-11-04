"""INTERNAL CANDIDATE for CC2 fan control (method 1030) — not a tool, not exposed, not claimed to
work. Nothing imports this module: ``server.py`` declares no fan tool, ``CC2_TOOL_NAMES``,
``_TOOL_ANNOTATIONS`` and ``_OPEN_WORLD_TOOLS`` do not name one, and no manifest or README mentions
it; ``tests/test_cc2_fan.py`` fails if that posture changes. It exists so a controlled live test,
with Nathan and an idle printer present, has one already-checked frame to send instead of somebody
inventing one at the console.

WHY IT IS STILL A CANDIDATE. The three first-party sources do not agree about the number that rides
``fan``/``aux_fan``/``box_fan``, and none of them is this printer:

* **Elegoo's own LAN control** (the 3.6 MB bundle the printer serves, ``lan/index.html``) has exactly
  one fan caller. Its component handlers are ``setFanSpeed({modelFanSpeed: v/100*255})``,
  ``{auxiliaryFanSpeed: v/100*255}`` and ``{chassisFanSpeed: v/100*255}``, and the store they reach
  (``printer-mqtt``) sends ``{method: 1030, params: {fan, box_fan, aux_fan}}`` — each key omitted
  when its value is undefined — treating ``error_code === 0`` as success and passing ``G7*5``
  (``G7 = 1e3``, so 5 s). So the widget Nathan drags holds a **percent 0-100** and the number that
  leaves is that percent times 2.55, i.e. the **0-255 family, unreduced** (50 % leaves as 127.5,
  because the client does not round).
* **That same client's read-back agrees with the wire, not with the widget**: it paints the widgets
  from ``Math.round((status.fans.<fan>.speed ?? 0) * 100/255)`` for the same three fields, so the
  1002 answers a number the client divides by 2.55.
* **pycentauri** (the library the Home Assistant side is built on) validates ``0 <= val <= 100`` and
  sends those numbers **unconverted** under the same three field names — while the 1030 row of its
  own ``docs/PROTOCOL.md`` records ``{"fan": <0-255>, "aux_fan": <0-255>, "box_fan": <0-255>}`` with
  ``error_code: 0`` and "fan physically responds", and its status mapping copies
  ``fans.<fan>.speed`` (five fields, including ``controller_fan``/``heater_fan``) into
  ``CurrentFanSpeed`` verbatim.

So the UI and the integration's *manual* both point at 0-255 on the wire and only the integration's
*code* offers a percent, and this project has never had a CC2 answer 1030. This module therefore
takes the official UI's input semantics — an explicit **percent 0-100** — applies the official UI's
own arithmetic to reach the wire value, and refuses to call either number proven: every reply says
``verified_on_this_printer: false``, the wire formula is shown before anything is sent, and the
read-back reports the printer's number **exactly as it answered it** with no percent attached.

WHAT THAT MEANS FOR THE READ-BACK, deliberately. ``cc2_peripherals`` holds 1030 back on the same
disagreement, and this module does not settle it. Converting the printer's number by the UI's
formula would report a percentage the unit may not be, so the read-back carries raw numbers plus a
per-fan ``changed`` fact (did that number move after the accepted command — raw against raw, no
unit implied). A live test reads its answer out of that: after an accepted 100 % the printer
reporting 255 is the UI's formula holding, a 100 would say pycentauri's code is right and the UI's
scalar is decorative, and no movement at all says the field is not the part-cooling fan.

SCOPE, kept narrow on purpose. One method (1030), the three fields the official UI actually writes,
``controller_fan``/``heater_fan`` are read-only observations this module never commands, and the
older websocket frame the same bundle still carries (``CmdDeviceStatusControl`` with
``TargetFanSpeed: {ModelFan, AuxiliaryFan, BoxFan}``, fed raw from ``CurrentFanSpeed`` and reaching no
caller) is never sent — one shape, from the client that still ships, with no invented variant.
Acceptance, refusal and ambiguity are the job controls' vocabulary imported from ``cc2_control``
rather than reworded; the wait is the longer of the two first-party windows (the client's 5 s here,
pycentauri's 10 s command timeout) as ``cc2_peripherals`` also takes; a command goes once and is
never resent; the only follow-up is a single read-only 1002, never after an on-record refusal and
never on a dead session. Nothing here contacts a printer, a network or a credential: the transport is
the pinned session, and no note or reply carries an address, a serial or a token."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import cc2_control as job
from . import cc2_state as st
from .cc2_control import ControlStatus
from .cc2_transport import Cc2TimeoutError, PrinterError

SETTING = "fan"
METHOD_SET_FAN = 1030             # FanControl in the official client's own enum

# The three fields the official client writes, beside the label Nathan's own sources use for them
# (its model/auxiliary/chassis words and pycentauri's model/auxiliary/chamber spellings agree on
# these three and only these three). Order carries no meaning: the client's own key order differs.
FAN_FIELDS: dict[str, str] = {"model": "fan", "auxiliary": "aux_fan", "chamber": "box_fan"}
FANS: tuple[str, ...] = tuple(FAN_FIELDS)

# The official client waits 5 s for this one call (G7*5, G7 = 1e3); pycentauri waits 10 s for every
# command it sends. The longer, for the reason cc2_peripherals gives for the same choice: the short
# window is what turns a command the printer took into a report of an answer that never arrived.
SET_WAIT_S: float = 10.0

PERCENT_MAX = 100                 # the official widget's own ceiling; nothing scales it again
WIRE_MAX = 255                    # the number that percent becomes under the UI's arithmetic

# Every authored string stays under the direct tools' reply cap (job.NOTE_CAP), asserted in
# tests/test_cc2_fan.py, so nothing a future registration says gets cut mid-sentence.
UNVERIFIED = (
    "No CC2 has ever answered 1030 for this project, so the fan scale on Nathan's firmware is "
    "unproven: the official LAN control multiplies its percent by 2.55 before sending, the "
    "integration's manual says 0-255, and its own code sends 0-100 unconverted."
)
SCALE_NOW = (
    "Wire value = percent / 100 * 255, Elegoo's own LAN control's arithmetic, unreduced (50% leaves "
    "as 127.5 because that client rounds nothing). Not a claim about this firmware."
)
READBACK_UNIT = (
    "The printer's own number for each fan, exactly as its 1002 answered fans.<fan>.speed. No unit "
    "is claimed for it and no percentage is derived from it here."
)
CONTRADICT = (
    "Note the disagreement: the printer accepted the fan command and its own status still reports "
    "{fan} as the same number it reported before ({value}). Reported as a fact of raw-against-raw; "
    "accepted and observed are separate and neither earns a second command."
)
OBSERVE_UNKNOWN_NOTE = (
    "The one read-only status look this call allows itself named none of these fans, so whether any "
    "of them moved is unknown. The command is not reconsidered and nothing is resent."
)
_AT_LEAST_ONE = (
    "Pass the fan(s) to change as model / auxiliary / chamber, each a whole percent 0-100. A fan you "
    "leave out is left alone — the official client omits it from the request too."
)


@dataclass(frozen=True)
class Cc2FanResult:
    """The whole outcome of one fan command. ``status`` says what the printer answered the 1030 and
    ``accepted`` is true only for its explicit integer 0; ``error_code`` keeps a refusal's number;
    ``sent`` is the exact params that left, so a caller can see the number the printer was actually
    given. ``observed`` is one read-only 1002 taken after an accepted or unanswered command — never
    after a refusal on record, never on a dead session — carrying each fan's raw number afterwards
    and whether it moved (``changed`` True/False, None when either look said nothing). ``asked``
    keeps the percent this call was given beside the wire value it became, because those are the two
    numbers a live test has to compare. ``verified_on_this_printer`` is False and stays False until a
    real CC2 answers 1030; ``notes`` carry what must be said out loud and ``as_dict()`` is the shape
    a tool would report."""
    method: int
    sent: dict
    status: ControlStatus
    waited_s: float
    accepted: bool = False
    error_code: int | None = None
    asked: dict | None = None
    observed: dict | None = None
    verified_on_this_printer: bool = False
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        seen = self.observed or {}
        return {
            "setting": SETTING, "method": self.method, "sent": dict(self.sent),
            "status": str(self.status), "accepted": self.accepted,
            "error_code": self.error_code, "waited_s": self.waited_s,
            "asked_percent": dict(self.asked or {}),
            "observed_fan_raw": dict(seen.get("raw") or {}),
            "observed_fan_changed": dict(seen.get("changed") or {}),
            "readback_unit": READBACK_UNIT,
            "verified_on_this_printer": self.verified_on_this_printer,
            "unverified": UNVERIFIED,
            "notes": list(self.notes),
        }



# --------------------------------------------------------------------------- the argument bar


def parse_percent(value: Any, fan: str) -> int:
    """One fan's speed as the official UI takes it: a whole percent 0..100. Whole-number text ("50")
    reads, because some clients send every argument as text; a bool does not (True is an int in
    Python and is not a speed), and neither does a float, a negative, 101, a raw 0-255 number above
    100 or a name. Refused before the printer is asked, with the scale named, because guessing which
    of the two competing scales a bare 127 was meant in is exactly the guess this module may not
    make. A caller holding a 0-255 number converts it to a percent deliberately, out loud, and says
    so; this never accepts one as a percent and never accepts one as a wire value."""
    number: int | None = None
    if isinstance(value, int) and not isinstance(value, bool):
        number = value
    elif isinstance(value, str) and value.strip().lstrip("+").isdigit():
        number = int(value.strip())
    if number is None or not 0 <= number <= PERCENT_MAX:
        raise PrinterError(
            "protocol_error",
            f"Fan speed must be a whole percent 0-{PERCENT_MAX}; {str(value)[:40]!r} is not one. "
            "Nothing was sent.",
            hint=f"Pass {fan} as a percent 0-100, the way Elegoo's own LAN control takes it, and "
                 f"this tool sends the number that control sends. {_AT_LEAST_ONE}",
            reason="fan_percent_range")
    return number


def wire_value(percent: int) -> int | float:
    """The number that goes on the wire for ``percent``: Elegoo's own LAN control computes
    ``percent / 100 * 255`` and sends the product **unreduced**, so this reproduces that exactly —
    0 -> 0, 20 -> 51, 50 -> 127.5, 100 -> 255 — including the fraction, because inventing a round()
    here would be this project's fingerprint on a frame the official client does not write. The
    firmware's taste in fractional numbers is unproven, and that is worth discovering on a controlled
    test rather than hiding behind a rounding."""
    value = percent * WIRE_MAX / PERCENT_MAX
    return int(value) if float(value).is_integer() else value


def fan_params(values: dict[str, Any]) -> dict:
    """The 1030 params: one key per named fan, each value the official client's product, and a fan
    whose value is None simply is not sent — the omission the official client performs with
    ``Te.modelFanSpeed !== void 0 &&``. An empty set refuses, because a 1030 with no fan field is a
    command nobody's source has ever shown, and an unknown fan name refuses rather than becoming a
    field name invented from a guess (``controller_fan``/``heater_fan`` are real field names in the
    integration's status mapping and no source commands them, so they refuse too)."""
    if not isinstance(values, dict):
        raise PrinterError("protocol_error",
                           f"Fan speeds must be given as {sorted(FAN_FIELDS)}; "
                           f"{type(values).__name__} is not that. Nothing was sent.",
                           hint=_AT_LEAST_ONE, reason="fan_argument")
    params: dict[str, Any] = {}
    for fan, value in values.items():
        if fan not in FAN_FIELDS:
            raise PrinterError(
                "protocol_error",
                f"{str(fan)[:40]!r} is not a fan this module commands. Nothing was sent.",
                hint="Command model, auxiliary or chamber only — the three fields Elegoo's own LAN "
                     "control writes. controller_fan and heater_fan appear in the integration's "
                     "status read and nothing commands them.",
                reason="fan_name")
        if value is None:
            continue
        params[FAN_FIELDS[fan]] = wire_value(parse_percent(value, fan))
    if not params:
        raise PrinterError("protocol_error",
                           "No fan speed was given, so there is nothing to send. Nothing was sent.",
                           hint=_AT_LEAST_ONE, reason="fan_none")
    return params


# The frames this protocol is evidenced to mean are every subset of the three fields — so the second
# lock is a key check plus a value check, not a fixed whitelist, and it fires on a caller that
# hand-rolled a frame with, say, a bare 255 or a fourth field.
def checked_params(params: Any) -> dict:
    # ValueError for a hand-rolled frame, not TypeError, because cc2_peripherals' checked_params is
    # the house pattern this mirrors and a caller reads one rule for both modules.
    if not isinstance(params, dict):
        raise ValueError(f"CC2 fan params must be an object of the protocol's own fields, not "   # noqa: TRY004
                         f"{type(params).__name__}")
    if not params or set(params) - set(FAN_FIELDS.values()):
        raise ValueError("CC2 fan params are one or more of fan / aux_fan / box_fan, each the "
                         f"value built by fan_params(); got {str(params)[:60]!r}")
    for key, value in params.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"CC2 fan param {key} must be a number from fan_params(), not "      # noqa: TRY004
                             f"{type(value).__name__}")
        if not 0 <= value <= WIRE_MAX:
            raise ValueError(f"CC2 fan param {key}={value} is outside the 0-{WIRE_MAX} the official "
                             "client's arithmetic produces")
    return dict(params)


def asked_percent(values: dict[str, Any]) -> dict:
    """The percents this call was given, keyed by the same field names as ``sent``, so a reply shows
    percent and wire number side by side instead of making a reader redo the arithmetic."""
    return {FAN_FIELDS[fan]: parse_percent(value, fan)
            for fan, value in values.items() if value is not None}


# --------------------------------------------------------------------------- the read-back


def _speed_or_none(value: Any) -> Any:
    """One fan's speed as it arrived: a number kept as it is (int or float, never a bool), anything
    else is no reading at all. No scaling, no rounding, no percent — the unit is what the module
    docstring refuses to claim."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def fan_raws(machine: st.Cc2MachineStatus) -> dict:
    """Each commanded fan's number as this 1002 reported it, or None for a fan the reply does not
    name. Read out of the kept raw reply on purpose: ``Cc2MachineStatus`` has no fan fields, and this
    is the module's one deliberate read of ``raw`` — the same way ``cc2_canvas`` bypasses a status
    reader whose job is to always answer, because a control's read-back must be allowed to say
    nothing."""
    fans = machine.raw.get("fans") if isinstance(machine.raw, dict) else None
    out: dict[str, Any] = {}
    for fan, field in FAN_FIELDS.items():
        block = fans.get(field) if isinstance(fans, dict) else None
        out[fan] = _speed_or_none(block.get("speed")) if isinstance(block, dict) else None
    return out


def _reading(machine: st.Cc2MachineStatus) -> dict:
    return {"fans_now_raw": fan_raws(machine), "readback_unit": READBACK_UNIT}


def _compare(before: dict, after: dict) -> dict:
    """Did each fan's number move? Raw against raw, so no unit is needed and none is implied: True
    and False are both real readings, and None means one of the two looks said nothing."""
    changed: dict[str, Any] = {}
    for fan in FANS:
        a, b = before.get(fan), after.get(fan)
        changed[fan] = None if a is None or b is None else bool(a != b)
    return changed


def _contradiction_note(sent: dict, before: dict, after: dict) -> str | None:
    """Said only when an accepted command left a commanded fan's number exactly where it was, and
    worded as what it is: two raw readings that did not move. It is not called a failure — the unit
    is unproven, so a firmware could legitimately report something else than the number we sent."""
    for fan, field in FAN_FIELDS.items():
        if field not in sent:
            continue
        if before.get(fan) is not None and after.get(fan) == before.get(fan):
            value = before[fan]
            note = CONTRADICT.format(fan=fan, value=value)[:job.NOTE_CAP]
            if len(note) == job.NOTE_CAP:
                return None          # never a cut mid-sentence; a truncated note says nothing
            return note
    return None


# --------------------------------------------------------------------------- the preview (read-only)


async def read_preview(transport: Any, values: dict[str, Any],
                       *, timeout_s: float | None = None) -> dict:
    """What this exact confirmed call would send, and what the printer says about its fans right now
    — the companion's ``confirm_required`` shape, the job controls' and ``cc2_peripherals``'. The
    read-only 1002 below is the only method this path ever sends; no 1030 leaves the wire without
    ``confirm=True``. The preview is where the scale argument is made visible rather than assumed: it
    shows the percent asked, the number it becomes, the formula that produced it, and that no CC2 has
    ever answered 1030 here. A status read that fails is raised rather than papered over — a preview
    built on nothing would be an invitation to confirm a guess."""
    params = fan_params(values)
    machine = await st.fetch_machine_status(transport, timeout_s=timeout_s)
    preview = {
        "will_send": {"method": METHOD_SET_FAN, "params": dict(params)},
        "asked_percent": asked_percent(values),
        "wire_formula": SCALE_NOW,
        "waits_up_to_s": SET_WAIT_S if timeout_s is None else timeout_s,
        **_reading(machine),
        "verified_on_this_printer": False,
        "unverified": UNVERIFIED,
    }
    return {"confirm_required": True, "action": SETTING, "preview": preview,
            "how": job.PREVIEW_HOW, "verified_on_this_printer": False, "unverified": UNVERIFIED}


# --------------------------------------------------------------------------- the one command


async def _observe_quietly(transport: Any, before: dict | None) -> tuple[dict | None, str | None]:
    """Exactly one read-only 1002, or the honest note that it did not land. The only follow-up a sent
    command may ever cause. ``before`` is the preview's own reading when the caller has one, so
    'moved' can mean moved-against-what-was-reported-rather-than-moved-since-the-moment-we-asked;
    with nothing to compare, every fan reads None rather than a confident False."""
    try:
        after = fan_raws(await st.fetch_machine_status(transport))
    except PrinterError as e:
        # Cc2DataError (a 1002 that cannot be read or was refused) is this same fact: no reading.
        why = getattr(e, "message", None) or str(e)
        return None, f"{job.OBSERVE_LOST_NOTE}{why[:job.NOTE_CAP]})"
    base = before if isinstance(before, dict) else {fan: None for fan in FANS}
    return {"raw": after, "changed": _compare(base, after), "readback_unit": READBACK_UNIT}, None


def _before_reading(before: Any) -> dict | None:
    """Accept a ``read_preview`` result, the same result's ``preview`` sub-dict, or nothing."""
    if not isinstance(before, dict):
        return None
    if "fans_now_raw" in before:
        return dict(before["fans_now_raw"])
    preview = before.get("preview")
    if isinstance(preview, dict) and "fans_now_raw" in preview:
        return dict(preview["fans_now_raw"])
    return None


async def set_fan(transport: Any, values: dict[str, Any], *,
                  before: Any = None, timeout_s: float | None = None) -> Cc2FanResult:
    """Send the one 1030 for ``values``, once, and report what came back — or report truthfully that
    nothing readable came back. ``values`` is the official UI's own input (percent 0-100 per named
    fan) and ``sent`` is the number that becomes; both ride in the reply. ``before`` may carry the
    preview's reading so the follow-up look can say whether a fan's number moved; omitting it leaves
    every fan's ``changed`` unknown rather than guessing. ``timeout_s`` overrides the sourced wait
    for tests. Nothing in this call is ever repeated, and the only extra read it may cause is the one
    1002 ``_observe_quietly`` takes after an accepted or unanswered command.

    The classification is the job controls' bar: an explicit integer ``error_code`` 0 is acceptance,
    any other integer is the printer declining on record (kept, and earning no follow-up read), and a
    missing, bool, float or text code is an ambiguity the caller resolves by reading state
    deliberately — never here, where a second command would be tempting. Acceptance is never
    restated as "the fan is now at X%", because the scale is the very thing on trial."""
    params = fan_params(values)
    asked = asked_percent(values)
    wait_s = SET_WAIT_S if timeout_s is None else timeout_s
    sent = dict(params)

    def base(status: ControlStatus, **over) -> dict:
        d = {"method": METHOD_SET_FAN, "sent": sent, "waited_s": wait_s, "status": status,
             "asked": asked}
        d.update(over)
        return d

    try:
        reply = await transport.request(METHOD_SET_FAN, sent, timeout_s=wait_s)
    except Cc2TimeoutError:
        notes = [job.LOST_NOTE.format(verb=SETTING, wait=wait_s)]
        observed, lost_note = await _observe_quietly(transport, _before_reading(before))
        if lost_note is not None:
            notes.append(lost_note)
        return Cc2FanResult(**base(ControlStatus.AMBIGUOUS, observed=observed,
                                   notes=tuple(notes)))
    except PrinterError as e:
        why = getattr(e, "message", None) or str(e)
        return Cc2FanResult(**base(ControlStatus.AMBIGUOUS,
                                   notes=(job.DEAD_NOTE.format(verb=SETTING) + why[:job.NOTE_CAP],)))

    if not isinstance(reply, dict) or "error_code" not in reply:
        detail = "carried no error code at all"
        return Cc2FanResult(**base(ControlStatus.AMBIGUOUS,
                                   notes=(job.UNREADABLE_NOTE.format(verb=SETTING, detail=detail),
                                          UNVERIFIED)))
    code = reply["error_code"]
    if not job.plain_int(code):
        detail = f"carried an error code that cannot be read ({type(code).__name__})"
        return Cc2FanResult(**base(ControlStatus.AMBIGUOUS,
                                   notes=(job.UNREADABLE_NOTE.format(verb=SETTING, detail=detail),
                                          UNVERIFIED)))
    if code != 0:
        return Cc2FanResult(**base(
            ControlStatus.REFUSED, error_code=code,
            notes=(job.refusal_note(SETTING, code), job.NO_OBSERVE_AFTER_REFUSAL)))

    observed, lost_note = await _observe_quietly(transport, _before_reading(before))
    notes = [job.ACCEPT_BASE.format(verb=SETTING),
             ("An accepted fan command proves the printer took the frame, nothing more: what its "
              "fan numbers mean on this firmware is still unproven, and that is what the live test "
              "is for.")]
    silence = None
    if observed is not None:
        if all(value is None for value in observed["raw"].values()):
            silence = OBSERVE_UNKNOWN_NOTE
        else:
            contra = _contradiction_note(sent, _before_reading(before) or {}, observed["raw"])
            if contra is not None:
                notes.append(contra)
    if silence is not None:
        notes.append(silence)
    if lost_note is not None:
        notes.append(lost_note)
    return Cc2FanResult(**base(ControlStatus.ACCEPTED, accepted=True, observed=observed,
                               notes=tuple(notes)))
