"""The CANVAS tray filament-info write — method 2003 ``SetFilamentInfo``, the nine fields Elegoo's own
LAN web client sends for one tray, and Elegoo's own filament catalog doing the deriving — with the
honesty contract every other CC2 control on this server keeps and its vocabulary imported rather than
reworded (``cc2_control``): accepted (an explicit ``error_code`` 0), refused (an explicit nonzero code,
kept) or ambiguous (no answer, an unreadable one, or a dead session), and a lost answer is never a
refusal and never resent.

**This command has never been offered to a CC2 by anyone on this project, and every reply says so.**
What is established here is what Elegoo's own software puts on the wire, not what this firmware does
with it. The older bundle the printer serves itself (``/webapp/lan/index.html``, firmware
02.01.00.00) does not contain ``SetFilamentInfo`` at all — that build still speaks the pre-CANVAS
websocket name for the same idea (``CmdAMSSetFilamentInfo = 501``) — so what is proven is the current
published client's frame, and whether this firmware answers 2003, what it does with a code or a brand
it has never seen, whether its 2005 read reflects the change immediately (the auto-refill readback
lagged) and whether it accepts a tray edit while a print runs are four questions with no answer yet.

Where the shape comes from, verbatim: the client's own method enum spells ``SetFilamentInfo=2003`` in
the same enum that names ``SetAutoFillFilament=2004`` and ``GetCanvasInfo=2005``, its store wraps the
params the tray edit dialog builds as ``{method: SetFilamentInfo, params: <those fields>}`` with no
timeout override of its own, treats a nonzero ``error_code`` as the failure it toasts as "Failed to set
filament information.", and its slot page overwrites ``canvas_id``/``tray_id`` from the page it was
opened from before sending. The dialog's own params are nine fields, sent every time:
``canvas_id``, ``tray_id``, ``brand``, ``filament_type``, ``filament_name``, ``filament_code``,
``filament_color``, ``filament_min_temp``, ``filament_max_temp`` — and of those nine, the dialog takes
only four from the human (which tray, which material, which colour, which brand): the type, the code
and both temperatures come out of its catalog lookup, ``bi(name)``, for the chosen name. So they come
out of ``CATALOG`` here too, and a caller-supplied temperature is refused as what it is — a guess the
vendor UI never asks for.

The catalog below is that lookup table: all 50 rows of the vendor bundle's catalog, transcribed to
``openspec/changes/cc2-tray-filament-info/filament-catalog.txt`` and embedded here so an installed
server needs no checkout. It is the reason a name outside it refuses before any session: an unknown
name is what the vendor's own lookup answers with empty strings and a 0-0 range, which on this wire is
a tray told it holds a material with no type, no code and no temperature — not a refusal the printer
owes anyone. The brand the dialog offers is "ELEGOO" (the rows whose ``elegoo`` flag is set) or
"Generic" (its ``generic`` flag, set for every row), plus free text, because the dialog accepts free
text and its brand filter filters the catalog rather than validating the string; a brand outside the
two names is therefore allowed and labelled as unproven against a printer by the reply's own
``verified_on_this_printer``.

One asymmetry is worth keeping straight, because both spellings are correct on their own half of the
wire: the write says ``filament_min_temp``/``filament_max_temp`` and carries ``filament_code``, while
the 2005 read answers ``min_nozzle_temp``/``max_nozzle_temp`` and never reports a code. Nothing here
invents a third spelling, and the wait is the same pair of first-party windows as the auto-refill
switch: the client's layer passes no override for 2003, so its 5 s default applies, the Home Assistant
integration waits 10 s for every command it sends, and the longer of the two is what this waits,
because a short window is what turns a command the printer took into a report of an answer that never
arrived — and nothing is ever resent to catch one.

What this module refuses before any session opens is the whole argument bar: the two ids as whole
numbers ≥ 0 (and note the printer's own numbering, A0-A3, is not capped here, because whether a
``(canvas_id, tray_id)`` pair exists is a fact the printer's 2005 roster answers, not an upper bound
invented in a module), the material name as an exact key of ``CATALOG``, the colour as ``#RRGGBB``
taken case-insensitively and sent upper-case, and the brand as a stripped, non-empty string of at most
32 characters. The one gate this module adds beside the vendor's own checks is an idle-machine gate,
and it is a bar of this project's own, not the vendor's: the edit dialog's load/unload buttons are
gated by machine state while the edit icon that opens it is not, so the vendor's dialog would happily
open a tray edit while a print runs, and whether this firmware accepts the write then is one of the
open questions above. This module does not volunteer to be the experiment: both phases read the
machine state first (``cc2_state.fetch_machine_status``, the same 1002 and the same ``ready_for_job``
bar ``cc2_motion`` holds a manual move to) and refuse, naming the printer's own state, unless that
bar says idle — and a 1002 that will not answer, or cannot be read, refuses too, because an
unreadable state is never an idle one. The preview carries that reading as ``machine_now``. Privacy: nothing here takes an address, a token or an environment variable — the
transport is the pinned session — and no message, note or result carries credentials or raw replies.
Every string is written inside the direct tools' reply cap (``cc2_control.NOTE_CAP``), because a
promise truncated mid-sentence is how an honesty note loses its tail.

The two phases are an idle gate, a roster read and, only on the confirmed one, one command. Both
start with the same read-only 1002 (the machine must call itself idle for a job) and then the same
read-only 2005 (``tray_now`` reads the raw frame the way ``cc2_canvas`` does, so a tray this
adapter cannot read stays "unknown" rather than becoming a raise): the ``(canvas_id, tray_id)`` pair
has to appear in that reply and its CANVAS unit has to report itself connected, or the change refuses
before anything is written, because a tray the printer never named is a write to nowhere. Unlike the
auto-refill switch, a 2005 that will not answer refuses here rather than previewing anyway, and the
difference is the shape of the two commands: a flag the printer stores for itself is legal with no
CANVAS behind it, a numbered tray of a numbered unit is not. The confirmed phase then sends exactly
one 2003, waits ``SET_WAIT_S``, classifies accepted / refused / ambiguous with ``cc2_control``'s words,
takes exactly one quiet 2005 afterwards after an accepted or unanswered command (never after an
on-record refusal, never after a dead session), and says out loud, in its own note, when that read
contradicts what was sent — a read that may lag, which is the second open question above and is
written into the note rather than smoothed over."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import cc2_control as job
from . import cc2_state as st
from .cc2_control import ControlStatus   # the same three-way answer, one vocabulary
from .cc2_transport import Cc2TimeoutError, PrinterError

METHOD_SET_FILAMENT_INFO = 2003   # SetFilamentInfo — the nine fields below, all of them, every time
ACTION = "set_tray_filament"      # the tool's own word for what a preview is offering to do
VERB = "tray filament"            # the word cc2_control's shared notes put in front of "command"

# The nine fields of the vendor frame, in the dialog's own order: the whole shape ``checked_frame``
# admits, and the reason a tenth field, a caller-supplied temperature or the read side's
# ``min_nozzle_temp`` spelling cannot reach a printer from this module.
WIRE_FIELDS = ("canvas_id", "tray_id", "brand", "filament_type", "filament_name", "filament_code",
               "filament_color", "filament_min_temp", "filament_max_temp")

# The longer of the two first-party windows (see the module docstring): the LAN client's 5 s default
# for a method that passes no override, and the integration's 10 s for every command it sends.
SET_WAIT_S = 10.0

# Said in every reply, so the candidate label is a field a model reads rather than a docstring it may
# not have loaded. It fits the reply cap on purpose (NOTE_CAP): a caveat cut in half is the half that
# gets trusted.
UNVERIFIED = ("Method 2003 has never been sent to a CC2 by anyone on this project: what is proven is "
              "the frame Elegoo's own published client sends, not what this firmware does with it, "
              "and a refusal is evidence about the printer's firmware, not a broken printer.")

# The brand the vendor dialog fills in when the human does not pick one, and the length its text
# field answers with. Free text is allowed because the dialog allows it (see the docstring); this bar
# is only the bound that keeps a reply-shaped string out of a tray label.
BRAND_DEFAULT = "Generic"
BRAND_MAX = 32

# The printer's own label for a tray number: the vendor's slot page is A0..A3 for tray_id 0..3, and
# the roster read (not this module) is what proves a given pair exists on this machine.
TRAY_LABEL_PREFIX = "A"


# --------------------------------------------------------------------------- the vendor catalog

@dataclass(frozen=True)
class Filament:
    """One row of the vendor catalog: the material type the frame sends as ``filament_type``, the
    hex code it sends as ``filament_code`` (kept as the string the bundle writes, ``"0x0000"``, not a
    number — nothing on this wire is a decimal filament id), the two nozzle temperatures it sends as
    ``filament_min_temp``/``filament_max_temp``, and the two brand flags its dialog filters by
    (``elegoo`` offers the brand word "ELEGOO" for the row, ``generic`` offers "Generic"). Frozen
    because a catalog row is a fact about the vendor's table and a module-level dict is shared state:
    one caller must not be able to retype a material for every caller after it."""
    type: str
    code: str
    min: int
    max: int
    elegoo: bool
    generic: bool


# The catalog Elegoo's client ships, transcribed from the vendor bundle into
# openspec/changes/cc2-tray-filament-info/filament-catalog.txt and copied here row for row, in the
# file's own order. That file's columns are (key, type, name, code, min, max, elegoo, generic); its
# ``name`` column is identical to its ``key`` column for all 50 rows, so it is not stored twice here,
# and its ``!0``/``!1`` are the bundle's own minified ``true``/``false``. The file's rows are 50 and
# ``tests/test_cc2_tray.py`` rebuilds this table from that file, so a transcription drift fails a test
# rather than a printer.
_CATALOG_ROWS: tuple[tuple[str, str, str, int, int, bool, bool], ...] = (
    ("PLA", "PLA", "0x0000", 190, 230, True, True),
    ("PLA+", "PLA", "0x0001", 190, 230, True, True),
    ("PLA PRO", "PLA", "0x0002", 190, 230, True, True),
    ("PLA Silk", "PLA", "0x0003", 190, 230, True, True),
    ("PLA-CF", "PLA", "0x0004", 210, 240, True, True),
    ("PLA Carbon", "PLA", "0x0005", 190, 230, False, True),
    ("PLA Matte", "PLA", "0x0006", 190, 230, True, True),
    ("PLA Fluo", "PLA", "0x0007", 190, 230, False, True),
    ("PLA Wood", "PLA", "0x0008", 190, 230, True, True),
    ("PLA Basic", "PLA", "0x0009", 190, 230, True, True),
    ("RAPID PLA+", "PLA", "0x000A", 190, 230, True, True),
    ("PLA Marble", "PLA", "0x000B", 190, 230, True, True),
    ("PLA Galaxy", "PLA", "0x000C", 190, 230, True, True),
    ("PLA Red Copper", "PLA", "0x000D", 190, 230, True, True),
    ("PLA Sparkle", "PLA", "0x000E", 190, 230, False, True),
    ("PETG", "PETG", "0x0100", 230, 260, True, True),
    ("PETG-CF", "PETG", "0x0101", 240, 270, True, True),
    ("PETG-GF", "PETG", "0x0102", 240, 270, True, True),
    ("PETG PRO", "PETG", "0x0103", 230, 260, True, True),
    ("PETG Translucent", "PETG", "0x0104", 230, 260, True, True),
    ("RAPID PETG", "PETG", "0x0105", 230, 260, True, True),
    ("ABS", "ABS", "0x0200", 240, 280, True, True),
    ("ABS-GF", "ABS", "0x0201", 240, 280, False, True),
    ("TPU", "TPU", "0x0300", 220, 240, False, True),
    ("TPU 95A", "TPU", "0x0301", 220, 240, True, True),
    ("RAPID TPU 95A", "TPU", "0x0302", 220, 240, True, True),
    ("PA", "PA", "0x0400", 260, 290, False, True),
    ("PA-CF", "PA", "0x0401", 260, 300, False, True),
    ("PAHT-CF", "PA", "0x0402", 280, 320, True, True),
    ("PA6", "PA", "0x0403", 260, 290, False, True),
    ("PA6-CF", "PA", "0x0404", 270, 310, False, True),
    ("PA12", "PA", "0x0405", 240, 270, False, True),
    ("PA12-CF", "PA", "0x0406", 260, 290, False, True),
    ("CPE", "CPE", "0x0500", 220, 250, False, True),
    ("PC", "PC", "0x0600", 260, 290, True, True),
    ("PCTG", "PC", "0x0601", 260, 290, False, True),
    ("PC-FR", "PC", "0x0602", 260, 290, True, True),
    ("PVA", "PVA", "0x0700", 180, 210, False, True),
    ("ASA", "ASA", "0x0800", 240, 280, True, True),
    ("BVOH", "BVOH", "0x0900", 190, 210, False, True),
    ("EVA", "EVA", "0x0A00", 180, 220, False, True),
    ("HIPS", "HIPS", "0x0B00", 220, 250, False, True),
    ("PP", "PP", "0x0C00", 210, 250, False, True),
    ("PP-CF", "PP", "0x0C01", 220, 260, False, True),
    ("PP-GF", "PP", "0x0C02", 230, 250, False, True),
    ("PPA", "PPA", "0x0D00", 290, 310, False, True),
    ("PPA-CF", "PPA", "0x0D01", 300, 320, False, True),
    ("PPA-GF", "PPA", "0x0D02", 290, 310, False, True),
    ("PPS", "PPS", "0x0E00", 330, 340, False, True),
    ("PPS-CF", "PPS", "0x0E01", 340, 360, False, True),
)

# name -> its row. The keys are exactly what the dialog's catalog lookup answers to, and the lookup is
# case-sensitive on purpose: the vendor table's own keys are ("PLA", "PLA+", "PETG", ...), so "pla" is
# not a name that table holds, and a looser reader would be this fork inventing a second name for a
# material.
CATALOG: dict[str, Filament] = {
    name: Filament(type=material, code=code, min=low, max=high, elegoo=elegoo, generic=generic)
    for name, material, code, low, high, elegoo, generic in _CATALOG_ROWS
}

_COLOUR = re.compile(r"#[0-9a-fA-F]{6}$")


def catalog_names() -> list[str]:
    """The material names this module will accept, in the vendor catalog's own order (not sorted: a
    caller comparing against the printer's dialog reads the dialog's order). The refusal's ``hint``
    carries only an example because the full list does not fit the reply cap; this is the whole list,
    and a refusal carries it as its own ``catalog_names`` field so none of it is ever truncated."""
    return list(CATALOG)


# --------------------------------------------------------------------------- the argument bar


def _refuse(message: str, *, hint: str, reason: str, **extra: Any) -> PrinterError:
    """One refusal, worded the way every other CC2 control words its own: what was wrong, the fact
    that nothing left this process, what to send instead, and a ``reason`` a caller can branch on
    without parsing a sentence. The message and the hint are each written to fit the direct tools'
    reply cap (``cc2_control.NOTE_CAP``), which is why the 50 catalog names go as their own field
    (``extra``) rather than as a tail of a sentence the sanitiser would cut mid-name."""
    return PrinterError("protocol_error", f"{message} Nothing was sent.", hint=hint, reason=reason,
                        **extra)


def _checked_id(value: Any, field: str) -> int:
    """An id exactly as the frame must carry it: a whole number ≥ 0, and never a bool, which is an
    int in Python and not a canvas or tray number on any wire (``cc2_control.plain_int``'s rule, the
    one the answer bar uses for ``error_code``). No upper bound here on purpose: the printer's roster
    is what proves a ``(canvas_id, tray_id)`` pair exists, and inventing 3 as a ceiling in a module
    that cannot see the roster would be a guess about someone else's machine."""
    if not job.plain_int(value) or value < 0:
        raise _refuse(
            f"{field} must be a whole number from 0 up (the printer's own numbering); "
            f"{str(value)[:24]!r} is neither.",
            hint=f"Send the {field} the printer reports for itself: a CANVAS tray is "
                 f"{TRAY_LABEL_PREFIX}0 = 0, {TRAY_LABEL_PREFIX}1 = 1, and so on, and canvas_id is the "
                 "CANVAS unit a tray belongs to. Whether that pair exists is the printer's roster's "
                 "answer, read before anything is sent.",
            reason=field)
    return int(value)


