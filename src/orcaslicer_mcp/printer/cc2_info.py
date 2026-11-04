"""The three CC2 reads that answer what the status frame cannot: what the printer **is** (method
1001 GET_ATTRIBUTES), what one of its files **is** (1046 GET_FILE_DETAIL) and what that file
**looks like** (1045 GET_FILE_THUMBNAIL). All three are reads and all three are asked exactly once
— the transport's honest timeout stays honest, nothing here resends, and no method of any kind
other than these three leaves this module.

Every wire fact below is the printer's own sources agreeing on it, not an inference from a similar
protocol. ``docs/CC2_PROTOCOL.md`` (the community manual derived from Elegoo's own SDK) gives the
methods table — 1001 takes *no* parameters, 1045 takes ``{"storage_media": "local", "file_name":
"..."}``, 1046 takes ``{"storage_media": "local", "filename": "..."}`` — and its file-operations
chapter carries the measured 1044/1045/1046 replies. Elegoo's own LAN web client, the one bundled on
the printer, agrees field for field: ``getSystemInfo`` sends ``{method: 1001}`` with nothing else,
``getFileThumbnail`` sends ``params: {storage_media, file_name}`` and reads ``result.thumbnail``,
``getFileDetail`` sends ``params: {storage_media, filename}`` (plus an optional ``dir`` this adapter
never sends, because sub-directory semantics have never been asked of this printer) and reads
``filename``/``print_time``/``layer``/``size``/``create_time``/``total_filament_used``/
``total_print_times``/``last_print_time``/``color_map``. The Home Assistant integration's CC2 client
sends the same three shapes and maps the 1001 fields one by one, which is the second independent
witness for the attribute names.

**The trap these three methods share is the parameter spelling.** 1045 wants ``file_name`` and 1046
wants ``filename``; the protocol manual records having published the wrong one for 1045 until the
working client corrected it, and says plainly that the file methods do not share a spelling (1047 is
sent ``file_path`` by the same web client, which is a third shape and is not implemented here).
Nothing is normalised between them: ``thumbnail_params`` builds ``file_name``, ``detail_params``
builds ``filename``, and a test pins both spellings so a future "consistency" cleanup has to break a
test to introduce the bug again.

``storage_media`` is fixed to ``"local"`` and there is no argument for the other stores. The measured
capture of 1045/1046 says it used ``local``, "the only value tried", with ``sdcard`` and ``udisk``
"offered as variants but not yet run" — and the sources do not even spell those variants the same
way (the web client's MQTT enum is ``local``/``u-disk``/``sd-card``, its HTTP enum is
``local``/``udisk``/``sdcard``, the manual's table lists ``local``/``u-disk``/``sd-card``). Asking
for a removable volume is a request nobody has seen this firmware answer, so guessing a spelling for
it would be inventing a wire shape; ``u-disk`` and ``sd-card`` refuse with those words, before a
request exists.

Names are held to the bar ``cc2_list_files`` already prints by — non-empty, no path separators in
either dialect, no control characters, at most 255 characters — plus ``.`` and ``..``, which are not
file names at all. It is deliberately the *same* bar and no stricter, because a name that listing
shows must be a name these two tools can be pointed at; it is also why neither tool can be used to
poke at the printer's filesystem above its own file store: a request carrying a path is refused here,
not sent and interpreted by the printer. Nothing in this module touches the local filesystem — these
methods talk about files on the printer and nowhere else.

**Nothing is assumed about a field that did not arrive.** For 1001 every field is optional, because
the one real capture of this printer's attributes reports ``mcu_version`` and ``soc_version`` empty,
and an empty string is not a version: absent, empty and unreadable all read as None and are also
named in ``not_reported``, so a model cannot mistake "the printer said nothing" for "the printer has
no camera". A 1001 (or 1046) reply that carries *none* of the documented fields is refused outright —
an all-null profile presented as a result would be the invention this adapter exists to avoid, and
the Home Assistant client's own "no enrichment markers" branch shows the same reply shape happens.
For 1046 the layer count arrives under up to three names (``layer``, ``TotalLayers``,
``total_layer`` — the manual's own variation table, read the same way by the integration); whichever
reads cleanly is used, the name that answered is reported in ``layers_field``, and two names that
contradict each other are said out loud instead of one being quietly preferred. A 1046 whose reported
``filename`` is not the name asked for is reported that way too, with both names: an answer about a
different file must never be read as an answer about this one.

**1045's base64 is decoded, verified and then bounded, never truncated.** The manual documents the
field as raw base64 (not a data URI) holding "typically PNG" data; the web client puts the value
straight into a ``data:image/png;base64,`` URL and the integration defends against a value that
already starts with ``data:``, so this module tolerates a data-URI prefix and embedded line breaks
and strictly decodes everything else (``validate=True`` — a lenient decode would silently drop
characters and hand back a corrupt picture). What comes out is classified by its own magic bytes,
because the official client labels every thumbnail ``image/png`` whatever it is holding: PNG, JPEG,
GIF and WebP are named from their signature and anything else refuses rather than being served under
a type nobody established. The decoded image is capped at ``MAX_THUMBNAIL_BYTES`` and an image over
the cap is **refused with its size named, never cut down** — a half an image is a corrupt image, and
this reply carries the image inline in one MCP message, so the cap protects the conversation, not the
printer. The cap is checked against the wire length before decoding (so an enormous reply is refused
without a second allocation) and against the decoded byte count after it.

A reply whose ``thumbnail`` field is absent or empty is not an empty image either: the manual records
that a file with no embedded thumbnail may omit the field, and the integration logs the same case.
That is reported as "this file has no thumbnail the printer can show", with the slicer that writes
them named as the reason, and it is a different sentence from the printer declining (a nonzero
``error_code``, kept — the measurements show one firmware answering 1003 for 1045 on one file in an
otherwise working session, and nobody has established what 1003 means per-file on this firmware, so
it is never renamed "file not found").

The envelope gate and the colour rule are the ones every other CC2 read uses, imported from
``cc2_state`` rather than restated: what an absent/zero/nonzero/``"x"`` ``error_code`` means, and what
counts as a colour, must not be able to drift between the file listing and the file detail. Timeouts
are the transport's own default 10 s, as for 1002/2005/1044; the web client's 5 s budget for these
methods is its own UI's patience, and 10 s is the same longer-of-the-two rule the peripheral
controls record, so no method here is given a bespoke number.

Privacy: none of the three methods takes a host, a token or an environment variable — the transport
is the pinned session — and the replies carry no credential, no address and no local filesystem path.
The attributes reply carries the printer's own reported serial, which ``cc2_status`` already reports,
and deliberately not its ``ip`` or ``mac``: nothing here addresses the printer (the address is the
one you configured), and a MAC is a permanent hardware identifier with no task to perform in this
server. What the reported serial *is* good for is the cross-check ``fetch_attributes`` makes: the
session is pinned to one discovered device, so an attributes reply naming a different serial is said
out loud in ``identity`` rather than passed off as the printer you configured."""
from __future__ import annotations

