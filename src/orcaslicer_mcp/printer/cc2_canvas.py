"""The CANVAS auto-refill switch — method 2004, one boolean, read back from the printer's own CANVAS
reply (2005 ``canvas_info.auto_refill``) — with the honesty contract every other CC2 control on this
server already keeps and its vocabulary imported rather than reworded (``cc2_control``): accepted (an
explicit ``error_code`` 0), refused (an explicit nonzero code, kept) or ambiguous (no answer, an
unreadable one, or a dead session), and a lost answer is never a refusal and never resent.

**This command has never been offered to a CC2 by anyone on this project, and the reply says so.**
Every successful reply carries ``verified_on_this_printer: false`` beside a sentence naming why: what
is established here is what Elegoo's own software puts on the wire, not what this firmware does with
it. A refusal from this tool is therefore evidence about the firmware, and a real print with a tray
running dry is the only thing that will ever show what the flag does.

What the sources actually say, and where they disagree:

* **Elegoo's own LAN web client, the bundle the printer serves itself,** defines ``SetAutoFillFilament
  = 2004`` and ``GetCanvasInfo = 2005`` in its own method enum, builds the command as
  ``{method: SetAutoFillFilament, params: {auto_refill: <the toggle's boolean>}}``, treats a nonzero
  ``error_code`` as a failure it shows as "Failed to set auto refill.", and paints the very same
  toggle from ``canvas_info.auto_refill`` of the 2005 reply. So the parameter name the printer's own
  client sends and the field the printer itself answers are one name, ``auto_refill``, and this
  module sends exactly that one field. Its request layer passes no timeout of its own for this
  method, so that layer's 5 s default applies; the Home Assistant integration waits 10 s for every
  command it sends, and the longer of the two is what this waits, because a short window is what
  turns a command the printer took into a report of an answer that never arrived — and nothing is
  ever resent to catch one.
* **The older integration protocol manual is not followed here, on purpose.** It spells this command
  ``{"enable": true}``, both in its System Methods table and in its own worked example, and the CC2
  integration defines the 2004 constant and never sends it, so that spelling has never been proven
  against a machine either. It is the only source for ``enable``, and against it stand the printer's
  own client and that same manual's own 2005 sample, which answers ``auto_refill``. The manual's 1047
  row makes the same class of error (its table says ``filename`` where Elegoo's client sends
  ``file_path``), which is the general reason a table row is not treated as proof about this
  firmware. So ``enable`` never leaves this module, in any combination, alongside ``auto_refill`` or
  on its own: sending both spellings "for compatibility", the way the light (1029) does, would be
  offering this printer an undocumented field no client of theirs has ever sent.

What this command does NOT touch is the point of the shape: one field, ``auto_refill``, and nothing
else, because 2004 sits next to 2003 (which writes a tray's filament info) and the 2005 reply carries
``active_canvas_id``/``active_tray_id`` besides it. Nothing here reads, writes or echoes either, so a
call to change the auto-refill flag cannot move which tray the printer is on, rename a spool, or
change a slot map; it does not send 2003, 2005 as a command, or any file, job or heater method, and
tests hold that on the recorded wire.

The read-back is deliberately not ``cc2_state``'s canvas reader. That reader is a status line, and a
status line has to answer, so it defaults an absent ``auto_refill`` to false; for a control's
read-back, an absent field is no reading at all, so this module reads the field itself: true/false
read as themselves, the integers 0 and 1 read as the numbers they are (the integration's own CANVAS
model records another Elegoo machine answering int 1 where the client expects a boolean), and
anything else — including a field that did not arrive and a 2005 that was refused or never answered —
is unknown with whatever did arrive kept beside it.

Unlike the light and the speed mode, a CANVAS read that will not answer does **not** block the
preview. That is a difference with a reason: a printer with no CANVAS attached is a legal state for
this setting (the flag is a property of the printer), and refusing a preview — and therefore the
change — because one read gave nothing would be a CC2-specific barrier the plan forbids. The preview
says what it could not read and goes ahead and shows what a confirmed call would send.

No pre-check of state stands before the command: whether auto-refill should be on right now is the
human's read of the preview, the printer refuses what it will not do with its own code, and the state
can change between one tool call and the next anyway. The two-phase confirm is enforced at the tool
layer (``server.py``) exactly as it is for the job and peripheral controls: without ``confirm=True``
nothing but the read-only 2005 leaves the wire. That confirmation is not an access control either —
it carries the user's authorization for this action, which their standing yes for this print or
session may already supply. Privacy: nothing here takes an address, a token or an environment
variable — the transport is the pinned session — and no note or result carries credentials or raw
replies. Every note is written inside the direct tools' reply cap (``cc2_control.NOTE_CAP``), because
a promise truncated mid-sentence is how an honesty note loses its tail."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import cc2_control as job
from . import cc2_state as st
from .cc2_control import ControlStatus   # the same three-way answer, one vocabulary
from .cc2_transport import Cc2TimeoutError, PrinterError

METHOD_SET_AUTO_REFILL = 2004   # SetAutoFillFilament — {"auto_refill": bool}, that field alone

SETTING = "auto_refill"         # the one word this module is asked to change, and the field it sends

# The longer of the two first-party windows (see the module docstring): the LAN client's 5 s default
# for a method that passes no override, and the integration's 10 s for every command.
SET_WAIT_S = 10.0

# Said in every reply, so the candidate label is a field a model reads rather than a docstring it
# may not have loaded. It is inside the reply cap on purpose: the sanitiser caps strings at exactly
# NOTE_CAP characters, and a caveat cut in half is the half that gets trusted.
UNVERIFIED = ("Method 2004 changed and restored the setting on one CC2. Automatic refill during "
              "a print remains unverified; a refusal is evidence about the printer's firmware, "
              "not a broken printer.")

_ACCEPT_TAIL = ("What the flag does at the printer when a CANVAS tray runs out has never been "
                "observed on this machine. The observed value is one separate read-only CANVAS look "
                "and is never part of acceptance.")

# Said only when the read afterwards actually contradicts what was asked for: acceptance is what the
# printer answered the command, the observation is what its CANVAS reply says after it, and neither
# one overwrites the other. It is never an offer to send again.
CONTRADICT = ("Note the disagreement: the printer accepted auto-refill {asked} and its own CANVAS "
              "read afterwards reports it {seen}. Accepted and observed are reported separately and "
              "neither overwrites the other; cc2_status reads it again on demand.")

# The one note said when the read afterwards produced no reading, and the reason as its own note so
# this promise is never the thing that gets truncated for it.
OBSERVE_UNKNOWN_NOTE = ("The one read-only CANVAS look (method 2005) this call allows itself gave no "
                        "auto-refill reading: it may have been refused, answered in a shape this "
                        "adapter cannot read, or have no CANVAS behind it. The flag stays unknown.")
OBSERVE_WHY = "Why the CANVAS look gave nothing: {why}"


@dataclass(frozen=True)
class Cc2RefillResult:
    """The whole outcome of one auto-refill command. ``status`` says what the printer answered the
    2004 and ``accepted`` is true only for its explicit 0; ``error_code`` keeps a refusal's number;
    ``sent`` is the exact params that left, so a caller can see the printer was told one field and
    nothing else. ``observed_auto_refill`` is the flag as the one read-only 2005 taken afterwards
    reports it — never after a refusal on record, never on a dead session — with the raw value it
    arrived in beside it, and it is None (unknown) whenever that look produced no reading, which is
    never read as off. ``verified_on_this_printer`` is False and stays False until a real print says
    otherwise; ``unverified`` is the sentence that explains it. ``notes`` carry what must be said out
    loud and ``as_dict()`` is the shape a tool reports."""
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
        seen = self.observed or {}
        return {
            "setting": SETTING, "method": self.method, "sent": dict(self.sent),
            "status": str(self.status), "accepted": self.accepted, "error_code": self.error_code,
            "waited_s": self.waited_s,
            "observed_auto_refill": seen.get("observed_auto_refill"),
            "observed_auto_refill_raw": seen.get("observed_auto_refill_raw"),
            "verified_on_this_printer": self.verified_on_this_printer, "unverified": UNVERIFIED,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- the argument bar


def parse_refill(value: Any) -> bool:
    """The argument exactly: the booleans True and False, nothing else — not "on", not 1, not a
    number that looks like yes. The one value that would be silently accepted by a looser reader is
    the value that decides whether the printer switches filament on its own, so this refuses."""
    if value is True or value is False:
        return value
    raise PrinterError(
        "protocol_error",
        f"The auto-refill switch takes a plain true or false; {str(value)[:40]!r} is neither. "
        "Nothing was sent.",
        hint="Pass true to ask the printer to refill automatically from another CANVAS tray, false "
             "to stop it. One boolean and nothing else: this tool sends the field Elegoo's own LAN "
             "client sends (auto_refill) and never the older manual's enable.",
        reason="refill_argument")


def refill_params(want: bool) -> dict:
    """The 2004 params: ``{"auto_refill": <bool>}``, the field Elegoo's own client sends and the
    same name the printer answers in its 2005 reply. One field on purpose — the tray and canvas ids
    that ride the 2005 reply belong to other commands, and touching them here would change what the
    printer is printing with while the user asked about a switch."""
    return {"auto_refill": parse_refill(want)}


def checked_params(params: Any) -> dict:
    """The second lock on the door the tool already holds: the params that may leave are exactly the
    one documented field with a boolean in it. The tool builds its own with ``refill_params``, so
    this never fires in normal use — it fires on a caller that hand-rolled a frame, and neither the
    manual's ``enable`` nor a frame carrying a tray id is allowed to reach a printer because some
    layer above forgot to check."""
    if not isinstance(params, dict) or set(params) != {"auto_refill"}:
        raise ValueError("CC2 auto-refill params are exactly the one field from refill_params() "
                         f"— {{'auto_refill': true|false}} — got {str(params)[:60]!r}")
    return refill_params(params["auto_refill"])


# --------------------------------------------------------------------------- the reading


def refill_word(result: Any) -> tuple[bool | None, Any]:
    """(the flag as a 2005 reply reports it, or None when it reports nothing readable; the raw value
    as it arrived, or None when there was no value). A reply is read as ``canvas_info`` when it
    carries that block and as itself when it is that block already, which is the tolerance
    ``cc2_state``'s canvas reader uses for the same reply. An absent field is unknown, never false:
    the status reader's default is an answer to a question a control's read-back must not answer for
    the printer. 0 and 1 are the numbers they are; anything else is unknown with the value kept."""
    if not isinstance(result, dict):
        return None, None
    info = result.get("canvas_info")
    if not isinstance(info, dict):
        info = result if "canvas_list" in result else {}
    raw = info.get("auto_refill")
    if isinstance(raw, bool):
        return raw, raw
    if raw is None:
        return None, None
    if raw in (0, 1):           # the same reading cc2_state's canvas reader applies to 0/1
        return bool(raw), raw
    return None, raw            # a value that cannot be read is an unknown, and it stays on record


def _reading(result: Any) -> dict:
    """The preview's view of what the printer says now, named apart from the command's own words so
    a caller can see that the preview state and the will-send value are two different things."""
    on, raw = refill_word(result)
    return {"auto_refill_now": on, "auto_refill_raw_now": raw, "auto_refill_is_known": on is not None}


# --------------------------------------------------------------------------- the preview (read-only)


async def read_preview(transport: Any, params: dict, *, timeout_s: float | None = None) -> dict:
    """What this exact confirmed call would send, and what the printer's CANVAS reply says about the
    flag right now — the maintainer companion's ``confirm_required`` shape, the same one the job and
    peripheral controls use. The 2005 read below is the only method this path ever sends: no command
    leaves the wire without ``confirm=True``, and the tests watch the recorded wire for exactly that.

    A CANVAS read that fails is reported, not raised, and the preview still says what a confirmed
    call would send. The light and the speed mode raise on a failed status read because their state
    is a field of the status frame every printer answers; this one has a third answer available —
    there may be no CANVAS attached, which is a legal state for a printer and not a reason to refuse
    a switch the printer stores for itself. Silently turning that into ``false`` is the thing that is
    refused instead: the preview says the flag is unknown and says the reason."""
    params = checked_params(params)
    notes: list[str] = []
    why: str | None = None
    try:
        reply = await transport.request(st.METHOD_CANVAS, {}, timeout_s=timeout_s)
    except PrinterError as e:
        reply, why = None, (getattr(e, "message", None) or str(e))[:180]
    reading = _reading(reply)
    if not reading["auto_refill_is_known"]:
        notes.append(OBSERVE_UNKNOWN_NOTE)
        if why:
            notes.append(OBSERVE_WHY.format(why=why))
    return {"confirm_required": True, "action": SETTING,
            "preview": {"will_send": {"method": METHOD_SET_AUTO_REFILL, "params": dict(params)},
                        "waits_up_to_s": SET_WAIT_S, **reading},
            "how": job.PREVIEW_HOW, "verified_on_this_printer": False, "unverified": UNVERIFIED,
            "notes": notes}


# --------------------------------------------------------------------------- the one command


async def _observe_quietly(transport: Any) -> tuple[dict | None, list[str]]:
    """Exactly one read-only 2005, or the notes saying it produced no reading. The only follow-up a
    sent command may ever cause; the command itself is never reconsidered, and a read that failed is
    not retried either."""
    why: str | None = None
    reply: Any = None
    try:
        reply = await transport.request(st.METHOD_CANVAS, {})
    except PrinterError as e:
        why = getattr(e, "message", None) or str(e)
    on, raw = refill_word(reply)
    if on is None and raw is None:
        notes = [OBSERVE_UNKNOWN_NOTE] + ([OBSERVE_WHY.format(why=why[:180])] if why else [])
        return None, notes
    return {"observed_auto_refill": on, "observed_auto_refill_raw": raw}, []


def _contradiction_note(sent: dict, observed: dict | None) -> str | None:
    """The one note that may say the printer accepted a command its own CANVAS read does not show —
    said only when the look actually contradicts the ask, and never as a proposal to send again: the
    command went, the printer did not refuse it, and this module sends nothing a second time."""
    if not observed:
        return None
    seen = observed.get("observed_auto_refill")
    if seen is None or seen == bool(sent.get("auto_refill")):
        return None
    return CONTRADICT.format(asked="on" if sent.get("auto_refill") else "off",
                             seen="off" if not seen else "on")


async def set_auto_refill(transport: Any, params: dict,
                          *, timeout_s: float | None = None) -> Cc2RefillResult:
    """Send the one 2004 command with ``params``, once, and report what came back — or report,
    truthfully, that nothing readable came back. ``timeout_s`` overrides the documented wait for
    tests; nothing in this call is ever repeated, and the only extra read it may cause is the one
    2005 ``_observe_quietly`` takes after an accepted or unanswered command.

    The classification is the job controls' bar, applied to this write: an explicit integer
    ``error_code`` 0 is acceptance, any other integer is the printer declining on record (kept, and
    earning no follow-up read), and a missing, bool, float or text code is an ambiguity the caller
    resolves by reading state deliberately — never here, where a second command would be tempting.
    Where the accepted command and the following look disagree, the disagreement is said out loud in
    its own note without ever doubting the acceptance. Whatever the outcome, the reply still carries
    ``verified_on_this_printer: false``: the printer answering 0 is the printer not refusing, and
    what it did to its own refill behaviour is a separate question with a separate test."""
    params = checked_params(params)
    wait_s = SET_WAIT_S if timeout_s is None else timeout_s
    sent = dict(params)

    def base(status: ControlStatus, **over) -> dict:
        d = {"method": METHOD_SET_AUTO_REFILL, "sent": sent, "waited_s": wait_s, "status": status}
        d.update(over)
        return d

    try:
        reply = await transport.request(METHOD_SET_AUTO_REFILL, sent, timeout_s=wait_s)
    except Cc2TimeoutError:
        notes = [job.LOST_NOTE.format(verb=SETTING, wait=wait_s)]
        observed, lost_notes = await _observe_quietly(transport)
        return Cc2RefillResult(**base(ControlStatus.AMBIGUOUS, observed=observed,
                                      notes=tuple(notes + lost_notes)))
    except PrinterError as e:
        why = getattr(e, "message", None) or str(e)
        return Cc2RefillResult(**base(ControlStatus.AMBIGUOUS,
                                      notes=(job.DEAD_NOTE.format(verb=SETTING) + why,)))

    # ``error_code``: only an explicit integer answers. No code at all, or one that cannot be read,
    # is an ambiguity the caller resolves by reading state deliberately — never here, where it would
    # tempt a second command (the same bar cc2_start, cc2_control and cc2_peripherals hold to).
    if not isinstance(reply, dict) or "error_code" not in reply:
        detail = "carried no error code at all"
        return Cc2RefillResult(**base(ControlStatus.AMBIGUOUS,
                                      notes=(job.UNREADABLE_NOTE.format(verb=SETTING,
                                                                        detail=detail),)))
    code = reply["error_code"]
    if not job.plain_int(code):
        detail = f"carried an error code that cannot be read ({type(code).__name__})"
        return Cc2RefillResult(**base(ControlStatus.AMBIGUOUS,
                                      notes=(job.UNREADABLE_NOTE.format(verb=SETTING,
                                                                        detail=detail),)))
    if code != 0:
        return Cc2RefillResult(**base(
            ControlStatus.REFUSED, error_code=code,
            notes=(job.refusal_note(SETTING, code), job.NO_OBSERVE_AFTER_REFUSAL)))

    observed, extra_notes = await _observe_quietly(transport)
    notes = [job.ACCEPT_BASE.format(verb=SETTING), _ACCEPT_TAIL]
    contra = _contradiction_note(sent, observed)
    if contra is not None:
        notes.append(contra)
    notes += extra_notes
    return Cc2RefillResult(**base(ControlStatus.ACCEPTED, accepted=True, observed=observed,
                                  notes=tuple(notes)))