def _checked_name(value: Any) -> str:
    """The material name as the vendor lookup takes it: one of ``CATALOG``'s 50 keys, exact and
    case-sensitive. An unknown name is refused rather than passed through, because the vendor's own
    lookup answers an unknown with empty strings and a 0-0 range, and on this wire that is a tray told
    it holds a material with no type, no code and no temperature — a corrupt label, not a no-op."""
    if isinstance(value, str) and value in CATALOG:
        return value
    sample = ", ".join(catalog_names()[:3])
    raise _refuse(
        f"{str(value)[:32]!r} is not a filament name in Elegoo's own catalog, so its type, code and "
        "nozzle range cannot be derived from it.",
        hint=f"Send a catalog name as the dialog writes it (for example {sample}, ...); the whole list "
             "is in this refusal's catalog_names field and in catalog_names(). Type, filament_code "
             "and both temperatures come from that name alone.",
        reason="filament_name",
        catalog_names=catalog_names())


def _checked_colour(value: Any) -> str:
    """The colour the frame sends: ``#RRGGBB``, read case-insensitively and normalised to upper case,
    because the vendor dialog writes its palette upper-case and a fork that sent lower-case would be
    this project's fingerprint on a frame the official client does not write that way. Anything else —
    a name, three digits, six digits without the hash, eight with it — is refused: no colour guess
    reaches a tray label."""
    text = value.strip() if isinstance(value, str) else value
    if isinstance(text, str) and _COLOUR.match(text):
        return text.upper()
    raise _refuse(
        f"{str(value)[:32]!r} is not a colour in the form this frame carries: #RRGGBB, one hash and "
        "exactly six hex digits.",
        hint="Send the colour as the tray dialog's palette writes it, for example #F88D36 or #000000 "
             "(lower case is accepted and sent upper-case). Colour names and three-digit forms are "
             "refused rather than expanded, because expanding one is a guess.",
        reason="filament_color")