import base64
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# The envelope gate and the colour pattern are shared with the other CC2 reads on purpose: the rules
# "what an error_code means" and "what a colour is" must not be able to drift between 1044 and these
# three methods, and cc2_state is where this package wrote them down first.
from .cc2_state import Cc2DataError, _HEX_RE, _check_envelope

METHOD_ATTRIBUTES = 1001       # GET_ATTRIBUTES — no params, the printer describes itself
METHOD_FILE_THUMBNAIL = 1045   # GET_FILE_THUMBNAIL — {"storage_media", "file_name"}
METHOD_FILE_DETAIL = 1046      # GET_FILE_DETAIL    — {"storage_media", "filename"}

# The only storage media whose 1045/1046 request has ever been answered by a real printer. The other
# two are refused by name, with the two spellings the sources use, rather than guessed at.
STORAGE_LOCAL = "local"
_OTHER_MEDIA = ("u-disk", "udisk", "sd-card", "sdcard")

_FILENAME_MAX = 255            # the same ceiling cc2_state accepts a listed name at

# An image this server will put into one MCP message. 2 MiB decoded (≈2.7 MB of base64 on the wire)
# is far above any slicer thumbnail this file format carries, and it is a limit on the conversation,
# not on the printer: over it the tool refuses with the size named, because a truncated image is a
# corrupt image and a silently-shrunk reply would be a lie about the picture.
MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024
# The same ceiling measured on the base64 text — the exact base64 length of a MAX_THUMBNAIL_BYTES
# image, ceil(bytes/3)*4 — so an oversized reply is refused before it is decoded into a second
# allocation. It is the cheap twin of the byte guard, not a replacement for it: an image one byte over
# the cap can encode to exactly this many characters, and it is the decoded length that refuses it.
_MAX_THUMBNAIL_CHARS = -(-MAX_THUMBNAIL_BYTES // 3) * 4

# Magic bytes -> the image type this server is willing to name, i.e. the four an MCP image block and
# the SDK's Image helper can both describe. Chosen from the bytes because the official web client
# stamps every thumbnail image/png whatever it received, and a wrong type is a claim.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


# --------------------------------------------------------------------------- field readers


def _text(value: Any) -> str | None:
    """A documented text field: text with something in it, or nothing at all. This firmware is
    measured sending empty strings for versions it does not track (the real capture reported
    ``mcu_version`` and ``soc_version`` empty), and an empty string is not a version number — it is
    the absence ``not_reported`` exists to name. Surrounding space is not content either."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _whole(value: Any) -> int | None:
    """A documented integer count or size: a plain non-negative int, else unknown. A bool is an int
    in Python and is not a memory size; a float, a string of digits and a negative number are not
    counts either, and none of them is rounded or coerced into an answer."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _flag(value: Any) -> bool | None:
    """A documented boolean, or unknown: ``True``/``False``, or the 1/0 the CC2 is known to send in
    some places for some flags. Anything else — 2, ``"yes"``, ``1.0``, absent — is unknown, never
    ``False``, because "this reply did not tell me" and "no" are different answers."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def _moment(value: Any) -> datetime | None:
    """A unix timestamp as the tools report it, or None: absent, a bool, a float, a negative number
    or a number no calendar holds all read as no time (0 among them, which the protocol uses for
    "never" — the caller decides whether 0 means absent or means never and says so in words)."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    with contextlib.suppress(ValueError, OSError, OverflowError):
        return datetime.fromtimestamp(value, tz=UTC)
    return None


def _stamp(value: Any) -> int | None:
    """A unix timestamp kept as the number the printer wrote, for callers that need 0 to mean the
    documented "never printed" rather than to read as a date."""
    return _whole(value)


def _unanswered(parsed: dict, keys: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    """The documented fields this reply left the reader with nothing for, named by their wire names
    and sorted. The rule is the parsers' own: every reader above answers ``None`` for absence,
    emptiness and an unreadable value alike, so a field that arrived as ``"yes"`` where a boolean
    belongs is exactly as unreported as one that never arrived — and a ``0`` or a ``False`` is an
    answer. ``not_reported`` is what lets a null in a reply be read as silence rather than as a fact
    about the printer."""
    return tuple(sorted(wire for wire, attr in keys if parsed.get(attr) is None))


def _nothing_answered(missing: tuple[str, ...], keys: tuple[tuple[str, str], ...]) -> bool:
    """Did the reply leave *every* documented field unanswered? That is a failure, not a profile."""
    return len(missing) == len(keys)


def _refuse(method: int, why: str, reason: str, **details) -> Cc2DataError:
    return Cc2DataError("protocol_error",
                        f"The printer's {method} reply {why}.", reason=reason, method=method,
                        **details)


def _no_documented_field(method: int, result: dict) -> Cc2DataError:
    """A reply that answered none of the documented fields. The message names the keys it *did*
    carry (keys, never values) because a firmware that renamed its fields is the way that happens,
    and the next person needs the names to notice."""
    seen = sorted(str(key) for key in result)[:12]
    return _refuse(method,
                   "carried none of the documented fields — it answered nothing a printer profile "
                   f"or a file detail is made of, only the key(s) {', '.join(seen)}",
                   "reply_empty",
                   hint="Nothing was assumed in its place. cc2_status reads the live state by "
                        "different fields; if the field you need is missing there too, this "
                        "firmware may name it differently than the protocol manual documents.")


def checked_lookup_name(value: Any, *, label: str = "file name") -> str:
    """The printer's own file name, exactly as given: refused unless it is a plain name of the kind
    ``cc2_list_files`` prints. No separators in either dialect, no control characters, no leading or
    trailing space, 1-255 characters, and not ``.``/``..``. The bar is the listing's bar on purpose:
    what the listing can show, these tools must accept — and a name carrying a path is refused here
    instead of being sent for the printer to interpret."""
    if not isinstance(value, str):
        raise _name_reject(value, label, "is not text")
    if not value:
        raise _name_reject(value, label, "is empty")
    if value != value.strip():
        raise _name_reject(value, label, "has leading or trailing space")
    if value in (".", ".."):
        raise _name_reject(value, label, "is a directory entry, not a file name")
    if "/" in value or "\\" in value:
        raise _name_reject(value, label, "is not a single plain name (it carries a path separator)")
    if len(value) > _FILENAME_MAX:
        raise _name_reject(value, label, f"is longer than {_FILENAME_MAX} characters")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise _name_reject(value, label, "carries control characters")
    return value


def _name_reject(value: Any, label: str, why: str) -> Cc2DataError:
    shown = value if isinstance(value, str) else type(value).__name__
    return Cc2DataError(
        "protocol_error",
        f"The {label} {str(shown)[:80]!r} {why}, so it is not a file the printer's own listing "
        "could show. Nothing was sent.",
        hint="Run cc2_list_files and pass one of its names unchanged — the printer's names are "
             "compared exactly, case and all, and a path is never what it wants.",
        reason="name_rejected")


def checked_storage(value: Any) -> str:
    """``local`` and nothing else, refused before a request exists. The other volumes are named in
    the refusal with both spellings the sources use, because the honest sentence is that nobody has
    seen this printer answer them (the measured 1045/1046 capture says ``local`` was "the only value
    tried"), not that they do not exist."""
    if isinstance(value, str) and value == STORAGE_LOCAL:
        return STORAGE_LOCAL
    spelling = (f" {str(value)[:24]!r} is not a storage this server asks for," if isinstance(value, str)
                else " that is not a storage this server asks for")
    raise Cc2DataError(
        "protocol_error",
        f"Only the printer's internal storage ('local') has a verified 1045/1046 request shape;"
        f"{spelling} and no request was sent.",
        hint="cc2_list_files lists what the printer's internal storage holds, and these tools read "
             "that store. Removable media (the sources spell it 'u-disk'/'udisk' and "
             "'sd-card'/'sdcard' without agreeing) have never been asked of this firmware, so this "
             "server does not guess at the spelling it would want.",
        reason="storage_unsupported")


# --------------------------------------------------------------------------- attributes (1001)

# The documented attributes fields, as (wire name, attribute). Every one is optional: the manual's
# sample carries them all and the one real capture of this printer already lacked two of them.
_TEXT_FIELDS: tuple[tuple[str, str], ...] = (
    ("machine_model", "model"),
    ("hostname", "name"),
    ("sn", "serial"),
    ("protocol_version", "protocol_version"),
    ("hardware_version", "hardware_version"),
    ("resolution", "resolution"),
    ("xyz_size", "xyz_size"),
    ("network_type", "network_type"),
)
_VERSION_FIELDS: tuple[tuple[str, str], ...] = (
    ("ota_version", "ota_version"),
    ("mcu_version", "mcu_version"),
    ("soc_version", "soc_version"),
)
_FLAG_FIELDS: tuple[tuple[str, str], ...] = (
    ("camera_connected", "camera_connected"),
    ("usb_connected", "usb_connected"),
)
_COUNT_FIELDS: tuple[tuple[str, str], ...] = (
    ("video_connections", "video_connections"),
    ("max_video_connections", "max_video_connections"),
    ("remaining_memory", "remaining_memory_bytes"),
)
# Wire name -> attribute for every documented field, with the nested versions named by their path so
# ``not_reported`` says which block was silent. Its size is the bar for ``_no_documented_field``: a
# reply that left every one of these unanswered is not a profile, it is a failure being reported as
# one result among many.
_ANSWER_KEYS: tuple[tuple[str, str], ...] = (
    tuple(_TEXT_FIELDS) + tuple((f"software_version.{key}", attr)
                                for key, attr in _VERSION_FIELDS)
    + tuple(_FLAG_FIELDS) + tuple(_COUNT_FIELDS))
ATTRIBUTE_FIELDS: tuple[str, ...] = tuple(key for key, _ in _ANSWER_KEYS)


@dataclass(frozen=True)
class Cc2PrinterAttributes:
    """What the printer says it is (method 1001), field by field, with no field invented. Every
    value is None when the printer said nothing — absent, empty (the measured empty ``mcu_version``
    case) or unreadable all read the same way — and ``not_reported`` names them again so a null can
    never be read as a fact about the printer. ``raw`` is the reply as it arrived: read it on
    purpose, never by dumping it. ``session_serial``/``serial_agree`` are the cross-check against the
    serial this MQTT session was pinned to at discovery; ``agree`` is None when either side is
    unknown and False when the reply describes a different device, which is worth a human reading
    before anything else is believed about this printer."""
    model: str | None = None
    name: str | None = None
    serial: str | None = None
    protocol_version: str | None = None
    hardware_version: str | None = None
    ota_version: str | None = None
    mcu_version: str | None = None
    soc_version: str | None = None
    resolution: str | None = None
    xyz_size: str | None = None
    network_type: str | None = None
    camera_connected: bool | None = None
    usb_connected: bool | None = None
    video_connections: int | None = None
    max_video_connections: int | None = None
    remaining_memory_bytes: int | None = None
    not_reported: tuple[str, ...] = ()
    session_serial: str | None = None
    serial_agree: bool | None = None
    raw: dict = field(default_factory=dict, compare=False)

    @classmethod
    def from_result(cls, result: Any, *, session_sn: str | None = None) -> "Cc2PrinterAttributes":
        """Parse a 1001 result. The reply must be an object and must not be a refusal; then every
        documented field is read if present and left None if not. A reply that answered none of them
        is refused — see ``_no_documented_field`` — because an all-null profile is a failure being
        reported as a result."""
        if not isinstance(result, dict):
            raise _refuse(METHOD_ATTRIBUTES, "was not an object at all", "attributes_malformed")
        _check_envelope(result, METHOD_ATTRIBUTES)
        sw = result.get("software_version")
        if not isinstance(sw, dict):
            sw = {}       # the nesting the manual documents; without it, versions are simply unknown
        fields: dict[str, Any] = {}
        for key, attr in _TEXT_FIELDS:
            fields[attr] = _text(result.get(key))
        for key, attr in _VERSION_FIELDS:
            fields[attr] = _text(sw.get(key))
        for key, attr in _FLAG_FIELDS:
            fields[attr] = _flag(result.get(key))
        for key, attr in _COUNT_FIELDS:
            fields[attr] = _whole(result.get(key))
        missing = _unanswered(fields, _ANSWER_KEYS)
        if _nothing_answered(missing, _ANSWER_KEYS):
            raise _no_documented_field(METHOD_ATTRIBUTES, result)
        serial = fields.get("serial")
        session = _text(session_sn)
        return cls(not_reported=tuple(sorted(missing)), session_serial=session,
                   serial_agree=(None if not serial or not session else serial == session),
                   raw=dict(result), **fields)

    def as_dict(self) -> dict:
        """The tool's reply: grouped for a person to read, with the protocol's own field names as the
        leaves so a claim maps back to the wire without a translation table. Keys the printer did
        not answer are present and null, and named again in ``not_reported``. ``ip`` and ``mac`` are
        absent by decision, not by omission — see the module docstring."""
        out = {
            "printer": {"model": self.model, "name": self.name, "serial": self.serial},
            "firmware": {"ota_version": self.ota_version, "mcu_version": self.mcu_version,
                         "soc_version": self.soc_version},
            "hardware": {"hardware_version": self.hardware_version,
                         "protocol_version": self.protocol_version},
            "build": {"resolution": self.resolution, "xyz_size": self.xyz_size,
                      "remaining_memory_bytes": self.remaining_memory_bytes},
            "network": {"type": self.network_type},
            "attachments": {"camera_connected": self.camera_connected,
                            "usb_connected": self.usb_connected,
                            "video_connections": self.video_connections,
                            "max_video_connections": self.max_video_connections},
            "identity": {"session_serial": self.session_serial, "serial_agree": self.serial_agree},
            "not_reported": list(self.not_reported),
        }
        if self.serial_agree is False:
            out["identity"]["warning"] = (
                "the printer's own reply names a different serial than the one this session "
                "connected and pinned, so this answer may not describe the printer you configured")
        return out


# --------------------------------------------------------------------------- file detail (1046)

# The documented 1046 fields as (wire name, attribute) — the attributes' rule again: what the reply
# left as None is what the printer did not answer, and a reply that answered none of them is refused
# rather than reported as an empty detail. ``layer`` stands for all three of its spellings;
# ``color_map`` counts the filament list, and an empty list is silence (the printer named no filament)
# rather than a claim that the file uses no tools.
_DETAIL_ANSWER_KEYS: tuple[tuple[str, str], ...] = (
    ("filename", "filename"), ("size", "size_bytes"), ("layer", "total_layers"),
    ("print_time", "print_seconds"), ("total_filament_used", "filament_grams"),
    ("create_time", "created"), ("last_print_time", "last_print_time"),
    ("total_print_times", "total_print_times"), ("color_map", "_filaments_answered"))

# The layer count's documented spellings, in the order this module prefers them: ``layer`` is the
# one in the measured reply, ``TotalLayers`` the manual's standard name, ``total_layer`` its third.
_LAYER_FIELDS: tuple[str, ...] = ("layer", "TotalLayers", "total_layer")


def detail_params(filename: str, storage: str = STORAGE_LOCAL) -> dict:
    """The 1046 params exactly: ``storage_media`` and **``filename``** — the un-underscored spelling,
    which is not the spelling 1045 wants. No ``dir``, no ``path``, no page: the optional ``dir`` the
    web client sends has never been given meaning on this printer."""
    return {"storage_media": checked_storage(storage), "filename": checked_lookup_name(filename)}


def thumbnail_params(filename: str, storage: str = STORAGE_LOCAL) -> dict:
    """The 1045 params exactly: ``storage_media`` and **``file_name``** — the underscored spelling,
    which is not the spelling 1046 wants. The manual records publishing this one wrong until
    Elegoo's own working client was read, so it is spelled out here and pinned by a test."""
    return {"storage_media": checked_storage(storage), "file_name": checked_lookup_name(filename)}


def _filaments(result: dict, *, name_for_say: str) -> tuple[dict | None, ...]:
    """The ``color_map`` the listing and the detail share, read by the listing's rules: a bare hex
    colour gets its ``#``, a value that is not a colour refuses the reply, and ``t`` is the G-code
    **tool** index (``T0``, ``T1``), not a CANVAS tray — the manual says so twice and the two are
    easy to confuse, so the reply calls it ``tool`` and never ``tray``. An entry that is not an
    object, or a map that is not a list, refuses the reply: half a colour map is not a colour map."""
    raw = result.get("color_map")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _refuse(METHOD_FILE_DETAIL,
                      f"sent a colour map that is not a list ({type(raw).__name__})",
                      "detail_malformed")
    out: list[dict | None] = []
    for item in raw:
        if not isinstance(item, dict):
            raise _refuse(METHOD_FILE_DETAIL,
                          f"sent a colour map entry that is not an object "
                          f"({type(item).__name__}) for {name_for_say}",
                          "detail_malformed")
        # An unreadable or negative tool index is no tool index, and no tool index is never tool 0.
        tool = _whole(item.get("t"))
        color = item.get("color")
        if isinstance(color, str) and color.strip():
            text = color.strip()
            if not text.startswith("#"):
                text = f"#{text}"
            if not _HEX_RE.fullmatch(text):
                raise _refuse(METHOD_FILE_DETAIL,
                              f"sent colour {color[:24]!r} for {name_for_say}, which is not a "
                              "colour — refusing to half-read a file it describes",
                              "detail_malformed")
            color = text
        else:
            color = None
        out.append({"tool": tool, "color": color, "name": _text(item.get("name"))})
    return tuple(out)


@dataclass(frozen=True)
class Cc2FileDetail:
    """One file on the printer, as method 1046 answers it. ``requested`` is the name this tool was
    given and ``filename`` the name in the reply; when the printer answers about a different file
    ``agree`` is False and the reply says so — a detail read is only about the file you asked for if
    the printer said which file it read. Every value is None when the printer said nothing, and
    ``not_reported`` names them. ``layers_field`` records which of the three documented spellings
    supplied ``total_layers``, because a firmware that answers under a second name is exactly what
    the manual warns about, and ``layers_conflict`` is that warning's evidence: the values it saw."""
    requested: str
    filename: str | None = None
    size_bytes: int | None = None
    total_layers: int | None = None
    layers_field: str | None = None
    layers_conflict: tuple[tuple[str, int], ...] = ()
    print_seconds: int | None = None
    filament_grams: float | None = None
    created: datetime | None = None
    last_printed: datetime | None = None
    last_print_time: int | None = None
    total_print_times: int | None = None
    filaments: tuple[dict | None, ...] = ()
    not_reported: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict, compare=False)

    @classmethod
    def from_result(cls, result: Any, *, requested: str) -> "Cc2FileDetail":
        """Parse a 1046 result for the file named in ``requested``. The reply must be an object and
        not a refusal; then each documented field is read if present. Note what is *not* required:
        the upload path needs a byte count because it compares one, a detail read only informs, so a
        missing ``size`` is an absent field here rather than a refused reply — and a reply that
        answered none of the documented fields is refused outright."""
        if not isinstance(result, dict):
            raise _refuse(METHOD_FILE_DETAIL, "was not an object at all", "detail_malformed")
        _check_envelope(result, METHOD_FILE_DETAIL)

        layers_seen: list[tuple[str, int]] = []
        for key in _LAYER_FIELDS:
            value = _whole(result.get(key))
            if value is not None:
                layers_seen.append((key, value))
        grams = result.get("total_filament_used")
        if isinstance(grams, bool) or not isinstance(grams, (int, float)):
            grams = None
        filaments = _filaments(result, name_for_say=requested)
        parsed = {
            "filename": _text(result.get("filename")),
            "size_bytes": _whole(result.get("size")),
            "total_layers": layers_seen[0][1] if layers_seen else None,
            "print_seconds": _whole(result.get("print_time")),
            "filament_grams": None if grams is None else float(grams),
            "created": _moment(result.get("create_time")),
            "last_print_time": _stamp(result.get("last_print_time")),
            "total_print_times": _whole(result.get("total_print_times")),
            # an empty or absent filament list is silence, not "no colour": the same line in
            # not_reported either way, and no invented answer in either direction
            "_filaments_answered": filaments or None,
        }
        missing = _unanswered(parsed, _DETAIL_ANSWER_KEYS)
        if _nothing_answered(missing, _DETAIL_ANSWER_KEYS):
            # Nothing documented arrived at all — the same "no enrichment markers" reply the Home
            # Assistant client logs and does not cache. An all-null detail presented as a result would
            # be a failure wearing a profile. One field answered is an answer; the rest stay null and
            # named in not_reported.
            raise _no_documented_field(METHOD_FILE_DETAIL, result)
        distinct = {value for _, value in layers_seen}
        conflict = () if len(distinct) <= 1 else tuple(layers_seen)
        return cls(
            requested=requested,
            filename=parsed["filename"],
            size_bytes=parsed["size_bytes"],
            total_layers=parsed["total_layers"],
            layers_field=layers_seen[0][0] if layers_seen else None,
            layers_conflict=conflict,
            print_seconds=parsed["print_seconds"],
            filament_grams=parsed["filament_grams"],
            created=parsed["created"],
            last_printed=_moment(result.get("last_print_time")),
            last_print_time=parsed["last_print_time"],
            total_print_times=parsed["total_print_times"],
            filaments=filaments,
            not_reported=missing,
            raw=dict(result))

    def as_dict(self) -> dict:
        """The reply, in the same words ``cc2_list_files`` uses for the same facts (``name``,
        ``size_bytes``, ``layers``, ``est_print_s``, ``est_filament_g``, ``created``, ``colors``,
        ``tools``) so a model is not learning two vocabularies for one file, plus what only a detail
        read can say: what the printer called the file it read, which layer name it answered under,
        how many times it has printed and when. Absent fields are present and null, named again in
        ``not_reported``, and a disagreement is said in the open rather than resolved by preference."""
        out: dict = {
            "method": METHOD_FILE_DETAIL,
            "requested": self.requested,
            "name": self.filename,
            "name_matches_request": (None if self.filename is None
                                     else self.filename == self.requested),
            "size_bytes": self.size_bytes,
            "layers": self.total_layers,
            "layers_from_field": self.layers_field,
            "est_print_s": self.print_seconds,
            "est_filament_g": self.filament_grams,
            "created": (self.created.isoformat() if self.created else None),
            "total_print_times": self.total_print_times,
            "last_printed": (self.last_printed.isoformat() if self.last_printed else None),
            # 0 is the protocol's own word for "never printed", so it is said as words as well as
            # left as the number; an absent last_print_time stays unknown.
            "never_printed": (None if self.last_print_time is None else self.last_print_time == 0),
            "filaments": [dict(item) for item in self.filaments],
            "not_reported": list(self.not_reported),
        }
        warnings = []
        if self.layers_conflict:
            out["layers_conflict"] = [{"field": key, "value": value}
                                      for key, value in self.layers_conflict]
            warnings.append("the printer answered the layer count under more than one field name "
                            "and the numbers disagree: layers/layers_from_field carry the first "
                            "name listed in layers_conflict, and nothing here averaged them")
        if out["name_matches_request"] is False:
            warnings.append("the printer's file-detail reply names a different file than the one "
                            "asked for, so this detail may not be about the file you named")
        if warnings:
            out["warnings"] = warnings
        return out


# --------------------------------------------------------------------------- thumbnail (1045)


@dataclass(frozen=True)
class Cc2FileThumbnail:
    """A thumbnail the printer actually sent: the decoded bytes, the image type read from their own
    signature, and how much base64 it took to carry them. The bytes are here to be handed to one MCP
    image block and nothing else — they are never a string in a tool result, and never cut short."""
    filename: str
    data: bytes
    kind: str
    wire_chars: int

    def as_dict(self) -> dict:
        """Metadata only, never the image: this is the half that rides alongside the image block."""
        return {"method": METHOD_FILE_THUMBNAIL, "filename": self.filename,
                "image_kind": self.kind, "bytes": len(self.data),
                "base64_chars": self.wire_chars}


def image_kind(data: bytes) -> str | None:
    """The image type the bytes declare for themselves, or None when they declare none of the four
    this server will name. WebP is the odd one out and is matched on both halves of its signature
    (``RIFF`` … ``WEBP``), because a file that starts with RIFF is a container, not yet a picture."""
    if len(data) < 4:
        return None
    for magic, kind in _SIGNATURES:
        if data.startswith(magic):
            return kind
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    return None


def _no_thumbnail(filename: str, why: str) -> Cc2DataError:
    return Cc2DataError(
        "protocol_error",
        f"The printer sent no thumbnail for {filename[:_FILENAME_MAX]} — {why}",
        hint="A slicer writes the preview into the G-code file, and a file sliced without one has "
             "nothing for the printer to show. The file itself is fine: cc2_file_detail still "
             "reports its size, layers, time, filament and colours.",
        reason="no_thumbnail")


def _too_big(filename: str, *, bytes_: int | None = None,
             chars: int | None = None) -> Cc2DataError:
    """The one refusal that says a picture is too big to carry: the size is named, nothing is cut to
    fit it, and nothing is asked of the printer a second time."""
    over = (f"arrived as {chars} base64 characters, at least about {chars * 3 // 4} bytes"
            if chars is not None else f"is {bytes_} decoded bytes")
    return Cc2DataError(
        "protocol_error",
        f"The thumbnail for {filename[:_FILENAME_MAX]} {over}, over this server's "
        f"{MAX_THUMBNAIL_BYTES} byte limit for one image reply. Nothing was truncated and nothing "
        "was asked again.",
        hint="A tool reply carries an image inline, so this limit protects the conversation, not "
             "the printer. cc2_file_detail gives the same file's metadata in text with no image.",
        reason="thumbnail_too_large",
        bytes=bytes_, base64_chars=chars, max_bytes=MAX_THUMBNAIL_BYTES)


def decode_thumbnail(value: Any, *, filename: str) -> tuple[bytes, int]:
    """The ``thumbnail`` field, turned into bytes or into an honest refusal — never into a shorter
    image. (bytes, base64 characters consumed).

    Two tolerances are documented in the sources and taken; everything else is strict. A value that
    already carries a ``data:`` URI prefix is accepted with the prefix removed, because Elegoo's own
    Home Assistant client defends against exactly that (``if not thumbnail.startswith('data:')``),
    and embedded line breaks are removed, because whitespace is not part of a base64 body. What is
    left is decoded with ``validate=True``: a lenient decode silently *drops* characters the
    alphabet does not know, which would turn a damaged reply into a valid-looking corrupt picture
    rather than a refusal. Empty text and a non-text value are refusals with their own sentences, and
    the size cap is applied before decoding (on the base64 length) and again after it (on the bytes).
    """
    if value is None:
        raise _no_thumbnail(filename, "its thumbnail field was null")
    if not isinstance(value, str):
        raise _refuse(METHOD_FILE_THUMBNAIL,
                      f"sent a thumbnail that is not text ({type(value).__name__})",
                      "thumbnail_malformed")
    text = value.strip()
    if not text:
        raise _no_thumbnail(filename, "its thumbnail field was empty")
    if text[:5].lower() == "data:":
        _, comma, rest = text.partition(",")
        if not comma or not rest.strip():
            raise _refuse(METHOD_FILE_THUMBNAIL,
                          "sent a thumbnail that looks like a data: URI but carries no data after "
                          "its comma", "thumbnail_malformed")
        text = rest.strip()
    # Whitespace inside a base64 body is formatting, never content (line-wrapped encoders exist).
    text = "".join(text.split())
    if len(text) > _MAX_THUMBNAIL_CHARS:
        raise _too_big(filename, chars=len(text))
    try:
        data = base64.b64decode(text, validate=True)
    except ValueError:
        raise _refuse(
            METHOD_FILE_THUMBNAIL,
            f"sent a thumbnail that is not base64 ({len(text)} characters). It was not decoded "
            "partially or repaired",
            "thumbnail_malformed",
            hint="Read the printer's state with cc2_status and the file with cc2_file_detail; a "
                 "damaged image reply is the printer's own, not something this server repairs.") from None
    if not data:
        raise _no_thumbnail(filename, "its thumbnail field decoded to no image bytes at all")
    if len(data) > MAX_THUMBNAIL_BYTES:
        raise _too_big(filename, bytes_=len(data))
    return data, len(text)


def thumbnail_from_result(result: Any, *, filename: str) -> Cc2FileThumbnail:
    """A 1045 reply, read: envelope first (a refusal is the printer's own answer with its code, and
    it is never relabelled "file not found" — the measurements show this firmware answering 1003 for
    one file's 1045 while the same method worked for another), then the thumbnail, then its type from
    its own magic bytes rather than from the manual's 'typically PNG'."""
    if not isinstance(result, dict):
        raise _refuse(METHOD_FILE_THUMBNAIL, "was not an object at all", "thumbnail_malformed")
    _check_envelope(result, METHOD_FILE_THUMBNAIL)
    if "thumbnail" not in result:
        # The documented way a file with no embedded preview says so: the protocol records that such a
        # file may omit the field entirely (and that the same firmware may instead answer an error,
        # which is the other sentence, above). Neither is a blank image, and neither is "no file".
        raise _no_thumbnail(filename, "its reply carried no thumbnail field, which is how the "
                                      "protocol documents a file with no embedded preview saying so")
    data, chars = decode_thumbnail(result["thumbnail"], filename=filename)
    kind = image_kind(data)
    if kind is None:
        # Named by its own first bytes, because the honest sentence is "this is image data of a type
        # nobody has established for this printer", not a mime type borrowed from its neighbour. The
        # official web client stamps every thumbnail image/png whatever arrived; this one does not.
        raise _refuse(METHOD_FILE_THUMBNAIL,
                      f"sent {len(data)} bytes of image data that begin {data[:4].hex()!r}, which "
                      "is none of the image types this server will name (png, jpeg, gif, webp by "
                      "their own signatures). It was not sent on under a guessed type",
                      "thumbnail_type_unknown",
                      hint="cc2_file_detail still reports this file's size, layers, time, filament "
                           "and colours, and the printer's own app shows the preview. If this "
                           "firmware really does send another image type, that is evidence worth "
                           "recording rather than a type to guess at.")
    return Cc2FileThumbnail(filename=filename, data=data, kind=kind, wire_chars=chars)


# --------------------------------------------------------------------------- the three reads


async def fetch_attributes(transport: Any, *, timeout_s: float | None = None) -> Cc2PrinterAttributes:
    """Ask the printer what it is (method 1001, no parameters — the manual's table and Elegoo's own
    client both send it empty) exactly once, and read the answer with nothing assumed. The serial the
    reply names is compared with the serial this session is pinned to; disagreement is reported in
    ``identity``, not settled here."""
    result = await transport.request(METHOD_ATTRIBUTES, {}, timeout_s=timeout_s)
    return Cc2PrinterAttributes.from_result(result, session_sn=getattr(transport, "sn", None))


async def fetch_file_detail(transport: Any, filename: str, *, storage: str = STORAGE_LOCAL,
                            timeout_s: float | None = None) -> Cc2FileDetail:
    """Ask for one file's detail (1046) exactly once. The name is validated and used **exactly** as
    given — no case folding, no normalisation, no path joining — because that is the spelling the
    printer's own listing wrote and the spelling its answer is compared against. A nonzero
    ``error_code`` arrives as a refusal carrying the printer's code; this module does not decide what
    any particular code means for this firmware, and does not ask twice."""
    params = detail_params(filename, storage)
    result = await transport.request(METHOD_FILE_DETAIL, params, timeout_s=timeout_s)
    return Cc2FileDetail.from_result(result, requested=params["filename"])


async def fetch_file_thumbnail(transport: Any, filename: str, *, storage: str = STORAGE_LOCAL,
                               timeout_s: float | None = None) -> Cc2FileThumbnail:
    """Ask for one file's thumbnail (1045) exactly once and return it decoded, typed and within the
    size limit. Note the parameter this method wants: ``file_name``, not 1046's ``filename``."""
    params = thumbnail_params(filename, storage)
    result = await transport.request(METHOD_FILE_THUMBNAIL, params, timeout_s=timeout_s)
    return thumbnail_from_result(result, filename=params["file_name"])
