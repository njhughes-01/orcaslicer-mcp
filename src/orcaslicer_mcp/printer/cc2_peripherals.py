"""The two CC2 peripheral controls whose wire shape and read-back the printer's own sources agree
on — the overhead light (method 1029) and the print speed mode (method 1031) — with the same honesty
contract the job controls already keep: one command at most, reported as accepted (an explicit
``error_code`` 0), refused (an explicit nonzero code, kept) or ambiguous (no answer, an unreadable
one, or a dead session), and a lost answer never resent. The acceptance vocabulary, the note wording
and the decode bar are the job controls' own (``cc2_control``), imported rather than reworded, so a
model reads one honesty out of every control tool on this server.

What the sources actually say, because these two commands are the two places where the CC2's own
documentation and its own web client disagree:

* **1029 SET_LIGHT.** The protocol doc's methods table spells the parameter ``brightness``
  (``{"brightness": 255}``), and the same doc's implementation-findings addendum corrects it from
  observation: the official web interface sends ``{"power": 1}``, with ``power`` documented
  0 = off, 1 = on, and ``brightness`` noted as possibly working "on some firmware versions". The
  LAN web client bundled on the printer confirms the addendum — its light call is literally
  ``{method: LightSwitch, params: {power: on ? 1 : 0}}``, ``power`` alone. This module sends both
  spellings, paired so they can never contradict each other (``power`` 1 with ``brightness`` 255,
  ``power`` 0 with ``brightness`` 0), which is what the Home Assistant integration does and states
  in its own comment: some firmware expects one, some the other. Binary on/off only — no source
  shows this light dimming (the integration says the same: it "assumes binary on/off only; dimming
  needs a separate field"). Read-back is the 1002 ``led.status``, documented "0=off, 1=on (may also
  be brightness 0-255)"; the integration reads above-zero as on and so does this module, and an
  absent or unreadable field is unknown, never off.
* **1031 SET_PRINT_SPEED.** A discrete MODE, never a percentage. The doc's table spells the
  parameter ``mode`` and its Speed Modes table gives 0 Silent/50%, 1 Balanced/100% (the default),
  2 Sport/150%, 3 Ludicrous/200%; the LAN client's own enum is Silent=0, Balanced=1, Sport=2,
  Frenzy=3 (its English label for 3 reads "Ludicrous") and it sends ``params: {mode: <n>}``
  straight through. The Home Assistant integration reaches a mode only by bucketing a percentage
  (its own 75/125/175 thresholds) to feed a percentage-shaped control — an HA convenience, not the
  protocol, and this module does no such bucketing: it takes the printer's own mode number and
  sends that. Read-back is the 1002 ``gcode_move_inf.speed_mode`` (documented int 0-3, produced by
  the middleware from Klipper's ``speed_factor``). The integration defaults an absent speed_mode to
  1; inventing a state is not reporting one, so an absent or unreadable value is unknown here.

Two peripherals are deliberately NOT here. **1030 SET_FAN_SPEED**: the doc's table shows 0-255 per
fan, the integration converts a percentage into 0-255, and the official UI's scale is not
established — the revised plan records the two first-party sources disagreeing and says verify and
restore before adding that control, so this server does not guess a scale. **1028
SET_TEMPERATURE** is no longer one of them: it now has its own module (``cc2_temperature``), because
what it needed was a defensible ceiling rather than a new command, and one has since been read out of
Elegoo's own sources for this machine (its official LAN control's own input bounds, with the
firmware's refusal window and the SDK's self-contradictory capability row beside it). This module
still cannot send either method, and a test holds that.

The wait is the two sources' own, again not invented: the LAN client's request layer defaults to a
5 s window (``request(method, params, timeout = 5e3)``) and passes no override for either of these
methods, while the Home Assistant integration waits 10 s (its ``CC2_COMMAND_TIMEOUT``) for every
command it sends. This takes the longer of the two, because the short window is what turns a command
the printer took into a report of an answer that never arrived, and nothing is ever resent to catch
it. Unlike a resume, neither command is motion, so 10 s is generous rather than tight.

No pre-check of state stands before either command: whether a light or a speed change makes sense
right now is the human's read of the preview, the printer refuses what does not apply with its own
code, and the state can change between one tool call and the next anyway. The two-phase confirm is
enforced at the tool layer (``server.py``) exactly as it is for the job controls: without
``confirm=True`` nothing but the read-only 1002 leaves the wire, and the confirmation carries the
user's authorization, which their standing yes for this print or session may already supply.
Privacy: nothing here takes an address, a token or an environment variable — the transport is the
pinned session — and no note or result carries credentials or raw replies."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import cc2_control as job
from . import cc2_state as st
from .cc2_control import ControlStatus   # the same three-way answer, one vocabulary
from .cc2_transport import Cc2TimeoutError, PrinterError

METHOD_SET_LIGHT = 1029   # SET_LIGHT — the web client's power 0/1, with brightness paired for it
METHOD_SET_SPEED = 1031   # SET_PRINT_SPEED — {"mode": 0..3}, a discrete mode

# setting -> the one method it sends. The whole vocabulary this module speaks; anything else is a
# programmer error, raised before a request exists.
_METHODS: dict[str, int] = {"light": METHOD_SET_LIGHT, "speed": METHOD_SET_SPEED}

# Both waits are the sources' own (see the module docstring): the LAN client waits 5 s for these two
# methods and the integration waits 10 s for every command, and the longer is the one that cannot
# call a landed command lost while the printer was still answering.
SET_WAIT_S: dict[str, float] = {"light": 10.0, "speed": 10.0}

# The protocol doc's Speed Modes table. The names ride along because a person reads "Sport" and the
# printer hears 2; the multiplier is the doc's own word for what a mode means, reported as such and
# measured by nothing here.
SPEED_MODES: dict[int, tuple[str, int]] = {
    0: ("Silent", 50),
    1: ("Balanced", 100),
    2: ("Sport", 150),
    3: ("Ludicrous", 200),
}

# Bounded by the direct tools' reply cap in server.py, like the job controls' notes (NOTE_CAP).
_ACCEPT_TAILS = {
    "light": ("The observed light is one separate read-only look (led.status above zero reads as "
              "on) and is never part of acceptance. The protocol documents binary on/off; some "
              "firmware reports a 0-255 brightness there."),
    "speed": ("The observed mode is one separate read-only look and is never part of acceptance. "
              "Mode is discrete: 0 Silent/50%, 1 Balanced/100%, 2 Sport/150%, 3 Ludicrous/200%."),
}

# Said only when the read afterwards actually contradicts what was asked for: acceptance is what the
# printer answered the command, the observation is what its status says after it, and neither one
# overwrites the other. It is never an offer to send again.
_CONTRADICT = {
    "light": ("Note the disagreement: the printer accepted the light command and its own status "
              "read afterwards reports the light {word}. Accepted and observed are reported "
              "separately and neither overwrites the other; cc2_status reads it again on demand."),
    "speed": ("Note the disagreement: the printer accepted mode {asked} and its own status read "
              "afterwards reports mode {seen}. Accepted and observed are reported separately and "
              "neither overwrites the other; cc2_status reads it again on demand."),
}


@dataclass(frozen=True)
class Cc2SettingResult:
    """The whole outcome of one peripheral command. ``status`` says what the printer answered and
    ``accepted`` is true only for its explicit 0; ``error_code`` keeps a refusal's number; ``sent``
    is the exact params that went out, so a caller can see what the printer was actually told. The
    ``observed_*`` fields (``observed_light_on``/``observed_led_status``, or
    ``observed_speed_mode``/``observed_speed_name``) are one read-only 1002 taken after an accepted
    or unanswered command — never after a refusal on record, never on a dead session — and they
    report what the printer says about itself now, not what the command hoped. An unknown is None
    and never a guess of either value. ``notes`` carry what must be said out loud; ``as_dict()`` is
    the shape a tool reports."""
    setting: str
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
            "setting": self.setting,
            "method": self.method,
            "sent": dict(self.sent),
            "status": str(self.status),
            "accepted": self.accepted,
            "error_code": self.error_code,
            "waited_s": self.waited_s,
            **(self.observed or {}),
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- the argument bar


def parse_mode(value: Any) -> int:
    """The speed mode exactly as the printer's table numbers it: a whole number 0..3. Whole-number
    text ("2") reads, because some clients send every argument as text; a bool does not (True is an
    int in Python and is not a mode), and neither does a percentage, a float, a name, an
    out-of-range number or any other spelling. Refused before the printer is asked, with the four
    modes named, because guessing a mode would silently change how a print runs."""
    mode: int | None = None
    if isinstance(value, int) and not isinstance(value, bool):
        mode = value
    elif isinstance(value, str) and value.strip().isdigit():
        mode = int(value.strip())
    if mode not in SPEED_MODES:
        raise PrinterError(
            "protocol_error",
            f"Print speed mode must be one of the printer's four modes; {str(value)[:40]!r} is "
            "not one of them. Nothing was sent.",
            hint="Pass the printer's own mode number: 0 Silent (50%), 1 Balanced (100%, the "
                 "default), 2 Sport (150%), 3 Ludicrous (200%). It is a discrete mode, never a "
                 "percentage — a speed is not spelled in percent on this protocol.",
            reason="mode_range")
    return mode


def parse_light(value: Any) -> bool:
    """The light argument exactly: the booleans True and False, nothing else — not "on", not 1, not
    a brightness. A value that merely looks like yes must never be what decides between the light
    on and the light off, so this refuses: the same exact-or-refusal rule ``bed_clear`` holds to."""
    if value is True or value is False:
        return value
    raise PrinterError(
        "protocol_error",
        f"The light takes a plain true or false; {str(value)[:40]!r} is neither. Nothing was sent.",
        hint="Pass true for the light on, false for the light off. The CC2's own web client spells "
             "the command as power 1 and power 0 and this tool sends exactly that; the light does "
             "not dim.",
        reason="light_argument")


def light_params(on: bool) -> dict:
    """The 1029 params: the web client's ``power`` (1/0) paired with the doc table's ``brightness``
    (255/0), because the protocol doc records firmware reading either spelling and the integration
    sends both for that reason. They are paired so one can never contradict the other, and they are
    binary: no source offers this light a dimmed level."""
    return {"power": 1 if on else 0, "brightness": 255 if on else 0}


def speed_params(mode: int) -> dict:
    """The 1031 params, exactly the one field the protocol names. No percentage, no fan, no
    acceleration: the LAN client sends ``{mode: n}`` and so does this."""
    return {"mode": parse_mode(mode)}


# The only two 1029 frames this protocol is documented to mean, spelled out so a caller cannot send
# a half-pair (power 1 with brightness 0 is a contradiction waiting for whichever field its firmware
# reads second).
_LIGHT_FRAMES = ({"power": 1, "brightness": 255}, {"power": 0, "brightness": 0})


def checked_params(setting: str, params: Any) -> dict:
    """The second lock on the same door the tools already hold: the params a peripheral command may
    leave with, refused before the wire if they are not exactly the documented frame for that
    setting. The tools build theirs with ``light_params``/``speed_params``, so this never fires in
    normal use — it fires on a caller that hand-rolled a frame, and a half-paired light command or
    a mode of 47 must never reach a printer because some layer above forgot to check."""
    if not isinstance(params, dict):
        raise ValueError(f"CC2 {setting} params must be an object of the protocol's own fields, "
                         f"not {type(params).__name__}")
    if setting == "light":
        if params in _LIGHT_FRAMES:
            return dict(params)
        raise ValueError("CC2 light params are exactly the paired frame from light_params() — "
                         f"{{'power': 1, 'brightness': 255}} or {{'power': 0, 'brightness': 0}}; "
                         f"got {str(params)[:60]!r}")
    return speed_params(params.get("mode"))


# --------------------------------------------------------------------------- the two readings


def light_word(machine: st.Cc2MachineStatus) -> tuple[bool | None, int | None]:
    """(on-or-unknown, the raw ``led.status`` as it arrived). Above zero is on and zero is off — the
    reading the doc's "0=off, 1=on (may also be brightness 0-255)" note and the integration's
    ``> 0`` agree on. A missing or unreadable field is (None, None): no reading is never off. A
    negative number is not a light level either, so it is unknown with the number kept."""
    raw = machine.led_status
    if raw is None:
        return None, None
    if raw < 0:
        return None, raw
    return raw > 0, raw


def speed_word(machine: st.Cc2MachineStatus) -> tuple[int | None, str | None]:
    """(the mode as the printer reported it, or None when it reported nothing readable; its name,
    or None when the number is not one of the four). A number outside 0..3 is reported as the
    number it is, with no name — it is never rounded into a neighbouring mode, and an absent field
    is never the integration's default of 1."""
    raw = machine.speed_mode
    if raw is None:
        return None, None
    named = SPEED_MODES.get(raw)
    return raw, (named[0] if named else None)