def _checked_brand(value: Any) -> str:
    """The brand string the frame sends, as a stripped, non-empty string of at most ``BRAND_MAX``
    characters. Free text is allowed on purpose (the dialog allows it, and its brand field filters the
    catalog rather than validating what was typed), so the only bar here is the one that keeps a
    reply-shaped string out of a tray's label; what the firmware makes of a brand it has never seen is
    unverified, and every reply says so."""
    if not isinstance(value, str) or not (text := value.strip()) or len(text) > BRAND_MAX:
        raise _refuse(
            f"brand must be a text label of 1 to {BRAND_MAX} characters; "
            f"{str(value)[:32]!r} is not one.",
            hint="Send the brand the dialog offers — \"ELEGOO\" for a row the vendor catalog marks as "
                 f"its own, \"{BRAND_DEFAULT}\" for any other spool — or the brand on the spool, up to "
                 f"{BRAND_MAX} characters, which is what the dialog's free text allows.",
            reason="brand")
    return text


def parse_request(tray_id: Any, filament_name: Any, colour: Any, brand: Any,
                  canvas_id: Any) -> dict:
    """The nine 2003 params, checked and built in the vendor frame's own field order — and the only
    door to them: every value a caller can pass goes through this, before any session opens, so a
    frame that leaves this module is a frame the vendor's dialog could have written.

    What is derived and not passed: ``filament_type``, ``filament_code``, ``filament_min_temp`` and
    ``filament_max_temp`` come out of ``CATALOG[filament_name]``, exactly as the dialog's
    ``bi(name)`` lookup supplies them, which is why a temperature is never an argument here. What is
    normalised: the colour goes out upper-case and the brand stripped. What is *not* checked: whether
    this CANVAS actually has that tray — that is the roster read (``read_preview`` /
    ``set_tray_filament``), because only the printer knows its own trays.

    Raises ``PrinterError`` (``protocol_error`` plus a ``reason``) on any value it cannot place on the
    frame, with the catalog's names carried in full in the refusal's ``catalog_names`` field; the
    tool layer turns that into the refusal a caller sees, and nothing has been sent when it does."""
    name = _checked_name(filament_name)          # first: nothing else can be derived without it
    return {
        "canvas_id": _checked_id(canvas_id, "canvas_id"),
        "tray_id": _checked_id(tray_id, "tray_id"),
        "brand": _checked_brand(brand),
        "filament_type": CATALOG[name].type,
        "filament_name": name,
        "filament_code": CATALOG[name].code,
        "filament_color": _checked_colour(colour),
        "filament_min_temp": CATALOG[name].min,
        "filament_max_temp": CATALOG[name].max,
    }


