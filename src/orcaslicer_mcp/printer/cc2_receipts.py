"""Upload receipts: the private local proof that this adapter moved exactly these bytes to exactly
one printer, because a CC2 cannot be asked. Stock CC2 offers no way to read a file's content back
through the supported API, so a remote listing proves only that *a* file with that name and size
exists — not that its bytes are the ones uploaded, and not that some other client did not replace
it afterwards. That gap is closed from this side: a receipt binds the printer's serial, the exact
remote name, the printer's own reported size, and the SHA-256 and length of the bytes that were
actually sent, plus the settings parsed from those bytes and the moment of the upload, and a start
refuses without a matching one.

Privacy: a receipt carries no credentials, no access code or upload token, no G-code text, and no
raw printer replies — settings and hashes only. The store lives outside any repository, under the
user's own data dir (mode 700) with every receipt written via mkstemp and renamed into place (mode
600), so a crash or a full disk never leaves a half-written receipt behind — the same discipline
that writes the remembered printer file. Receipt files are named by a hash of (serial, name), so
the store can hold any filename the printer's own listing accepted without path games.

This module also owns the per-printer cross-process lock the upload and start operations share:
flock on a private lock file, refused rather than waited when someone else holds it, because the
point of the lock is that two agents never interleave printer checks and writes. It governs only
clients of this adapter — the printer has no idea who else is connected, and state can change
between a check and a command even inside the lock; that limit is the receipt's and the lock's
shared honesty, stated where callers will hit it.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .cc2_stock_templates import STOCK as _STOCK_TEMPLATES
from .errors import PrinterError

# Version 2 added ``extrusion_nozzle_temps``: the nozzle temperatures extrusion was verified to
# run at (each proven by an M109 wait at that exact target, in command order), which a start
# compares against the loaded spool's range. Version 3 added ``tools``: one entry per tool the
# file selects — its own material, first-layer and steady nozzle setpoints, the nozzle
# temperatures that tool's extrusion was verified at, and, apart from them, the tool's
# ``flush_nozzle_temps``: every accepted flush R (the multi-tool change's design amendment 9)
# the tool was left or entered at. A flush R is the vendor purge's declared range-high, which
# the flush really extrudes at but no M109 ever waits at, so it is no printing temperature; a
# start reports it against each tray's rated maximum instead. The flat ``material``/
# ``tool_index``/``nozzle_*`` fields keep the first tool's values, and the flat
# ``extrusion_nozzle_temps`` the file-wide verified union, for readers that know only those.
# This build writes version 3 and reads it fully; every v3 tool entry must carry
# ``flush_nozzle_temps``, empty or not. It reads version 2 as a one-tool receipt built from the
# flat fields, with no flush temperatures, keeping v2's stricter rule that every extrusion
# temperature is a commanded setpoint (a v2 file had no flush-R exception to explain one).
# Version 1 recorded only the indiscriminate commanded setpoints, which cannot tell a 140 °C
# non-extruding preheat from a cold extrusion; no version-1 receipt was ever written outside a
# test, and this build reads nothing else.
RECEIPT_VERSION = 3
ENV_RECEIPTS_DIR = "ORCA_CC2_RECEIPTS_DIR"
DEFAULT_DIR = Path.home() / ".local" / "share" / "orcaslicer-mcp" / "cc2-receipts"

# The same shapes the rest of the CC2 adapter accepts: a serial as discovery reports it, a name
# as the printer's own listing validator reports it. A receipt that claimed anything else was
# never written by this store, and reading it anyway would be guessing at somebody's file.
_SN_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_FILENAME_MAX = 255
_TEMP_MAX = 450.0
_MATERIAL_MAX = 64

_STORE_HINT = (f"Point {ENV_RECEIPTS_DIR} at a directory only you can write (chmod 700), or leave "
               "it unset for the default under ~/.local/share/.")


def _plain_name(value: object) -> str:
    """A filename as this store may key it: a plain name, never empty, never path-ish, never
    control. Mirrors the check the 1044 listing applies to the printer's own entries, because a
    receipt must key exactly the names the verification step can see."""
    if (not isinstance(value, str) or not value or value != value.strip()
            or "/" in value or "\\" in value or len(value) > _FILENAME_MAX
            or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)):
        raise PrinterError("protocol_error",
                           f"'{str(value)[:60]!r}' is not a plain printer filename, so it can "
                           "have no receipt.", reason="receipt_name_invalid")
    return value


def _checked_str(value: object, label: str, max_len: int = 4096) -> str:
    if (not isinstance(value, str) or not value or len(value) > max_len
            or any(ord(ch) < 0x20 or ch == 0x7F for ch in value)):
        raise PrinterError("protocol_error",
                           f"A receipt would record an invalid {label}, so it is refused rather "
                           "than half-trusted.", reason="receipt_invalid")
    return value


def _checked_temp(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PrinterError("protocol_error",
                           f"A receipt would record a non-numeric {label}, so it is refused.",
                           reason="receipt_invalid")
    v = float(value)
    if not (0.0 <= v <= _TEMP_MAX):
        raise PrinterError("protocol_error",
                           f"A receipt would record {label} of {v}, outside 0-{_TEMP_MAX:g} °C — "
                           "refusing a receipt this adapter could not have written.",
                           reason="receipt_invalid")
    return v


def _checked_temps(value: object, label: str) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise PrinterError("protocol_error",
                           f"A receipt would record {label} as something other than a list of "
                           "temperatures, so it is refused.", reason="receipt_invalid")
    return tuple(_checked_temp(v, f"one {label} entry") for v in value)


@dataclass(frozen=True)
class ToolReceipt:
    """What the receipt says about one of the file's own tools: the material it prints, the
    nozzle setpoints it prints at (first layer may legitimately differ from the rest, and both
    are kept), the nozzle temperatures that tool's extrusion was verified to run at — each one
    an M109 wait that tool ran behind, and the list a start compares against the tray that tool
    maps to — and, kept apart, ``flush_nozzle_temps``: every accepted flush R the tool was left
    or entered at (design amendment 9), a temperature the purge pushed through it without any
    wait, which a start reports against the tray's rated maximum rather than refusing on.
    The first entry of the receipt's ``tools`` is the tool the receipt's flat fields describe."""
    tool_index: int
    material: str
    nozzle_first_layer_temp: float
    nozzle_temp: float
    extrusion_nozzle_temps: tuple[float, ...]
    flush_nozzle_temps: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("extrusion_nozzle_temps", "flush_nozzle_temps"):
            value = getattr(self, field_name)
            # only a real list or tuple is normalised; None, a number, a string or anything
            # else stays as it is so _checked_temps refuses it below — ``value or ()`` once
            # turned a null into an empty list and let a receipt load with no temperatures at
            # all, which no range check would ever catch (review finding, PR #11)
            if isinstance(value, (list, tuple)):
                object.__setattr__(self, field_name, tuple(value))
        if (isinstance(self.tool_index, bool) or not isinstance(self.tool_index, int)
                or self.tool_index < 0):
            raise PrinterError("protocol_error",
                               "A receipt would name a tool that is not a whole number, so it is "
                               "refused.", reason="receipt_invalid")
        _checked_str(self.material, "material", _MATERIAL_MAX)
        _checked_temp(self.nozzle_first_layer_temp, "first-layer nozzle temperature")
        _checked_temp(self.nozzle_temp, "nozzle temperature")
        _checked_temps(self.extrusion_nozzle_temps, "extrusion nozzle temperatures")
        _checked_temps(self.flush_nozzle_temps, "flush nozzle temperatures")

    def to_dict(self) -> dict:
        return {
            "tool_index": self.tool_index,
            "material": self.material,
            "nozzle_first_layer_temp": self.nozzle_first_layer_temp,
            "nozzle_temp": self.nozzle_temp,
            "extrusion_nozzle_temps": list(self.extrusion_nozzle_temps),
            "flush_nozzle_temps": list(self.flush_nozzle_temps),
        }

    @classmethod
    def from_dict(cls, data: object) -> "ToolReceipt":
        """One tool entry of a version-3 receipt, failing closed at every gate like its parent:
        not an object, a missing field (``flush_nozzle_temps`` included: version 3 always writes
        it, so an entry without it was not written by this store) — anything else raises,
        because a tool entry a start compares a tray against cannot half-exist."""
        if not isinstance(data, dict):
            raise Cc2UploadReceipt._bad("carries a tool entry that is not an object")

        def need(key: str):
            if key not in data:
                raise Cc2UploadReceipt._bad(f"carries a tool entry missing {key!r}")
            return data[key]

        return cls(tool_index=need("tool_index"),
                   material=need("material"),
                   nozzle_first_layer_temp=need("nozzle_first_layer_temp"),
                   nozzle_temp=need("nozzle_temp"),
                   extrusion_nozzle_temps=need("extrusion_nozzle_temps"),
                   flush_nozzle_temps=need("flush_nozzle_temps"))


@dataclass(frozen=True)
class Cc2UploadReceipt:
    """The whole private record of one successful upload. Every field is validated on the way in
    and on the way back from disk: a receipt this store refuses to build is a bug or a tampered
    file, never something a start is allowed to squint at. ``reported_size_bytes`` is the byte
    count the printer's own complete listing gave the finalised file; ``sent_bytes`` and
    ``content_sha256`` describe the bytes this adapter actually sent — when the two sizes
    disagree the receipt cannot exist, but a later listing move is the start's problem to catch.
    ``commanded_nozzle_setpoints`` keeps every non-zero setpoint the file itself commanded —
    including transient flush/standby steps, which is why a start checks the first-layer and
    steady setpoints against a spool range and treats the rest as evidence, not as targets.
    ``extrusion_nozzle_temps`` is the start's other half: the nozzle temperatures extrusion was
    verified to run at file-wide (the union over the file's tools, first order seen) — each one
    proven by an M109 wait at that temperature in command order. An accepted flush R, the vendor
    change-filament purge's own declared range-high, is not among them: it lives in each tool's
    ``flush_nozzle_temps`` (design amendment 9). So a non-extruding preheat (a 140 °C flush warm-up before the 260 °C wait) never
    reads as a printing temperature, and every temperature that must sit inside the loaded
    spool's range does. A verified wait is always a commanded setpoint. The rule below still
    admits, for a version-3 receipt, an extrusion temperature that is no commanded setpoint when
    every tool printing below it carries it (the shape task 1.3 accepted while a flush R was
    still recorded as an extrusion temperature); this build no longer writes one. A receipt
    carrying an extrusion temperature neither story explains was not written by this store, and one carrying more of them only ever makes a
    start refuse more trays, never fewer — that direction is stated here because it is the
    whole reason the loosened rule costs nothing. ``tools`` carries one ``ToolReceipt`` per
    tool the file selects, in the parse's first-seen order; the flat ``material``,
    ``tool_index`` and nozzle fields are that first entry's values, and a v3 receipt whose flat
    fields and first tool disagree is not one this store wrote.

    ``printer_model``, ``vendor_templates`` and ``templates_compared`` are the parse's own
    upload-time facts (the stock-profile allow list's designs 1 and 5), carried on the receipt so
    the upload reply can say them: which model the file named, whether its five machine-level
    G-code templates are the shipped stock ones, and how many of the five the file actually
    carried. They are NOT part of the on-disk shape — ``to_dict`` never writes them and
    ``from_dict`` never reads them — because the receipt on disk is the proof about the file that
    sits on the printer, while a template verdict is a fact about the parse that ran a moment
    ago, which a later start must not read as though the file had been re-checked. A receipt
    loaded back from the store therefore always carries all three as ``None``; only the receipt
    object the upload just returned carries values. Neither the receipt format nor its version
    changes for them, so an upload's reply and a start's proof stay exactly as they were."""
    printer_sn: str
    remote_filename: str
    reported_size_bytes: int
    content_sha256: str
    sent_bytes: int
    material: str
    nozzle_first_layer_temp: float
    nozzle_temp: float
    bed_first_layer_temp: float
    bed_temp: float
    commanded_nozzle_setpoints: tuple[float, ...]
    commanded_bed_setpoints: tuple[float, ...]
    extrusion_nozzle_temps: tuple[float, ...]
    tool_index: int
    uploaded_at: datetime
    printer_display: str | None = None
    tools: tuple[ToolReceipt, ...] = ()
    # the upload-only facts (design 1's model gate verdict and design 5's template report), read
    # by the upload reply and never written to disk — see the class docstring
    printer_model: str | None = None
    vendor_templates: str | None = None
    templates_compared: int | None = None

    def __post_init__(self) -> None:
        # normalise before validating: a receipt read back from JSON carries lists where the
        # in-memory type keeps tuples, and the two spellings must be the one receipt
        for field_name in ("commanded_nozzle_setpoints", "commanded_bed_setpoints",
                            "extrusion_nozzle_temps", "tools"):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name) or ()))
        if not isinstance(self.printer_sn, str) or not _SN_RE.fullmatch(self.printer_sn):
            raise PrinterError("protocol_error",
                               "A receipt would name a printer by something other than the serial "
                               "discovery reported, so it is refused.", reason="receipt_invalid")
        _plain_name(self.remote_filename)
        for n, label in ((self.reported_size_bytes, "reported size"), (self.sent_bytes, "sent length")):
            if isinstance(n, bool) or not isinstance(n, int) or n <= 0:
                raise PrinterError("protocol_error",
                                   f"A receipt would record {label} that is not a positive byte "
                                   "count, so it is refused.", reason="receipt_invalid")
        if (not isinstance(self.content_sha256, str)
                or not _HEX64_RE.fullmatch(self.content_sha256)):
            raise PrinterError("protocol_error",
                               "A receipt without a full 64-digit SHA-256 would bind nothing, so "
                               "it is refused.", reason="receipt_invalid")
        _checked_str(self.material, "material", _MATERIAL_MAX)
        for temp, label in ((self.nozzle_first_layer_temp, "first-layer nozzle temperature"),
                            (self.nozzle_temp, "nozzle temperature"),
                            (self.bed_first_layer_temp, "first-layer bed temperature"),
                            (self.bed_temp, "bed temperature")):
            _checked_temp(temp, label)
        nozzle_cmds = _checked_temps(self.commanded_nozzle_setpoints, "nozzle setpoints")
        _checked_temps(self.commanded_bed_setpoints, "bed setpoints")
        extrusion = _checked_temps(self.extrusion_nozzle_temps, "extrusion nozzle temperatures")
        # the tools: given, the first entry must be exactly what the flat fields say (the parse
        # writes both halves from the same first tool, so a receipt where they disagree was
        # assembled by someone else); omitted, the flat fields describe exactly one tool, and
        # that single entry is built from them rather than trusted to be added by hand — which
        # is also how a version-2 receipt read from disk gets its one tool.
        if not self.tools:
            object.__setattr__(self, "tools", (ToolReceipt(
                tool_index=self.tool_index, material=self.material,
                nozzle_first_layer_temp=self.nozzle_first_layer_temp,
                nozzle_temp=self.nozzle_temp, extrusion_nozzle_temps=extrusion),))
        else:
            for entry in self.tools:
                if not isinstance(entry, ToolReceipt):
                    raise PrinterError("protocol_error",
                                       "A receipt would record one of its tools as something "
                                       "other than a tool entry, so it is refused.",
                                       reason="receipt_invalid")
            if len({tool.tool_index for tool in self.tools}) != len(self.tools):
                raise PrinterError("protocol_error",
                                   "A receipt would name the same tool twice, which no read of "
                                   "one file could write, so it is refused.",
                                   reason="receipt_invalid")
            first = self.tools[0]
            if (first.tool_index != self.tool_index or first.material != self.material
                    or float(first.nozzle_first_layer_temp) != float(self.nozzle_first_layer_temp)
                    or float(first.nozzle_temp) != float(self.nozzle_temp)):
                raise PrinterError("protocol_error",
                                   "A receipt's flat material and nozzle fields would not be "
                                   "its first tool's values, so it is refused rather than "
                                   "half-trusted.", reason="receipt_invalid")
        # The flat ``extrusion_nozzle_temps`` IS the union of the tools' lists, in both directions
        # (review finding, PR #11, both rounds): a tool temperature the flat list lacks, or a flat
        # temperature no tool ran at, is a receipt that disagrees with itself — the start compares
        # trays against the per-tool lists, so a flat-only temperature would never be checked.
        # And every extrusion temperature must be a commanded M104/M109 setpoint: since design
        # amendment 9 the parse keeps flush R in ``flush_nozzle_temps`` and writes only verified
        # waits here, and a version-2 receipt reads as one tool with exactly the flat list, so
        # no shipped receipt needs the looser rule task 1.3 once allowed.
        per_tool_union = {t for tool in self.tools for t in tool.extrusion_nozzle_temps}
        if per_tool_union != set(extrusion):
            raise PrinterError("protocol_error",
                               "A receipt's file-wide extrusion temperatures would not be the "
                               "union of its tools' lists, so it is refused rather than "
                               "half-trusted.", reason="receipt_invalid")
        recorded = list(dict.fromkeys(
            list(extrusion) + [t for tool in self.tools for t in tool.extrusion_nozzle_temps]))
        unexplained = [temp for temp in recorded if temp not in nozzle_cmds]
        if unexplained:
            raise PrinterError("protocol_error",
                               f"A receipt would claim extrusion at "
                               f"{', '.join(f'{t:g}' for t in unexplained[:4])} °C, temperatures "
                               "no commanded setpoint names and no flush every tool printing "
                               "below them carried — a receipt the parser could not have written "
                               "is refused.", reason="receipt_invalid")
        if (isinstance(self.tool_index, bool) or not isinstance(self.tool_index, int)
                or self.tool_index < 0):
            raise PrinterError("protocol_error",
                               "A receipt would name a tool that is not a whole number, so it is "
                               "refused.", reason="receipt_invalid")
        if not isinstance(self.uploaded_at, datetime) or self.uploaded_at.tzinfo is None:
            raise PrinterError("protocol_error",
                               "A receipt without a timezone-aware upload time could not be "
                               "called fresh or stale, so it is refused.", reason="receipt_invalid")
        if self.printer_display is not None:
            _checked_str(self.printer_display, "printer display name", 255)
        # the upload-only facts are validated on the way in like every other field: a receipt
        # built with a nonsense verdict in it is refused rather than half-trusted. Nothing reads
        # them back, so the only writer of a non-None value is the parse that just ran, and a
        # count outside the templates this build knows is a receipt it could not have written.
        if self.printer_model is not None:
            _checked_str(self.printer_model, "printer model", 255)
        if self.vendor_templates is not None:
            _checked_str(self.vendor_templates, "stock-template verdict", 255)
        if self.templates_compared is not None:
            if (isinstance(self.templates_compared, bool)
                    or not isinstance(self.templates_compared, int)
                    or not 0 <= self.templates_compared <= len(_STOCK_TEMPLATES)):
                raise PrinterError(
                    "protocol_error",
                    f"A receipt would say {self.templates_compared!r} stock templates were "
                    f"compared, which no read of one file could report (this build knows "
                    f"{len(_STOCK_TEMPLATES)}), so it is refused.", reason="receipt_invalid")

    # ---- the on-disk shape ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Plain JSON data. Field order here is the order written; nothing that never belongs in
        a receipt (tokens, codes, replies) has a field to arrive in — and neither do the three
        upload-only facts (``printer_model``, ``vendor_templates``, ``templates_compared``), which
        describe the parse that just ran rather than the file on the printer, so they live only in
        the receipt object the upload returns and never on disk."""
        return {
            "version": RECEIPT_VERSION,
            "printer_sn": self.printer_sn,
            "printer_display": self.printer_display,
            "remote_filename": self.remote_filename,
            "reported_size_bytes": self.reported_size_bytes,
            "content_sha256": self.content_sha256,
            "sent_bytes": self.sent_bytes,
            "material": self.material,
            "nozzle_first_layer_temp": self.nozzle_first_layer_temp,
            "nozzle_temp": self.nozzle_temp,
            "bed_first_layer_temp": self.bed_first_layer_temp,
            "bed_temp": self.bed_temp,
            "commanded_nozzle_setpoints": list(self.commanded_nozzle_setpoints),
            "commanded_bed_setpoints": list(self.commanded_bed_setpoints),
            "extrusion_nozzle_temps": list(self.extrusion_nozzle_temps),
            "tool_index": self.tool_index,
            "tools": [tool.to_dict() for tool in self.tools],
            "uploaded_at": self.uploaded_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Cc2UploadReceipt":
        """Rebuild one receipt from disk, failing closed at every gate: unknown version, missing
        field, wrong type, an out-of-range number — all raise, because a start may only stand on
        a receipt this very code would have written."""
        if not isinstance(data, dict):
            raise cls._bad("was not an object at all")
        version = data.get("version")
        if isinstance(version, bool) or version not in (2, RECEIPT_VERSION):
            raise cls._bad(f"carries version {version!r}, which this build does not read")

        def need(key: str):
            if key not in data:
                raise cls._bad(f"is missing {key!r}")
            return data[key]

        raw_ts = need("uploaded_at")
        if not isinstance(raw_ts, str):
            raise cls._bad("carries an upload time that is not a string")
        try:
            uploaded_at = datetime.fromisoformat(raw_ts)
        except ValueError:
            raise cls._bad("carries an upload time it cannot read") from None
        display = data.get("printer_display")
        tools: tuple[ToolReceipt, ...] = ()
        if version == RECEIPT_VERSION:
            # a version-3 receipt names its tools; reading one without them would be guessing
            # which of the entries the flat fields describe were never written down
            raw_tools = need("tools")
            if (isinstance(raw_tools, (str, bytes)) or not isinstance(raw_tools, (list, tuple))
                    or not raw_tools):
                raise cls._bad("carries a tools list that is not a non-empty list of tool "
                               "entries")
            tools = tuple(ToolReceipt.from_dict(entry) for entry in raw_tools)
        receipt = cls(
            printer_sn=need("printer_sn"),
            remote_filename=need("remote_filename"),
            reported_size_bytes=need("reported_size_bytes"),
            content_sha256=need("content_sha256"),
            sent_bytes=need("sent_bytes"),
            material=need("material"),
            nozzle_first_layer_temp=need("nozzle_first_layer_temp"),
            nozzle_temp=need("nozzle_temp"),
            bed_first_layer_temp=need("bed_first_layer_temp"),
            bed_temp=need("bed_temp"),
            commanded_nozzle_setpoints=need("commanded_nozzle_setpoints"),
            commanded_bed_setpoints=need("commanded_bed_setpoints"),
            extrusion_nozzle_temps=need("extrusion_nozzle_temps"),
            tool_index=need("tool_index"),
            uploaded_at=uploaded_at,
            tools=tools,
            printer_display=display if display is None or isinstance(display, str) else None)
        if version == 2:
            # a version-2 file predates the flush-R exception: every extrusion temperature it
            # recorded was a commanded setpoint, and a v2 dict claiming otherwise is not one
            # the code that wrote v2 receipts could ever have written
            commanded = set(receipt.commanded_nozzle_setpoints)
            if rogue := [f"{t:g}" for t in receipt.extrusion_nozzle_temps if t not in commanded]:
                raise cls._bad(f"carries version 2 extrusion at {', '.join(rogue[:4])} °C, "
                               "temperatures its own commanded nozzle setpoints never commanded")
        return receipt

    @staticmethod
    def _bad(why: str) -> PrinterError:
        return PrinterError("protocol_error",
                            f"The stored upload receipt {why}, so it is refused rather than "
                            "trusted — the upload must be redone deliberately.",
                            reason="receipt_invalid")


class Cc2ReceiptStore:
    """The private directory receipts and printer locks live in, and nothing else does. Root
    resolution is explicit path, then ``ORCA_CC2_RECEIPTS_DIR``, then the user's data dir — the
    default is never inside a repository. Every path that touches disk goes through here, so the
    'outside the repo, 700/600, atomic' promises live in one place."""

    def __init__(self, root: str | os.PathLike[str] | None = None, env: dict[str, str] | None = None):
        env = os.environ if env is None else env
        raw = str(root).strip() if root is not None else (env.get(ENV_RECEIPTS_DIR) or "").strip()
        self.root = Path(raw) if raw else DEFAULT_DIR

    @property
    def receipts_dir(self) -> Path:
        return self.root / "receipts"

    @property
    def locks_dir(self) -> Path:
        return self.root / "locks"

    # ---- directories, private on principle ----------------------------------------------------

    def _private_dir(self, path: Path) -> Path:
        """Create (or accept) one private directory of this store, tightened to 700 whatever the
        umask said. A root that exists as anything else — a file, a symlink — is refused: this
        store's whole privacy claim is its path, so it is not followed anywhere."""
        try:
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
            st = path.stat()
        except OSError as e:
            raise PrinterError("not_configured",
                               f"The receipt store at '{path}' cannot be used ({e.strerror or e}).",
                               hint=_STORE_HINT) from None
        if path.is_symlink() or not stat.S_ISDIR(st.st_mode):
            raise PrinterError("not_configured",
                               f"The receipt store path '{path}' is not a real directory, so "
                               "nothing is written under it.", hint=_STORE_HINT)
        if st.st_mode & 0o077:
            os.chmod(path, 0o700)
        return path

    def _ensure(self, path: Path) -> Path:
        """Root first, then one of its private subdirectories — so no receipt or lock is ever
        written before the store has checked (and tightened) every directory above it."""
        self._private_dir(self.root)
        return self._private_dir(path)

    def path_for(self, printer_sn: str, remote_filename: str) -> Path:
        """Where one receipt lives: keyed by a hash of (serial, name) so any filename the
        listing accepted fits without the store ever interpreting it as a path."""
        if not isinstance(printer_sn, str) or not _SN_RE.fullmatch(printer_sn):
            raise PrinterError("protocol_error",
                               "A receipt is keyed by the serial discovery reported; this one is "
                               "not that.", reason="receipt_invalid")
        _plain_name(remote_filename)
        key = hashlib.sha256(f"{printer_sn}\n{remote_filename}".encode()).hexdigest()
        return self.receipts_dir / f"{key}.json"

    def save(self, receipt: Cc2UploadReceipt) -> Path:
        """Write one receipt atomically at mode 600: mkstemp beside the target (it opens 600 and
        is ours alone from the first byte), write, fsync, rename. An interrupted save unlinks its
        temp and raises — a half-written receipt never reaches a reader, and the caller that
        already uploaded tells Nathan so in plain words rather than retrying the printer."""
        path = self.path_for(receipt.printer_sn, receipt.remote_filename)
        self._ensure(self.receipts_dir)
        payload = json.dumps(receipt.to_dict(), ensure_ascii=True, indent=1)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=self.receipts_dir, prefix="receipt.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
                os.fchmod(f.fileno(), 0o600)
            os.replace(tmp, path)
            tmp = None
        except OSError as e:
            raise PrinterError("not_configured",
                               f"The receipt for '{receipt.remote_filename}' could not be "
                               f"written to '{path}' ({e.strerror or e}).",
                               hint=_STORE_HINT) from None
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return path

    def load(self, printer_sn: str, remote_filename: str) -> Cc2UploadReceipt | None:
        """The receipt for one printer and name, or None when there simply is none. Anything
        else — a file that is not readable JSON, an object this code would not have written, a
        receipt filing itself under someone else's slot — raises: absent is a fact a start may
        report, corrupt is a trust failure it must not walk past."""
        path = self.path_for(printer_sn, remote_filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except ValueError:
            raise PrinterError("protocol_error",
                               f"The stored upload receipt at '{path}' is not readable JSON, so "
                               "it is refused rather than trusted — the upload must be redone "
                               "deliberately.", reason="receipt_invalid") from None
        except OSError as e:
            raise PrinterError("protocol_error",
                               f"The stored upload receipt at '{path}' cannot be read "
                               f"({e.strerror or e}), so this file cannot be started.",
                               hint=_STORE_HINT) from None
        receipt = Cc2UploadReceipt.from_dict(data)
        if receipt.printer_sn != printer_sn or receipt.remote_filename != remote_filename:
            raise PrinterError("protocol_error",
                               f"The stored upload receipt at '{path}' names "
                               f"'{receipt.printer_sn}'/'{receipt.remote_filename}', not the "
                               "printer and file it is filed under, so it is refused.",
                               reason="receipt_mismatch")
        return receipt

    # ---- the per-printer lock -------------------------------------------------------------------

    @contextmanager
    def lock(self, printer_sn: str):
        """Hold the one upload-or-start lock for one printer. flock, non-blocking: held means
        somebody else is mid-operation on this printer and this call refuses at once instead of
        queueing — the checks an upload makes and the receipt it writes are only coherent end to
        end, and waiting would pretend otherwise. Cross-process by construction (flock is on the
        file, and the lock survives as long as the fd), released on every path, including the
        exceptions that pass through it. This governs clients of this adapter only: other
        software talks to the printer directly and keeps no promises to us."""
        if not isinstance(printer_sn, str) or not _SN_RE.fullmatch(printer_sn):
            raise PrinterError("protocol_error",
                               "A printer lock is keyed by the serial discovery reported; this "
                               "one is not that.", reason="lock_key_invalid")
        self._ensure(self.locks_dir)
        path = self.locks_dir / f"{hashlib.sha256(printer_sn.encode()).hexdigest()[:32]}.lock"
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.fchmod(fd, 0o600)
        except OSError as e:
            raise PrinterError("not_configured",
                               f"The printer lock at '{path}' cannot be opened "
                               f"({e.strerror or e}).", hint=_STORE_HINT) from None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                    raise PrinterError(
                        "not_reachable",
                        f"Another upload or start already holds the lock for printer "
                        f"'{printer_sn}'; this operation refused rather than queueing behind it, "
                        "and touched nothing.", reason="locked") from None
                raise
            yield path
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