def _observed_fields(machine: st.Cc2MachineStatus, setting: str) -> dict:
    if setting == "light":
        on, raw = light_word(machine)
        return {"observed_light_on": on, "observed_led_status": raw}
    mode, name = speed_word(machine)
    return {"observed_speed_mode": mode, "observed_speed_name": name}


def _reading(machine: st.Cc2MachineStatus, setting: str) -> dict:
    """The preview's view of what is true now, named apart from the command's own words so a caller
    can see that the preview state and the will-send value are two different things."""
    if setting == "light":
        on, raw = light_word(machine)
        return {"light_on_now": on, "led_status_now": raw, "light_is_known": on is not None}
    mode, name = speed_word(machine)
    return {"mode_now": mode, "mode_name_now": name, "mode_is_known": mode is not None}


# --------------------------------------------------------------------------- the preview (read-only)


async def read_preview(transport: Any, setting: str, params: dict,
                       *, timeout_s: float | None = None) -> dict:
    """What this exact confirmed call would send, and what the printer says about that setting right
    now — the maintainer companion's ``confirm_required`` shape, the same one the job controls use.
    The 1002 status read below is the only method this path ever sends: no control method leaves the
    wire without ``confirm=True``, which the tests watch on the recorded wire. A status read that
    fails is raised to the tool rather than papered over — a preview built on nothing would be an
    invitation to confirm a guess."""
    if setting not in _METHODS:
        raise ValueError(f"unknown CC2 peripheral setting {setting!r} — this module speaks only "
                         f"{sorted(_METHODS)}")
    params = checked_params(setting, params)
    machine = await st.fetch_machine_status(transport, timeout_s=timeout_s)
    preview = {"will_send": {"method": _METHODS[setting], "params": dict(params)},
               "waits_up_to_s": SET_WAIT_S[setting],
               **_reading(machine, setting)}
    return {"confirm_required": True, "action": setting, "preview": preview, "how": job.PREVIEW_HOW}