def checked_frame(params: Any) -> dict:
    """The second lock on the door ``parse_request`` already holds: the only nine-field frame that may
    leave is one ``parse_request`` would itself have written. The tool builds its own through
    ``parse_request``, so this never fires in normal use — it fires on a caller that hand-rolled a
    frame, and neither a caller-supplied temperature, a tenth field, a ``min_nozzle_temp`` spelling
    borrowed from the read side, nor a frame missing a field is allowed to reach a printer because
    some layer above forgot to check."""
    if not isinstance(params, dict) or set(params) != set(WIRE_FIELDS):
        raise PrinterError(
            "protocol_error",
            "CC2 tray filament params are exactly the nine fields parse_request() builds. "
            "Nothing was sent.",
            hint="Build the params with parse_request(tray_id, filament_name, colour, brand, "
                 "canvas_id): it derives filament_type, filament_code and both filament_*_temp from "
                 "the catalog name, which is why no temperature is an argument anywhere here.",
            reason="params")
    return parse_request(params["tray_id"], params["filament_name"], params["filament_color"],
                         params["brand"], params["canvas_id"])


# --------------------------------------------------------------------------- the roster read (2005)

# The seven fields a 2005 tray entry answers for itself, in the printer's own spelling. Note what is
# NOT here: no filament_code (the read side reports none), and min/max nozzle temps under the read's
# spelling, which is not the write's — the two halves of the wire are deliberately kept apart.
TRAY_FIELDS = ("status", "filament_type", "filament_name", "brand", "filament_color",
               "min_nozzle_temp", "max_nozzle_temp")

# A 2005 tray colour arrives bare on this firmware (``cc2_state`` documents the same and puts the hash
# back for its own reader). This module keeps the raw value in every reply — it reports what the
# printer said — and normalises only when it compares, because comparing "00B0F0" to "#00B0F0" as
# different strings would flag a disagreement the printer never said.
_HEX = "0123456789abcdefABCDEF"

# The printer's own message, kept as evidence and clipped: it can name the peer, and the sentence a
# caller acts on must stay inside the reply cap. _DEAD_WHY_MAX is the tightest of the three because
# the dead-session note it rides in is the longest of the templates.
_WHY_MAX = 110
_OBSERVE_WHY_MAX = 100


def _clip(text: Any, limit: int) -> str:
    """A fact from the printer's side, kept, and never cut mid-fact in silence: when a clip really
    bites it ends with an ellipsis, so a reader of the reply can see the sentence was bounded here
    rather than by the sanitiser on its way out."""
    text = str(text)
    return text if len(text) <= limit else text[:max(0, limit - 1)] + "\u2026"


def _colour_reading(value: Any) -> str | None:
    """A colour as a comparison, or None when it is not a reading to compare: six hex digits, with or
    without the hash, either case. None means "nothing usable arrived", which can never contradict
    anything — it is not a report of a different colour."""
    if not isinstance(value, str):
        return None
    text = value.strip().lstrip("#")
    return text.upper() if len(text) == 6 and all(c in _HEX for c in text) else None


