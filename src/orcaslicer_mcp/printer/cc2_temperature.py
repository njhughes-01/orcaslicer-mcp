"""The CC2 temperature setpoint control (method 1028 SET_TEMPERATURE) — one heater, one command at
most, reported with the same honesty the job controls and the peripheral controls already keep:
accepted (an explicit ``error_code`` 0), refused (an explicit nonzero code, kept) or ambiguous (no
answer, an unreadable one, or a dead session), and a lost answer never resent. The acceptance
vocabulary, the note wording and the decode bar are imported from ``cc2_control`` rather than
reworded, so a model reads one honesty out of every control tool on this server.

WHY THIS MODULE EXISTS NOW, AND WHAT IT WILL NOT INVENT. A temperature command needs a ceiling, and
when the light and speed controls were written the ceiling for this heater pair was not established,
so 1028 was left out on purpose (see ``cc2_peripherals``). What has since been read out of
Elegoo's own sources, machine-first-party, is this:

* **The wire shape is not in dispute.** Elegoo's LAN web client (``elegooofficial/elegoo-fdm-web``,
  release 20260625, ``lan_service_web.zip``) enumerates ``TemperatureControl = 1028`` and calls it
  two ways only — ``temperatureControl({extruder: v})`` for the nozzle row and
  ``temperatureControl({heater_bed: v})`` for the bed row — one heater per request, never both.
  Elegoo's LAN SDK (``elegooofficial/elegoo-link``@ec2d2269,
  ``elegoo_fdm_cc2_message_adapter.cpp``) maps the same call onto ``{"heater_bed": …}`` and
  ``{"extruder": …}``. The local protocol doc's table shows ``{"extruder": 220, "heater_bed": 60}``,
  i.e. both fields exist in one frame; no first-party sender puts both in one request, and one
  action per confirmation is this server's rule anyway, so this module commands one heater per call
  and names the other untouched. Values are degrees Celsius; both replies are read the same way as
  every other command's (``error_code`` 0 = the printer did not refuse).
* **The heater set is extruder and bed.** The same web client's ``device-temp`` component offers
  three rows: nozzle, heated bed and chamber — and the chamber row is ``editable: false``, while
  the official firmware config (``elegooofficial/CentauriCarbon2``@5a2ea7fc,
  ``debugfile/printer.cfg``) has ``[extruder]`` and ``[heater_bed]`` heaters and a
  ``[ztemperature_sensor box]`` sensor with no heater behind it. So chamber is a reading, not a
  control, and asking this tool for it refuses.
* **The range is the printer's own official control's, and it is not claimed to be a hardware
  rating.** The official control's own inputs are ``min: 0, max: 350`` for the nozzle and
  ``min: 0, max: 110`` for the heated bed, and its input handler clamps a typed number into that
  window before it sends. Those are the numbers this module enforces, and they are reported as what
  they are: the range Elegoo's own official control for this printer accepts. They are not
  presented as the hot end's or the bed's rated maximum, and the sources do not agree on one:
  the same Elegoo SDK's capability table records 300 for that extruder and 120 for that bed — but
  the five values of each row are bound against ``TemperatureComponent{name, controllable,
  supportsTemperatureReading, minTemperature, maxTemperature}``, so what actually lands there is
  ``supportsTemperatureReading = 0``, ``minTemperature = 300`` and ``maxTemperature = 1``: those
  lines contradict themselves and cannot be used as a limit. The firmware's own ceilings are a
  sensor envelope rather than a target — ``max_temp`` 600 for the extruder and 125 for the bed in
  the official config — and Elegoo's firmware (``elegoo/extras/heaters.cpp``) refuses a nonzero
  setpoint outside that window, so a setpoint above it is the printer's own "no", not this server's
  guess. The third-party Home Assistant integration clamps 0–320 / 0–110 and the maintainer's
  Klipper MCP companion fixes 300 / 110 for Klipper generally; neither is this printer's word.
  Nothing here is taken from a slicer profile either: the official Elegoo Centauri Carbon 2 profile
  bundle carries no temperature-ceiling key of any kind (verified across its machine and filament
  files), which is exactly why a profile's nozzle or bed number is a print setting and never a
  machine limit, and why no profile value is offered here as one for other filaments or nozzles.

So: the ceiling a call may ask for is the official control's own (nozzle 350, bed 110, floor 0 for
both), named in every reply with its source, and the disagreement above is said out loud whenever a
call asks for an extruder number above the 300 the SDK names. A ceiling is never found by sending
out-of-range values — the revised plan forbids it and this module makes it unnecessary. If a caller
wants a tighter bound than the machine's own control imposes, it asks for a lower temperature; there
is no parameter here that raises a ceiling, because that is what would be the guess.

The wait is the sources' own, as everywhere else in this lane: the LAN client's request layer
defaults to 5 s (``request(…, timeout = 5e3)``) and passes no override for this method, while the
integration waits 10 s (``CC2_COMMAND_TIMEOUT``) for every command. The longer is used, because the
short window is what turns a command the printer took into a report of an answer that never
arrived — and nothing is ever resent to catch it.

Read-back is separate from acceptance, in three places. The preview carries what the printer
reports for that heater right now (1002 ``extruder``/``heater_bed`` blocks, each a ``temperature``
plus a ``target``, the only shape this firmware is known to answer). A confirmed call takes exactly
one look afterwards, and reports both the target and the actual temperature it saw. A look that
contradicts an accepted command — the printer reporting a target that is not the number it just
accepted — is said out loud without ever earning a second command. And an accepted command whose
actual temperature is still climbing is never called a failure: heating takes time, and this reads
the printer once. An absent field stays unknown (None) and is never a zero.

No pre-check of state stands before a command: whether a setpoint makes sense right now (a nozzle
at 350 with no filament loaded, a bed at 110 with nothing on it) is a human's read of the preview,
the printer refuses what does not apply with its own code, and the state can change between one tool
call and the next anyway. The two-phase confirm is enforced at the tool layer (``server.py``) exactly
as it is for the job and peripheral controls: without ``confirm=True`` nothing but the read-only 1002
leaves the wire, and the confirmation carries the user's authorization, which their standing yes for
this print or session may already supply. Privacy: nothing here takes an address, a token or an
environment variable — the transport is the pinned session — and no note or result carries
credentials or raw replies."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import cc2_control as job
from . import cc2_state as st
from .cc2_control import ControlStatus   # the same three-way answer, one vocabulary
from .cc2_transport import Cc2TimeoutError, PrinterError

METHOD_SET_TEMPERATURE = 1028   # SET_TEMPERATURE — {"extruder": n} or {"heater_bed": n}

# The one wait, the two sources' own (see the module docstring): the LAN client's 5 s default for
# this method and the integration's 10 s for every command, and the longer is the one that cannot
# call a landed command lost while the printer was still answering.
SET_WAIT_S = 10.0

# The two heaters the protocol can command, with the words each is addressed by. The two keys per
# heater are the only spellings accepted because each comes from a source: the wire field
# (``extruder`` / ``heater_bed``, both senders) and the row labels a person reads (the official
# control's own nozzle row, and ``bed``, the word the maintainer's companion uses for the same
# heater). Chamber is deliberately absent — see the module docstring — and says so when asked.
_HEATERS: dict[str, str] = {"extruder": "extruder", "nozzle": "extruder",
                            "heater_bed": "heater_bed", "bed": "heater_bed"}

# What each heater is called in a sentence, and what its block in the 1002 status reply is read
# through (``cc2_state`` already parses both pairs as (actual, target)).
_WORDS: dict[str, str] = {"extruder": "extruder", "heater_bed": "bed"}

# The floor and the ceiling of what this printer's own official control accepts, per heater: the
# web client's own input bounds (nozzle 0-350, heated bed 0-110), clamped into that window before
# it sends. Reported with its source in every reply, and never described as a hardware rating.
FLOOR_C = 0.0
CEILING_C: dict[str, float] = {"extruder": 350.0, "heater_bed": 110.0}
CEILING_SOURCE = ("the range the CC2's own official LAN control accepts (Elegoo's LAN web client "
                  "release 20260625: nozzle 0-350, heated bed 0-110), not a hardware rating")

# The extruder number the other first-party source names. Above it the two Elegoo sources
# disagree, so the tool says so rather than choosing a side on a hunch (see the docstring for
# why the SDK's own row cannot be read as a limit either).
_SDK_EXTRUDER_C = 300.0

# How far a target the printer reports afterwards may sit from the number this call sent before it
# counts as a disagreement worth saying out loud. Half a degree is this module's stated rule for
# comparing two floats, not a fact about the printer, and it is named in the note it produces.
_TARGET_TOLERANCE_C = 0.5

# The human-facing sentence about where a bound came from, kept under the reply cap (NOTE_CAP).
_LIMIT_NOTE = ("Ceiling this call enforced: {ceiling:g}°C for the {word} — the bound the printer's "
               "own official LAN control accepts (Elegoo's LAN web client release 20260625: "
               "nozzle 0-350, bed 0-110). Not a hardware rating; the firmware's own max_temp "
               "refuses higher.")

_ABOVE_SDK_NOTE = (f"Above {_SDK_EXTRUDER_C:g}°C the two Elegoo sources disagree: the printer's own "
                   "web control reaches this high while Elegoo's LAN SDK records 300 for this "
                   "heater — a row that contradicts itself. Nothing here claims it is rated for "
                   "this; the preview and the human decide.")

_ZERO_NOTE = ("A target of 0 means the heater is off, not cold-ish — that is what this printer's "
              "own end-of-print G-code sends (M104 S0, M140 S0).")

_ACCEPT_TAIL = ("The observed temperatures are one separate read-only look and are never part of "
                "acceptance. Reaching a setpoint takes time: what the actual temperature says one "
                "look later is never evidence against an accepted command.")

# Said only when the look afterwards reports a target that is not the number just accepted. It is
# never an offer to send again.
_CONTRADICT = ("Note the disagreement: the printer accepted {asked:g}°C for the {word} and its own "
               "status read afterwards reports the target as {seen:g}°C (apart by more than "
               f"{_TARGET_TOLERANCE_C:g}°C). Accepted and observed are reported separately; "
               "cc2_status reads it again on demand.")


@dataclass(frozen=True)
class Cc2TempResult:
    """The whole outcome of one temperature command. ``heater`` is the wire field commanded
    (``"extruder"`` or ``"heater_bed"``) and ``sent`` the exact params that went out, so a caller can
    see what the printer was actually told. ``status`` says what the printer answered and
    ``accepted`` is true only for its explicit 0; ``error_code`` keeps a refusal's number.
    ``ceiling_c`` is the bound this call enforced and ``limit_basis`` names where that bound comes
    from, so no reader has to trust that a number was invented by the printer. The ``observed_*``
    fields (``observed_target_c``, ``observed_actual_c``, both for the heater commanded) come from
    one read-only 1002 taken after an accepted or unanswered command — never after a refusal on
    record, never on a dead session — and report what the printer says about itself now, not what
    the command hoped. An unknown is None and never a zero. ``notes`` carry what must be said out
    loud; ``as_dict()`` is the shape a tool reports."""
    heater: str
    method: int
    sent: dict
    status: ControlStatus
    waited_s: float
    ceiling_c: float
    accepted: bool = False
    error_code: int | None = None
    observed: dict | None = None
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "heater": self.heater,
            "method": self.method,
            "sent": dict(self.sent),
            "status": str(self.status),
            "accepted": self.accepted,
            "error_code": self.error_code,
            "waited_s": self.waited_s,
            "ceiling_c": self.ceiling_c,
            "limit_basis": CEILING_SOURCE,
            **(self.observed or {}),
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- the argument bar


def parse_heater(value: Any) -> str:
    """The heater as the protocol names it, from any spelling a source actually uses. Anything else
    refuses before a session opens, naming what is accepted — including ``chamber``, which the
    printer's own control shows read-only and which has no heater in the official firmware config,
    so an offer to set it would be a lie about a capability."""
    if isinstance(value, str):
        field = _HEATERS.get(value.strip().lower())
        if field is not None:
            return field
    said = str(value)[:40] if value is not None else "nothing"
    extra = ""
    if isinstance(value, str) and value.strip().lower() in {"chamber", "box", "enclosure"}:
        extra = (" The chamber is a reading only: its official control row is not editable and the "
                 "firmware config gives it a sensor, no heater.")
    raise PrinterError(
        "protocol_error",
        f"The heater must be named; {said!r} is not one this protocol can command. Nothing was "
        f"sent.{extra}",
        hint="Pass 'extruder' (the nozzle, also spelled 'nozzle') or 'heater_bed' (also 'bed') — "
             f"the only two heaters this protocol commands.{extra}",
        reason="heater_name")


def parse_target(value: Any, field: str) -> float:
    """A setpoint in degrees Celsius, exactly: a JSON number, or its decimal text because some
    clients send every argument as text. A bool is refused (True is an int in Python and is not a
    temperature), and so is anything that is not finite. Then the bound the machine's own control
    imposes: 0 to the heater's ceiling, nothing above it and nothing below zero, refused with the
    number and its source named and nothing sent. This is where a wild setpoint dies — not by
    finding out what the printer does with it."""
    if field not in CEILING_C:
        raise ValueError(f"unknown CC2 heater {field!r} — this module speaks only {list(_FIELDS)}")
    num: float | None = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        num = float(value)
    elif isinstance(value, str):
        try:
            num = float(value.strip())
        except ValueError:
            num = None
    if num is None or num != num or num in (float("inf"), float("-inf")):
        raise PrinterError(
            "protocol_error",
            f"The temperature must be a number of degrees Celsius; {str(value)[:40]!r} is not one. "
            "Nothing was sent.",
            hint="Pass the setpoint in °C as a plain number — 220 for the nozzle, 60 for the bed, "
                 "0 to turn a heater off. Not a percentage, not a filament name, not a preset.",
            reason="temp_value")
    if num < FLOOR_C or num > CEILING_C[field]:
        raise PrinterError(
            "protocol_error",
            f"{num:g}°C is outside the {FLOOR_C:g}-{CEILING_C[field]:g}°C the printer's own control "
            f"accepts for the {_WORDS[field]}: {CEILING_SOURCE}. Nothing was sent.",
            hint=f"Ask for a number between {FLOOR_C:g} and {CEILING_C[field]:g} for the "
                 f"{_WORDS[field]} (0 turns it off). This tool will not send anything above that, "
                 "and it cannot be raised from here: the ceiling comes from the machine's own "
                 "official control, not from a caller.",
            reason="temp_range")
    return num


def _wire_number(value: float) -> float | int:
    """The number as it goes on the wire: a whole setpoint goes out whole (220, not 220.0), because
    that is the shape both first-party senders use — Elegoo's web client hands over whatever was
    typed and the Home Assistant integration sends ``int(temperature)`` — and a genuine fraction
    (60.5) goes out as a fraction, which is what the status fields are documented as (float). One
    shape is never traded for the other by accident: this is the only coercion in the path, and it
    preserves the value exactly."""
    return int(value) if float(value).is_integer() else value


def temp_params(field: str, target: Any) -> dict:
    """The 1028 params: one heater field, one setpoint — the exact frame Elegoo's own control sends
    for that row. Never both heaters in one request (no first-party sender does that, and one
    confirmation here means one action), and never a second field."""
    return {field: _wire_number(parse_target(target, field))}


# The two frames this protocol is documented to mean, spelled out so a hand-rolled caller cannot
# reach the wire with a frame no first-party sender has ever put on it.
_FIELDS = ("extruder", "heater_bed")


def checked_params(field: str, params: Any) -> tuple[str, float]:
    """The second lock on the door the tools already hold: the one (heater, setpoint) a command may
    leave with, refused before the wire if it is not exactly the documented single-heater frame. The
    tools build theirs with ``temp_params``, so this never fires in normal use — it fires on a
    caller that hand-rolled a frame, and both-heaters-in-one-request or a setpoint of 5000 must
    never reach a printer because some layer above forgot to check."""
    if not isinstance(params, dict):
        raise ValueError(f"CC2 temperature params must be an object of the protocol's own fields, "
                         f"not {type(params).__name__}")
    if set(params) != {field}:
        raise ValueError(f"CC2 temperature params are exactly one field for one heater — "
                         f"{{'{field}': <°C>}}; got keys {sorted(str(k) for k in params)}")
    return field, _wire_number(parse_target(params[field], field))


# --------------------------------------------------------------------------- the reading of a heater


def temp_word(machine: st.Cc2MachineStatus, field: str) -> tuple[float | None, float | None]:
    """(the actual temperature, the target) for one heater exactly as the printer reports them,
    either None when it reported nothing readable. ``cc2_state`` already refuses to invent a
    temperature, so no reading is an unknown and never a zero — and the target that comes back is
    what the printer believes, not what this call asked for."""
    if field == "extruder":
        return machine.nozzle_temp, machine.nozzle_target
    if field == "heater_bed":
        return machine.bed_temp, machine.bed_target
    raise ValueError(f"unknown CC2 heater {field!r} — this module speaks only {list(_FIELDS)}")


def _observed_fields(machine: st.Cc2MachineStatus, field: str) -> dict:
    actual, target = temp_word(machine, field)
    return {"observed_target_c": target, "observed_actual_c": actual}


def _reading(machine: st.Cc2MachineStatus, field: str) -> dict:
    """The preview's view of what is true for this heater now, named apart from the command's own
    words so a caller can see that the preview state and the will-send value are two things."""
    actual, target = temp_word(machine, field)
    return {"actual_now_c": actual, "target_now_c": target,
            "heater_is_known": actual is not None or target is not None}


# --------------------------------------------------------------------------- the preview (read-only)


async def read_preview(transport: Any, field: str, params: dict,
                       *, timeout_s: float | None = None) -> dict:
    """What this exact confirmed call would send, the bound it enforced, and what the printer says
    about that heater right now — the maintainer companion's ``confirm_required`` shape, the same one
    the job and peripheral controls use. The 1002 status read below is the only method this path ever
    sends: no control method leaves the wire without ``confirm=True``, which the tests watch on the
    recorded wire. A status read that fails is raised to the tool rather than papered over — a
    preview built on nothing would be an invitation to confirm a guess."""
    field, target = checked_params(field, params)
    machine = await st.fetch_machine_status(transport, timeout_s=timeout_s)
    preview = {"will_send": {"method": METHOD_SET_TEMPERATURE, "params": {field: target}},
               "waits_up_to_s": SET_WAIT_S,
               "heater": field,
               "ceiling_c": CEILING_C[field],
               "limit_basis": CEILING_SOURCE,
               **_reading(machine, field)}
    notes = [_LIMIT_NOTE.format(ceiling=CEILING_C[field], word=_WORDS[field])]
    if field == "extruder" and target > _SDK_EXTRUDER_C:
        notes.append(_ABOVE_SDK_NOTE)
    if target == 0:
        notes.append(_ZERO_NOTE)
    return {"confirm_required": True, "action": f"{_WORDS[field]}_temperature",
            "preview": preview, "notes": notes, "how": job.PREVIEW_HOW}


# --------------------------------------------------------------------------- the one command


async def _observe_quietly(transport: Any, field: str) -> tuple[dict | None, str | None]:
    """Exactly one read-only 1002, or the honest note that it did not land. The only follow-up a
    sent command may ever cause; the command itself is never reconsidered."""
    try:
        return _observed_fields(await st.fetch_machine_status(transport), field), None
    except PrinterError as e:
        why = getattr(e, "message", None) or str(e)
        return None, f"{job.OBSERVE_LOST_NOTE}{why})"


def _contradiction_note(field: str, asked: float, observed: dict | None) -> str | None:
    """The one note that may say the printer accepted a setpoint its own status does not show — said
    only when the look actually contradicts the ask, and never as a proposal to send again."""
    if not observed:
        return None
    seen = observed.get("observed_target_c")
    if seen is None or abs(seen - asked) <= _TARGET_TOLERANCE_C:
        return None
    return _CONTRADICT.format(asked=asked, seen=seen, word=_WORDS[field])


async def set_temperature(transport: Any, field: str, params: dict,
                          *, timeout_s: float | None = None) -> Cc2TempResult:
    """Send the one 1028 command for ``field`` with ``params``, once, and report what came back — or
    report, truthfully, that nothing readable came back. ``timeout_s`` overrides the sourced wait for
    tests; nothing in this call is ever repeated, and the only extra read it may cause is the one 1002
    ``_observe_quietly`` takes after an accepted or unanswered command.

    The classification is the job controls' bar, applied to a heater write: an explicit integer
    ``error_code`` 0 is acceptance, any other integer is the printer declining on record (kept, and
    earning no follow-up read), and a missing, bool, float or text code is an ambiguity the caller
    resolves by reading state deliberately — never here, where a second command would be tempting.
    Where an accepted command and the following look disagree about the target, the disagreement is
    said out loud without ever doubting the acceptance; where the actual temperature is simply still
    climbing, nothing is said about it at all, because one look cannot tell slow from refused."""
    field, target = checked_params(field, params)
    word = _WORDS[field]
    verb = f"{word} temperature"
    wait_s = SET_WAIT_S if timeout_s is None else timeout_s
    sent = {field: target}

    def base(status: ControlStatus, **over) -> dict:
        d = {"heater": field, "method": METHOD_SET_TEMPERATURE, "sent": sent, "waited_s": wait_s,
             "ceiling_c": CEILING_C[field], "status": status}
        d.update(over)
        return d

    try:
        reply = await transport.request(METHOD_SET_TEMPERATURE, sent, timeout_s=wait_s)
    except Cc2TimeoutError:
        notes = [job.LOST_NOTE.format(verb=verb, wait=wait_s)]
        observed, lost_note = await _observe_quietly(transport, field)
        if lost_note is not None:
            notes.append(lost_note)
        return Cc2TempResult(**base(ControlStatus.AMBIGUOUS, observed=observed,
                                    notes=tuple(notes)))
    except PrinterError as e:
        why = getattr(e, "message", None) or str(e)
        return Cc2TempResult(**base(ControlStatus.AMBIGUOUS,
                                    notes=(job.DEAD_NOTE.format(verb=verb) + why,)))

    # ``error_code``: only an explicit integer answers. No code at all, or one that cannot be read,
    # is an ambiguity the caller resolves by reading state deliberately — never here, where a second
    # command would be tempting (the same bar cc2_start and cc2_control hold their answers to).
    if not isinstance(reply, dict) or "error_code" not in reply:
        detail = "carried no error code at all"
        return Cc2TempResult(**base(ControlStatus.AMBIGUOUS,
                                    notes=(job.UNREADABLE_NOTE.format(verb=verb, detail=detail),)))
    code = reply["error_code"]
    if not job.plain_int(code):
        detail = f"carried an error code that cannot be read ({type(code).__name__})"
        return Cc2TempResult(**base(ControlStatus.AMBIGUOUS,
                                    notes=(job.UNREADABLE_NOTE.format(verb=verb, detail=detail),)))
    if code != 0:
        return Cc2TempResult(**base(
            ControlStatus.REFUSED, error_code=code,
            notes=(job.refusal_note(verb, code), job.NO_OBSERVE_AFTER_REFUSAL)))

    observed, lost_note = await _observe_quietly(transport, field)
    notes = [job.ACCEPT_BASE.format(verb=verb), _ACCEPT_TAIL,
             _LIMIT_NOTE.format(ceiling=CEILING_C[field], word=word)]
    if field == "extruder" and target > _SDK_EXTRUDER_C:
        notes.append(_ABOVE_SDK_NOTE)
    if target == 0:
        notes.append(_ZERO_NOTE)
    contra = _contradiction_note(field, target, observed)
    if contra is not None:
        notes.append(contra)
    if lost_note is not None:
        notes.append(lost_note)
    return Cc2TempResult(**base(ControlStatus.ACCEPTED, accepted=True, observed=observed,
                                notes=tuple(notes)))