# --------------------------------------------------------------------------- the one command


async def _observe_quietly(transport: Any, setting: str) -> tuple[dict | None, str | None]:
    """Exactly one read-only 1002, or the honest note that it did not land. The only follow-up a
    sent command may ever cause; the command itself is never reconsidered."""
    try:
        return _observed_fields(await st.fetch_machine_status(transport), setting), None
    except PrinterError as e:
        why = getattr(e, "message", None) or str(e)
        return None, f"{job.OBSERVE_LOST_NOTE}{why})"


def _contradiction_note(setting: str, sent: dict, observed: dict | None) -> str | None:
    """The one note that may say the printer accepted a command its own status does not show — said
    only when the look actually contradicts the ask, and never as a proposal to send again: the
    command went, the printer did not refuse it, and this module sends nothing a second time."""
    if not observed:
        return None
    if setting == "light":
        asked_on = bool(sent.get("power"))
        seen = observed.get("observed_light_on")
        if seen is None or seen == asked_on:
            return None
        return _CONTRADICT["light"].format(word="off" if not seen else "on")
    asked = sent.get("mode")
    seen = observed.get("observed_speed_mode")
    if asked is None or seen is None or seen == asked:
        return None
    return _CONTRADICT["speed"].format(asked=asked, seen=seen)