def _canvas_info(frame: Any) -> dict | None:
    """The ``canvas_info`` block of a 2005 reply, or None when there is no roster to read at all. The
    bare inner object is accepted too, the way ``cc2_state``'s reader and ``cc2_canvas`` accept it, so
    a printer that answers the block without its envelope is still readable. What this deliberately
    does not do is ``Cc2CanvasStatus.from_result``'s trick of raising on a tray it cannot parse: a
    control's read needs the tray it asked about and nothing else, and one unreadable tray in someone
    else's slot must not be the end of this call."""
    if not isinstance(frame, dict):
        return None
    info = frame.get("canvas_info")
    if isinstance(info, dict):
        return info
    return frame if isinstance(frame.get("canvas_list"), list) else None


def _unit_for(info: dict, canvas_id: int) -> dict | None:
    """The CANVAS unit a tray belongs to, by the id the frame carries (``canvas_id`` defaults to 0 the
    way ``cc2_state`` reads the same default). The roster is the printer's list of its own units, so
    this is the whole answer to "does this canvas exist"."""
    units = info.get("canvas_list")
    if not isinstance(units, list):
        return None
    for unit in units:
        if isinstance(unit, dict) and unit.get("canvas_id", 0) == canvas_id:
            return unit
    return None


def _connected(unit: dict) -> bool | None:
    """(does this unit report itself connected; None when it reports neither yes nor no). 0/False are
    the printer's "not connected", 1/True its "connected", and anything else, including an absent
    field, is unknown — which for a gate is a refusal, never a guess in either direction."""
    raw = unit.get("connected")
    if raw is True or (job.plain_int(raw) and raw == 1):
        return True
    if raw is False or (job.plain_int(raw) and raw == 0):
        return False
    return None


def _tray_entry(unit: dict, tray_id: int) -> dict | None:
    """One tray by the number the printer itself wrote beside it, or None when this unit does not list
    it. Nothing is defaulted into existence: an absent tray is an absent tray."""
    trays = unit.get("tray_list")
    if not isinstance(trays, list):
        return None
    for entry in trays:
        if isinstance(entry, dict) and entry.get("tray_id") == tray_id:
            return entry
    return None


def _reading_of(entry: dict, tray_id: int) -> dict:
    """The tray's own seven fields as they arrived, plus the id it was found under. Absent fields
    arrive as None rather than as a zero, an empty string or a guess: a tray the printer lists but
    says nothing about is a tray whose material is unknown, and a preview that filled in "PLA" for it
    would be this fork writing the label it came to read."""
    now: dict[str, Any] = {"tray_id": entry.get("tray_id", tray_id)}
    for field_name in TRAY_FIELDS:
        now[field_name] = entry.get(field_name)
    return now


def tray_now(frame: Any, canvas_id: Any, tray_id: Any) -> dict | None:
    """What the printer's own 2005 roster says about one tray right now, or None when that reply does
    not list it — including when the CANVAS unit it would belong to is absent or reports itself not
    connected, because "connected unit A has no tray 3" and "there is no unit A" are both answers of
    "this tray is not something the printer is telling me about".

    The keys are the read side's own spellings (``filament_type``, ``filament_name``, ``brand``,
    ``filament_color``, ``status``, ``min_nozzle_temp``, ``max_nozzle_temp``) with the values exactly
    as they arrived, hash or no hash, placeholder or real word: this is the printer's sentence about
    its own tray, and both the preview's ``tray_now`` and the write's ``observed`` are that sentence
    with its parts named apart from the command's own words."""
    info = _canvas_info(frame)
    if info is None:
        return None
    unit = _unit_for(info, canvas_id)
    if unit is None or _connected(unit) is not True:
        return None
    entry = _tray_entry(unit, tray_id)
    return None if entry is None else _reading_of(entry, tray_id)


def _tray_view(now: dict) -> dict:
    """The preview's look at that reading, in the words a human reads a tray in (material, name,
    colour) with the printer's numbers kept as numbers. Renamed, never revalued: every value is the
    one the roster answered."""
    return {"tray_id": now["tray_id"], "material": now["filament_type"], "name": now["filament_name"],
            "brand": now["brand"], "colour": now["filament_color"],
            "min_nozzle_temp": now["min_nozzle_temp"], "max_nozzle_temp": now["max_nozzle_temp"],
            "status": now["status"]}


# --------------------------------------------------------------------------- the gate (both phases)

_ROSTER_HINT = ("Read the printer's CANVAS roster deliberately (cc2_status reads it on demand) and "
                "call again with a tray the printer itself named. Nothing was sent.")
_PAIR_HINT = ("Send the canvas_id and tray_id the printer's own CANVAS roster reports: a tray of "
              "CANVAS 0 is A0 = 0, A1 = 1, and so on. cc2_status reads the roster on demand. "
              "Nothing was sent.")


def _refuse_roster(detail: str, *, hint: str, reason: str) -> PrinterError:
    """One gate refusal, in the same voice as every other CC2 refusal: what could not be
    established, in one sentence, the fact that nothing left, and a ``reason`` to branch on. The
    detail is the printer's own words about why its roster did not answer, clipped so the sentence
    keeps its tail inside the reply cap."""
    return _refuse(f"The printer's CANVAS roster read (method {st.METHOD_CANVAS}) {detail}.",
                   hint=hint, reason=reason)


def _pair_label(canvas_id: int, tray_id: int) -> str:
    """The tray being argued about, in the printer's numbering and the human's label at once, so a
    refusal about tray 1 of canvas 0 is not mistaken for one about tray 1 of canvas 2."""
    return f"tray {TRAY_LABEL_PREFIX}{tray_id} (tray_id {tray_id}) of canvas {canvas_id}"


def _pair_short(canvas_id: int, tray_id: int) -> str:
    """The same tray in the fewest characters that still name it, for the one note with the least room
    to spare (``CONTRADICT"): the ids stay, the parenthetical does not."""
    return f"tray {TRAY_LABEL_PREFIX}{tray_id} of canvas {canvas_id}"