async def set_peripheral(transport: Any, setting: str, params: dict,
                         *, timeout_s: float | None = None) -> Cc2SettingResult:
    """Send the one peripheral method for ``setting`` with ``params``, once, and report what came
    back — or report, truthfully, that nothing readable came back. ``timeout_s`` overrides the
    documented wait for tests; nothing in this call is ever repeated, and the only extra read it may
    cause is the one 1002 ``_observe_quietly`` takes after an accepted or unanswered command.

    The classification is the job controls' bar, applied to a peripheral write: an explicit integer
    ``error_code`` 0 is acceptance, any other integer is the printer declining on record (kept, and
    earning no follow-up read), and a missing, bool, float or text code is an ambiguity the caller
    resolves by reading state deliberately — never here, where a second command would be tempting.
    Where an accepted command and the following look disagree, the disagreement is said out loud in
    its own note without ever doubting the acceptance."""
    if setting not in _METHODS:
        raise ValueError(f"unknown CC2 peripheral setting {setting!r} — this module speaks only "
                         f"{sorted(_METHODS)}")
    params = checked_params(setting, params)
    method = _METHODS[setting]
    wait_s = SET_WAIT_S[setting] if timeout_s is None else timeout_s
    sent = dict(params)

    def base(status: ControlStatus, **over) -> dict:
        d = {"setting": setting, "method": method, "sent": sent, "waited_s": wait_s,
             "status": status}
        d.update(over)
        return d

    try:
        reply = await transport.request(method, sent, timeout_s=wait_s)
    except Cc2TimeoutError:
        notes = [job.LOST_NOTE.format(verb=setting, wait=wait_s)]
        observed, lost_note = await _observe_quietly(transport, setting)
        if lost_note is not None:
            notes.append(lost_note)
        return Cc2SettingResult(**base(ControlStatus.AMBIGUOUS, observed=observed,
                                       notes=tuple(notes)))
    except PrinterError as e:
        why = getattr(e, "message", None) or str(e)
        return Cc2SettingResult(**base(ControlStatus.AMBIGUOUS,
                                       notes=(job.DEAD_NOTE.format(verb=setting) + why,)))

    # ``error_code``: only an explicit integer answers. No code at all, or one that cannot be read,
    # is an ambiguity the caller resolves by reading state deliberately — never here, where it would
    # tempt a second command (the same bar cc2_start and cc2_control hold the 1020/1021 answers to).
    if not isinstance(reply, dict) or "error_code" not in reply:
        detail = "carried no error code at all"
        return Cc2SettingResult(**base(ControlStatus.AMBIGUOUS,
                                       notes=(job.UNREADABLE_NOTE.format(verb=setting,
                                                                         detail=detail),)))
    code = reply["error_code"]
    if not job.plain_int(code):
        detail = f"carried an error code that cannot be read ({type(code).__name__})"
        return Cc2SettingResult(**base(ControlStatus.AMBIGUOUS,
                                       notes=(job.UNREADABLE_NOTE.format(verb=setting,
                                                                         detail=detail),)))
    if code != 0:
        return Cc2SettingResult(**base(
            ControlStatus.REFUSED, error_code=code,
            notes=(job.refusal_note(setting, code), job.NO_OBSERVE_AFTER_REFUSAL)))

    observed, lost_note = await _observe_quietly(transport, setting)
    notes = [job.ACCEPT_BASE.format(verb=setting), _ACCEPT_TAILS[setting]]
    contra = _contradiction_note(setting, sent, observed)
    if contra is not None:
        notes.append(contra)
    if lost_note is not None:
        notes.append(lost_note)
    return Cc2SettingResult(**base(ControlStatus.ACCEPTED, accepted=True, observed=observed,
                                   notes=tuple(notes)))