_IDLE_HINT = ("Bring the printer back to idle — let the job finish, or cancel it deliberately — and "
              "preview again; cc2_status reads the machine state on demand. Nothing was sent.")
_STATE_HINT = ("Nothing was sent. Read the state deliberately (cc2_status) and decide again — a "
               "state this adapter cannot read is not evidence that the machine is idle.")


def _refuse_state(message: str, *, hint: str, reason: str,
                  printer_says: str | None = None) -> PrinterError:
    """One refusal from the idle gate, in the same voice as the roster gate: what the state read
    could not establish, in one sentence, the fact that nothing left, the printer's own words beside
    it in their own field (clipped to the same bar the roster gate uses, so the sentence keeps its
    tail inside the reply cap), and a ``reason`` to branch on."""
    details = {} if printer_says is None else {"printer_says": _clip(printer_says, _WHY_MAX)}
    return _refuse(f"The printer's machine state read (method {st.METHOD_STATUS}) {message}.",
                   hint=hint, reason=reason, **details)


def _machine_view(machine: st.Cc2MachineStatus) -> dict:
    """The state the idle gate stood on, as the preview shows it: the printer's own machine number
    and ``cc2_state``'s word for it (the one table, imported, never a second list of words; a number
    off the doc's 0-15 list stays None, which is what 'this adapter does not know it' looks like in
    a reply). Nothing is interpreted: the numbers are the printer's, the word is the doc's."""
    return {"machine_status": machine.machine_status,
            "state": st._MACHINE_WORDS.get(machine.machine_status)}


async def _gate(transport: Any, params: dict, *, timeout_s: float | None = None
                ) -> tuple[dict, st.Cc2MachineStatus]:
    """The two read-only gates both phases stand on, in one order — the machine first, then the
    roster — and the pair they answer: the tray's reading and the machine state the idle gate used.

    The 1002 runs first because a machine that is not idle refuses the call before it has asked
    anything else, exactly the way ``cc2_motion`` reads its state before a move: a call that sends
    nothing must also have asked nothing after it. The bar is ``ready_for_job`` — machine status 1,
    sub-state 0, the idle print state, no error, no file held — so printing, paused (cc2_state's own
    words for 2/2502: a job, not a clear bed), initializing or any state the adapter cannot name all
    refuse with the printer's own sentence in the refusal, and so does a 1002 that will not answer or
    cannot be read, because an unreadable state is never an idle one. Neither read is retried.

    Then the roster: this is the gate that replaces any ceiling this module could invent: whether a
    ``(canvas_id, tray_id)`` pair exists and whether its unit is connected are facts on the printer,
    and the printer is the only thing that can answer them. Three answers refuse, each with no command
    on the wire: the roster would not answer or could not be read as a roster
    (``canvas_unreadable``), the printer does not list that pair (``tray_not_listed``), and the unit
    is there but says it is not connected (``canvas_disconnected``). The auto-refill switch previews
    through a silent CANVAS read because a printer with no CANVAS is a legal state for a flag; it is
    exactly that freedom this phase cannot take, because a tray write needs a tray.

    A failed read is never retried and never followed by a command: the reason a state or a roster is
    missing may well be that the session is dying, and the printer's refusal is the answer this
    module accepts about its own command."""
    canvas_id, tray_id = params["canvas_id"], params["tray_id"]
    pair = _pair_label(canvas_id, tray_id)
    try:
        machine = await st.fetch_machine_status(transport)
    except PrinterError as e:
        why = _clip(getattr(e, "message", None) or str(e), _WHY_MAX)
        raise _refuse_state(f"did not answer or could not be read as a machine state ({why})",
                            hint=_STATE_HINT, reason="machine_unreadable") from None
    ok, why = machine.ready_for_job()
    if not ok:
        raise _refuse_state(f"will not let a tray be edited while the printer holds this state: "
                            f"{_clip(why, _WHY_MAX)}", hint=_IDLE_HINT, reason="machine_not_idle",
                            printer_says=why)
    try:
        frame = await transport.request(st.METHOD_CANVAS, {}, timeout_s=timeout_s)
    except PrinterError as e:
        why = _clip(getattr(e, "message", None) or str(e), _WHY_MAX)
        raise _refuse_roster(f"did not answer ({why})", hint=_ROSTER_HINT,
                             reason="canvas_unreadable") from None
    info = _canvas_info(frame)
    if info is None:
        raise _refuse_roster("answered in a shape this adapter cannot read as a CANVAS roster",
                             hint=_ROSTER_HINT, reason="canvas_unreadable")
    if not isinstance(info.get("canvas_list"), list) or not info.get("canvas_list"):
        raise _refuse_roster("answered with no CANVAS unit list to read a tray against",
                             hint=_ROSTER_HINT, reason="canvas_unreadable")
    unit = _unit_for(info, canvas_id)
    if unit is None:
        raise _refuse_roster(f"does not list a CANVAS unit {canvas_id}, so nothing lists {pair}",
                             hint=_PAIR_HINT, reason="tray_not_listed")
    if _connected(unit) is not True:
        raise _refuse_roster(f"reports CANVAS unit {canvas_id} as not connected, so no tray of it is "
                             "a tray the printer will act on",
                             hint=_PAIR_HINT, reason="canvas_disconnected")
    if not isinstance(unit.get("tray_list"), list):
        raise _refuse_roster(f"lists CANVAS unit {canvas_id} with no tray list to read a tray "
                             "against", hint=_ROSTER_HINT, reason="canvas_unreadable")
    entry = _tray_entry(unit, tray_id)
    if entry is None:
        raise _refuse_roster(f"lists CANVAS unit {canvas_id} but no {pair}",
                             hint=_PAIR_HINT, reason="tray_not_listed")
    return _reading_of(entry, tray_id), machine


# --------------------------------------------------------------------------- the notes

# Said in both phases: this wait for an idle machine is the module's own bar, not the vendor's. The
# dialog that writes this frame has no such gate, and the printer answering a tray edit mid-print is
# exactly the question this module refuses to be the first to ask a real machine.
IDLE_GATE = ("This write waited for an idle machine: the 1002 read before the roster read refuses "
             "unless the printer says it is idle, and an unreadable state is never idle. The "
             "vendor's tray edit dialog has no such gate — this wait is this module's own.")

# The per-command tail of the acceptance sentence, and the second open question written into it: what
# a read afterwards says about a tray label may lag the write (the auto-refill readback did), so the
# observation is never the evidence and the evidence is never softened by it.
_ACCEPT_TAIL = ("What this firmware stores for a tray it has never seen is unverified, and the one "
                "CANVAS look afterwards may lag the write: the observed tray is a separate read, "
                "never part of acceptance, and never a reason to send again.")

# The note said only when that one look actually contradicts what was sent. It cannot be read as an
# offer to send again, and it does not doubt the acceptance: the printer answered the command, and
# this is what its own roster says. The parts are clipped so the sentence always keeps its tail.
CONTRADICT = ("Note the disagreement: the printer accepted {tray} as {asked}; its own CANVAS read "
              "afterwards says {seen}. The read may lag; accepted and observed stand apart, and "
              "nothing is resent.")
_CONTRA_PART = 36
_CONTRA_VALUE = 16

# The read afterwards produced no reading for this tray. Its own note, with the reason beside it, so
# this promise is never the thing that gets cut for space.
OBSERVE_UNKNOWN_NOTE = ("The one read-only CANVAS look (method 2005) this call allows itself gave no "
                        "reading for this tray: it may have been refused, answered in a shape this "
                        "adapter cannot read, or no longer list that tray. The tray stays unknown.")
OBSERVE_WHY = "Why the CANVAS look gave nothing: {why}"


@dataclass(frozen=True)
class Cc2TrayResult:
    """The whole outcome of one tray filament-info command. ``status`` says what the printer answered
    the 2003 and ``accepted`` is true only for its explicit 0; ``error_code`` keeps a refusal's number;
    ``sent`` is the exact nine-field params that left, so a caller can see which four came from it and
    which five the vendor catalog derived. ``tray_now`` is the roster's reading of that tray taken
    *before* the command — the gate's own evidence, which is also the preview's — and ``observed`` is
    the reading taken *after* an accepted or unanswered command (never after an on-record refusal, never
    on a dead session), or None when that look produced no reading, which is never read as "the tray is
    empty". Two readings and one command, three facts, never merged. ``verified_on_this_printer`` is
    False and stays False until this firmware has actually been asked; ``notes`` carry what must be
    said out loud and ``as_dict()`` is the shape a tool reports."""
    method: int
    sent: dict
    status: ControlStatus
    waited_s: float
    tray_now: dict | None = None
    accepted: bool = False
    error_code: int | None = None
    observed: dict | None = None
    verified_on_this_printer: bool = False
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "action": ACTION, "method": self.method, "sent": dict(self.sent),
            "status": str(self.status), "accepted": self.accepted, "error_code": self.error_code,
            "waited_s": self.waited_s,
            "tray_now": dict(self.tray_now) if self.tray_now else None,
            "observed": dict(self.observed) if self.observed else None,
            "verified_on_this_printer": self.verified_on_this_printer, "unverified": UNVERIFIED,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- the preview (read-only)


async def read_preview(transport: Any, params: dict, *, timeout_s: float | None = None) -> dict:
    """What this exact confirmed call would send, and what the printer's own roster says about that
    tray right now — the maintainer companion's ``confirm_required`` shape, the same one the job,
    peripheral and auto-refill controls use. The 1002 and the 2005 below are the only methods this
    path ever sends: no command leaves the wire without ``confirm=True``, and the tests watch the
    recorded wire for exactly that.

    The two gate reads are the difference from the auto-refill preview: the machine must call itself
    idle for a job (``machine_now`` is that reading, the printer's number and ``cc2_state``'s word
    for it, and a printer that is printing, paused or unreadable refuses here first), and a CANVAS
    read that will not answer, or that does not name this tray, or whose unit says it is not
    connected, refuses the preview — and with it the change — rather than showing a frame that could
    only ever be a guess about a tray the printer never named. What the preview does say, whenever
    the gates hold, is the nine-field ``will_send`` (four values from the caller, five derived by the
    catalog), the wait, the tray as the printer itself words it, and the machine state the write
    waited on."""
    params = checked_frame(params)
    now, machine = await _gate(transport, params, timeout_s=timeout_s)
    return {"confirm_required": True, "action": ACTION,
            "preview": {"will_send": {"method": METHOD_SET_FILAMENT_INFO, "params": dict(params)},
                        "waits_up_to_s": SET_WAIT_S, "tray_now": _tray_view(now),
                        "machine_now": _machine_view(machine)},
            "how": job.PREVIEW_HOW, "verified_on_this_printer": False, "unverified": UNVERIFIED,
            "notes": [IDLE_GATE]}


# --------------------------------------------------------------------------- the one command


def _contradiction_note(sent: dict, observed: dict | None) -> str | None:
    """The one note that may say the printer accepted a command its own roster does not show — said
    only when the look actually contradicts the ask, and never as a proposal to send again. Comparison
    is deliberately narrow, because a note that fires on nothing is worse than one that stays quiet:
    a name or type is a disagreement only when both sides wrote a real word for it (an empty field or
    the firmware's ``—`` placeholder is no reading), a colour is one only when both sides are readable
    hex through the printer's habit of dropping the hash, and a field that arrived absent contradicts
    nothing. Each side is clipped, and the note's own tail is what must survive."""
    if not observed:
        return None
    diffs: list[tuple[str, str, str]] = []
    for field, label in (("filament_name", "name"), ("filament_type", "type")):
        asked, seen = sent.get(field), observed.get(field)
        seen = seen.strip() if isinstance(seen, str) else ""
        if (isinstance(asked, str) and asked and seen and not seen.startswith("\u2014")
                and asked != seen):
            diffs.append((label, asked, seen))
    asked_c = _colour_reading(sent.get("filament_color"))
    seen_c = _colour_reading(observed.get("filament_color"))
    if asked_c and seen_c and asked_c != seen_c:
        diffs.append(("colour", str(sent.get("filament_color")), str(observed.get("filament_color"))))
    if not diffs:
        return None
    asked_text = ", ".join(f"{label}={_clip(value, _CONTRA_VALUE)}" for label, value, _ in diffs)
    seen_text = ", ".join(f"{label}={_clip(value, _CONTRA_VALUE)}" for label, _, value in diffs)
    return CONTRADICT.format(tray=_pair_short(sent["canvas_id"], sent["tray_id"]),
                             asked=_clip(asked_text, _CONTRA_PART),
                             seen=_clip(seen_text, _CONTRA_PART))


async def _observe_quietly(transport: Any, canvas_id: int, tray_id: int
                           ) -> tuple[dict | None, list[str]]:
    """Exactly one read-only 2005, or the notes saying it produced no reading for this tray. The only
    follow-up a sent command may ever cause; the command itself is never reconsidered, and a read that
    failed is not retried either."""
    why: str | None = None
    frame: Any = None
    try:
        frame = await transport.request(st.METHOD_CANVAS, {})
    except PrinterError as e:
        why = getattr(e, "message", None) or str(e)
    now = tray_now(frame, canvas_id, tray_id)
    if now is None:
        notes = [OBSERVE_UNKNOWN_NOTE] + ([OBSERVE_WHY.format(why=_clip(why, _OBSERVE_WHY_MAX))]
                                          if why else [])
        return None, notes
    return now, []


async def set_tray_filament(transport: Any, params: dict,
                            *, timeout_s: float | None = None) -> Cc2TrayResult:
    """Gate-read the machine state and the roster, then send the one 2003 with ``params``, once, and
    report what came back — or report, truthfully, that nothing readable came back. ``timeout_s``
    overrides the documented wait
    for tests; nothing in this call is ever repeated, the gate reads (the machine's 1002, then the
    roster's 2005) are its only reads before the
    command, and the only extra read it may cause afterwards is the one 2005 ``_observe_quietly`` takes
    after an accepted or unanswered command. A machine that is not idle for a job — or whose state
    cannot be read — refuses here exactly as it refuses the preview, before any 2003 exists.

    The classification is the job controls' bar, applied to this write: an explicit integer
    ``error_code`` 0 is acceptance, any other integer is the printer declining on record (kept, and
    earning no follow-up read), and a missing, bool, float or text code is an ambiguity the caller
    resolves by reading the roster deliberately — never here, where a second 2003 would be tempting.
    Where the accepted command and the following look disagree about the name, the type or the colour
    of that tray, the disagreement is said out loud in its own note, together with the one thing that
    makes it survivable: the read may lag. Whatever the outcome, the reply still carries
    ``verified_on_this_printer: false`` — the printer answering 0 is the printer not refusing, and
    what it stored for the tray is a separate question with a separate test."""
    params = checked_frame(params)
    wait_s = SET_WAIT_S if timeout_s is None else timeout_s
    sent = dict(params)
    # No command at all until the printer calls itself idle for a job and then names the tray.
    now, _machine = await _gate(transport, params)

    def base(status: ControlStatus, **over) -> dict:
        d = {"method": METHOD_SET_FILAMENT_INFO, "sent": sent, "waited_s": wait_s,
             "status": status, "tray_now": now}
        d.update(over)
        return d

    try:
        reply = await transport.request(METHOD_SET_FILAMENT_INFO, sent, timeout_s=wait_s)
    except Cc2TimeoutError:
        notes = [job.LOST_NOTE.format(verb=VERB, wait=wait_s)]
        observed, lost_notes = await _observe_quietly(transport, sent["canvas_id"], sent["tray_id"])
        return Cc2TrayResult(**base(ControlStatus.AMBIGUOUS, observed=observed,
                                    notes=tuple(notes + lost_notes)))
    except PrinterError as e:
        # The dead-session note is the longest of the shared templates, so the room left inside the
        # reply cap for the printer's own words about why is what is left, clipped with a visible
        # ellipsis: the fact that it ended is in the note, and the status beside it already says
        # ambiguous, so nothing is invented and nothing is silently cut.
        room = job.NOTE_CAP - len(job.DEAD_NOTE.format(verb=VERB))
        why = _clip(getattr(e, "message", None) or str(e), room)
        return Cc2TrayResult(**base(ControlStatus.AMBIGUOUS,
                                    notes=(job.DEAD_NOTE.format(verb=VERB) + why,)))

    # ``error_code``: only an explicit integer answers. No code at all, or one that cannot be read, is
    # an ambiguity the caller resolves by reading the roster deliberately — never here, where it would
    # tempt a second command (the same bar cc2_start, cc2_control and cc2_canvas hold to).
    if not isinstance(reply, dict) or "error_code" not in reply:
        detail = "carried no error code at all"
        return Cc2TrayResult(**base(ControlStatus.AMBIGUOUS,
                                    notes=(job.UNREADABLE_NOTE.format(verb=VERB, detail=detail),)))
    code = reply["error_code"]
    if not job.plain_int(code):
        detail = f"carried an error code that cannot be read ({type(code).__name__})"
        return Cc2TrayResult(**base(ControlStatus.AMBIGUOUS,
                                    notes=(job.UNREADABLE_NOTE.format(verb=VERB, detail=detail),)))
    if code != 0:
        return Cc2TrayResult(**base(
            ControlStatus.REFUSED, error_code=code,
            notes=(job.refusal_note(VERB, code), job.NO_OBSERVE_AFTER_REFUSAL)))

    observed, extra_notes = await _observe_quietly(transport, sent["canvas_id"], sent["tray_id"])
    notes = [job.ACCEPT_BASE.format(verb=VERB), _ACCEPT_TAIL, IDLE_GATE]
    contra = _contradiction_note(sent, observed)
    if contra is not None:
        notes.append(contra)
    notes += extra_notes
    return Cc2TrayResult(**base(ControlStatus.ACCEPTED, accepted=True, observed=observed,
                                notes=tuple(notes)))
