"""Direct CC2 upload: one sliced G-code file, moved once, in 1 MiB HTTP chunks, to the one printer
this adapter is configured to reach — and then proven on the printer's own complete listing before
any receipt is believed. The wire is the one the working direct upload used, reverse-engineered
from Elegoo's own ``elegoo-link`` SDK (and confirmed by pycentauri's reading of it): ``PUT
http://<pinned-ip>:80/upload``, the raw chunk as an octet-stream body, and the headers
``Content-Range: bytes <start>-<end>/<total>`` (inclusive), ``X-File-Name`` (the destination),
``X-File-MD5`` — the whole file's MD5, sent with every chunk because Elegoo's protocol mandates
MD5; it is not a security use here — and ``X-Token``, the printer's access code, or the documented
no-access-code value only in the mode discovery itself reported as having no code. Success is
HTTP 200 with a JSON object whose ``error_code`` is 0; an absent error code or anything else —
another status, an unparseable body, a nonzero code — is the printer not saying
yes, which is a no. The replies' offset-like fields were never observed to mean anything on this
printer and the SDK docs give them no required acknowledgment, so none is treated as one here:
nothing invented, nothing assumed.

What a chunk means for the whole transfer: every chunk is sent exactly once, ever. No failed,
rejected or unanswered chunk is retried, because a chunk that got no clean yes may have landed —
resending guesses at the printer's state, and this adapter's rule is that the human decides. On
any stop a partial file may sit on the printer under the destination name (the CC2 protocol
offers no delete); the message says so, and the untouched name stays claimable only by a human
decision, never by automation.

Immutability, because the receipt promises the printer holds the bytes it hashes: the source file
is hashed from one buffered open (MD5 and SHA-256 before the first byte leaves), the send pass
re-reads and re-hashes the same offsets through a second, unbuffered descriptor, and any
divergence — the file shrinking or growing mid-send, its content changing under the reader —
stops the upload before the final listing or receipt, naming what changed. The bytes sent are the bytes hashed by construction of
the send pass; the closing hash comparison turns a raced read into a refusal instead of a false
receipt. The destination name is refused unless it is a plain ASCII ``.gcode`` basename — no
separators, no control characters, no dotfiles, nothing that is not a single honest filename —
and an existing file of any size under that name stops the operation before anything is sent:
never overwrite, never while the file listing is incomplete, never after an ambiguous one.

The file itself is read as evidence, not trusted metadata: ``parse_gcode_settings`` takes the
material, the first-layer and steady nozzle and bed setpoints, and the tool index from the
commands the file actually issues cross-checked against its own slicer config block — accepting
the legitimate case of a first layer hotter than the rest, refusing a file whose config promises
temperatures its G-code never commands, and refusing anything multi-tool until this adapter can
speak multi-tool. Only the opened bytes are ever parsed; nothing about the file is inferred from
its name.

Heater commands are read in the order the file issues them, because what a purge runs at is a
fact of order, not of contents. The parser carries the current nozzle target and the value of
the most recent ``M109`` wait: a non-extruding preheat (the observed ``M104 S140`` warm-up flush
hold before the ``M109 S260``) is legal merely as a preheat; every extrusion — conservatively,
any G0/G1 move that commands E and every ``M6211`` purge, whatever that Elegoo command's own
unread parameters mean — is refused unless the most recent verified ``M109`` waited at the very
target now in force. A target changed after a wait (``M104`` to a new value) revives the
requirement: only another matching ``M109`` re-arms extrusion. A nozzle cooldown (``S0``) before
the file's last extrusion contradicts itself — the filament path is cold while the file still
means to extrude — and is refused; the end-block cooldown after the last extrusion is the normal
ending it is. No printing-temperature range is checked here, at all: what a spool tolerates is
the CANVAS tray's answer at start, and the verified extrusion temperatures ride on the receipt
so that start can make that comparison. A heater command this parser cannot pin to one
unambiguous setpoint — no S, two S, a second numbered parameter like ``R200`` beside the S, a
value no heater in this family could hold — is ambiguity about what the printer would do, and
ambiguity is refused, never guessed at.

Privacy: the upload token is an ``AccessCode`` (the transport's redacting credential type) that
lives only in request headers; no message, error or receipt echoes it, and no G-code text or raw
reply leaves this module in any direction. The HTTP session is created per upload and closed on
every path, exactly like the MQTT session it rides beside.
"""
from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import os
import re
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from . import cc2_state
from .cc2_receipts import Cc2ReceiptStore, Cc2UploadReceipt
from .cc2_transport import AccessCode, _refusal
from .errors import PrinterError

# --- the wire, as the working upload and the SDK both drew it ------------------------------------

UPLOAD_PORT = 80                    # the plain-HTTP port the direct upload used
UPLOAD_PATH = "/upload"             # PUT; the SDK's one CC2 upload endpoint
CHUNK_SIZE = 1024 * 1024            # 1 MiB — what the working upload sent and Elegoo's per-chunk maximum
CHUNK_TIMEOUT_S = 180.0             # a whole file's worth of patience, per chunk
CONNECT_TIMEOUT_S = 3.0             # same reachability budget the read clients keep
MAX_SOURCE_BYTES = 256 * 1024 * 1024  # sanity line: three orders above the largest file this
                                      # printer is known to hold; above it, refuse, don't OOM
_HASH_BLOCK = 128 * 1024

_DEST_MAX = 255                     # the name ceiling the 1044 listing itself refuses beyond

_TOKEN_HINT = ("The upload token is the printer's access code — the same credential the MQTT "
               "session uses; pass cc2_transport.mqtt_password(discovery, config). On a LAN "
               "Only Mode printer with no access code the documented default applies; set "
               "ORCA_CC2_ACCESS_CODE (or a chmod-600 file via ORCA_CC2_ACCESS_CODE_FILE) to "
               "provide one.")
_RECEIPT_HINT = ("The receipt store holds the private proof a start needs; point "
                 "ORCA_CC2_RECEIPTS_DIR at a directory only you can write (chmod 700). The "
                 "upload itself is not repeated on this strength — decide deliberately whether "
                 "to re-upload under a new name.")


class Cc2UploadError(PrinterError):
    """An upload that stopped: the same codes every printer tool speaks, with a `reason` naming
    the kind (``collision``, ``source_changed``, ``upload_chunk_failed``, ``not_finalized``,
    ``size_mismatch``, ``http_failed``) so a caller can act without parsing messages. Nothing
    that raised this was retried."""


# --- the response, as small as honesty needs it ---------------------------------------------------


@dataclass(frozen=True)
class Cc2ChunkResponse:
    """One chunk's answer, reduced to what the protocol says success is made of: the HTTP status
    and the raw body bytes. Offset-flavoured fields a body may carry were never observed to mean
    anything on this printer — this adapter neither reads them nor requires them, because a
    semantics nobody proved is not a fact to code against."""
    status_code: int
    body: bytes


class Cc2UploadHttp:
    """The production HTTP session: base_url is the pinned private address only — built once by
    the upload after its own policy check — so no per-chunk request ever consults a name a
    rebinding could steer, and a literal address leaves no socket-level lookup behind either.
    Owns one httpx connection pool; ``aclose`` is mandatory and the upload calls it on every
    path."""

    def __init__(self, base_url: str):
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(CHUNK_TIMEOUT_S, connect=CONNECT_TIMEOUT_S))

    async def put_chunk(self, *, body: bytes, headers: dict[str, str]) -> Cc2ChunkResponse:
        resp = await self._http.put(UPLOAD_PATH, content=body, headers=headers)
        return Cc2ChunkResponse(status_code=resp.status_code, body=resp.content)

    async def aclose(self) -> None:
        await self._http.aclose()


# --- the file as evidence: settings parsing ---------------------------------------------------------

# Slicer config lines of this exact shape are what the printer-side filename the Elegoo fork
# builds from them, and what the direct-read reference slice carries at its tail:
#     ; nozzle_temperature_initial_layer = 260
#     ; filament_type = PETG
# The same key may repeat across config sections; repeats that disagree are an inconsistent file.
_CONFIG_RE = re.compile(r"^;\s*([a-z0-9_]+)\s*=\s?(.*?)\s*$")
# A tool-select line after comment stripping: a bare `T0` (the reference slice's only one). Not
# the `T0` inside `M6211 ... T0 ...` and never the `T[next_extruder]` of a config comment — the
# command grammar is line-initial, as the slicer writes it and Klipper parses it.
_TOOL_RE = re.compile(r"^T\s*([0-9]+)(?:\s+.*)?$")
# Sanity fences for commanded setpoints: generous hardware bounds, not material logic — the real
# range is the spool's, and that check is the start's. A value outside these is a broken or
# hostile file, and no CC2 heater that this adapter may serve reaches them.
_NOZZLE_MAX, _BED_MAX = 450.0, 200.0

# A second parameter that carries a number — ``R200`` beside ``M109 S260``, Marlin's ``B`` on an
# ``M190``, a timeout ``T`` on an ``M104``. Some printer dialects give those letters a second,
# different temperature of their own; which one the heater ends the print at is then a dialect
# question this adapter cannot answer from the file, so the command is refused instead of read
# half. A bare flag letter the reference file really carries — the ``A`` on its ``M190 S70 A`` —
# has no number after it and is not a second target; the refusal names this in the message.
_EXTRA_PARAM_RE = re.compile(r"[A-Za-z][-+.0-9]")

# The commands whose E parameter makes the move an extrusion; G-code spells moves G0 and G1,
# with and without the leading zero. Read conservatively: any E letterhead on the line counts,
# because a move whose extrusion cannot be ruled out is a move to hold behind a verified
# temperature wait.
_MOVE_HEADS = frozenset({"G0", "G00", "G1", "G01"})

# Which filament config keys carry the bed setpoints, by the plate the file says it prints on.
# Orca's own four plate families; a file naming a plate this map doesn't hold prints at a bed
# temperature this adapter cannot identify, which is a refusal, not a guess.
_PLATE_BED_KEYS = {
    "high temp plate": ("hot_plate_temp_initial_layer", "hot_plate_temp"),
    "cool plate": ("cool_plate_temp_initial_layer", "cool_plate_temp"),
    "textured pei plate": ("textured_plate_temp_initial_layer", "textured_plate_temp"),
    "engineering plate": ("eng_plate_temp_initial_layer", "eng_plate_temp"),
}


@dataclass(frozen=True)
class Cc2GcodeSettings:
    """What a file says about how it means to be printed, read only from the file's own bytes.
    The four print setpoints are what a start checks a spool against — first layer may
    legitimately differ from the rest, and both are kept. ``nozzle_setpoints`` /
    ``bed_setpoints`` are every non-zero setpoint the G-code commands, in first order seen:
    evidence that includes transient steps (the Elegoo start G-code's 140 °C flush hold before
    its ``M109 S260`` is observed and real), which is why they are reported apart from the print
    setpoints instead of silently widening them. ``extrusion_nozzle_temps`` are the nozzle
    targets extrusion was verified to run at — each proven in command order by an M109 wait at
    that exact temperature, kept in first order seen, and every one of them also a commanded
    setpoint — which is what a start compares against the loaded spool's range. Heater-off
    commands (S0) are cooldowns, not setpoints, and read out. ``tool_index`` exists only because
    exactly one tool was seen."""
    material: str
    nozzle_first_layer_temp: float
    nozzle_temp: float
    bed_first_layer_temp: float
    bed_temp: float
    tool_index: int
    nozzle_setpoints: tuple[float, ...]
    bed_setpoints: tuple[float, ...]
    extrusion_nozzle_temps: tuple[float, ...]
    bed_plate: str


def _refuse(what: str, hint: str | None = None) -> PrinterError:
    return PrinterError("protocol_error",
                        f"The G-code file cannot be uploaded through this workflow: {what}. "
                        "Nothing was sent to the printer.", hint=hint, reason="gcode_unreadable")


def _config_value(raw: str) -> list[str]:
    """One config value, split the way the slicer writes multi-slot lists — comma separated. A
    bracketed list is a slicer dump of the same list; read it the same way."""
    text = raw.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [piece.strip() for piece in text.split(",") if piece.strip()]


def _collect_config(lines: Iterable[str]) -> dict[str, list[str]]:
    """Every ``; key = value`` config line of interest, values kept in order of appearance.
    Comments carrying G-code inside config values (``machine_start_gcode`` templates and friends)
    are config values, never commands — commands are scanned only over comment-stripped lines,
    which is what keeps a template's ``M104`` from ever looking printed."""
    config: dict[str, list[str]] = {}
    for line in lines:
        m = _CONFIG_RE.match(line)
        if m and m.group(1) in _WANTED_KEYS:
            config.setdefault(m.group(1), []).append(m.group(2))
    return config


_WANTED_KEYS = frozenset({"filament_type", "nozzle_temperature", "nozzle_temperature_initial_layer",
                          "curr_bed_type", "hot_plate_temp_initial_layer", "hot_plate_temp",
                          "cool_plate_temp_initial_layer", "cool_plate_temp",
                          "textured_plate_temp_initial_layer", "textured_plate_temp",
                          "eng_plate_temp_initial_layer", "eng_plate_temp"})


def _config_single(config: dict[str, list[str]], key: str, label: str) -> str:
    """One value for one key: every occurrence must name the same thing, and no occurrence at all
    is a refusal naming the key — the design's 'reject missing' with the key left on the record."""
    occurrences = config.get(key)
    if not occurrences:
        raise _refuse(f"it carries no {label} (no '; {key} =' config line), and print settings "
                      "are read from the file, never guessed")
    values: list[str] = []
    for raw in occurrences:
        pieces = _config_value(raw)
        if not pieces:
            raise _refuse(f"its '{key} =' config line carries no value")
        for piece in pieces:
            if piece not in values:
                values.append(piece)
    if len(values) > 1:
        raise _refuse(f"its {label} is not one value but {values[:4]} — a file that lists more "
                      "than one cannot be identified with a single setting, and this adapter "
                      "supports exactly one tool")
    return values[0]


def _config_temp(config: dict[str, list[str]], key: str, label: str, *, maximum: float) -> float:
    raw = _config_single(config, key, label)
    try:
        value = float(raw)
    except ValueError:
        raise _refuse(f"its {label} is {raw!r}, which is not a temperature") from None
    if not 0.0 < value <= maximum:
        raise _refuse(f"its {label} is {value}, outside the 0–{maximum:g} °C a printer of this "
                      "family could hold — refusing rather than trusting it")
    return value


def _parse_S(token: str) -> float:
    if not token.startswith("S") or len(token) < 2:
        raise _refuse(f"a heater command names {token!r}, which is not an S<temperature> parameter")
    try:
        value = float(token[1:])
    except ValueError:
        raise _refuse(f"a heater command names S{token[1:]!r}, which is not a number") from None
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf: not a temperature
        raise _refuse("a heater command names a temperature that is not a finite number")
    return value


def parse_gcode_settings(lines: Iterable[str]) -> Cc2GcodeSettings:
    """Read one single-tool CC2 file's print settings from its own bytes, fail-closed.

    Commands first: only comment-stripped, line-initial G-code counts (the file's config block
    quotes start G-code templates full of M-codes; none of those were ever issued). The nozzle
    commands are M104/M109 and the bed's M140/M190; every non-zero S setpoint is kept in first
    order seen, S0 read out as the cooldown it is, and a heater command without a sane S is an
    unparseable file. The same scan walks the temperature commands **in order**, carrying the
    nozzle target and the most recent verified ``M109`` wait: every extrusion (G0/G1 with E, and
    every ``M6211`` purge) must find a wait at exactly the target in force, or the file is
    refused — which is what keeps the real 140 °C non-extruding preheat legal while no cold
    extrusion can slip through, and no printing-temperature range is hardcoded anywhere here.
    Tool selects must use exactly one tool number — none at all is missing tool
    metadata, more than one is multi-tool, and the ``T`` inside ``M6211`` is a parameter of the
    flush command, not a select.

    Config second: material, the plate's first-layer and steady bed temperatures, and the nozzle
    first-layer and steady setpoints, each refusing to be missing, ambiguous or unphysical.
    Cross-check last: every one of the four print setpoints must also appear among the commands —
    a file whose config promises 260 while its G-code never heats to 260 is an inconsistent file,
    and inconsistency is exactly what this parse exists to catch. Command setpoints beyond the
    print setpoints (the observed 140 °C flush hold) stay listed as evidence and never block:
    what the printer holds during a purge step is its own business, and the start is what checks
    print setpoints against spool ranges. The file's own declared filament ranges
    (``nozzle_temperature_range_*``) are deliberately ignored — the authority for a spool is the
    CANVAS tray the printer reports at start, not text in a file someone may have edited."""
    text_lines = list(lines)  # small files in tests, real files streamed by the upload; either way once
    config = _collect_config(text_lines)

    material = _config_single(config, "filament_type", "filament type")
    if (len(material) > 64 or not material.isascii()
            or not material.isprintable() or any(c.isspace() for c in material)):
        raise _refuse(f"its filament type {material[:20]!r} is not a plain printable word")

    plate_word = _config_single(config, "curr_bed_type", "build-plate type").lower()
    if plate_word not in _PLATE_BED_KEYS:
        raise _refuse(f"it prints on plate {plate_word!r}, whose bed-temperature config keys this "
                      "adapter does not know, so its bed setpoints cannot be identified")
    first_key, steady_key = _PLATE_BED_KEYS[plate_word]

    nozzle_first = _config_temp(config, "nozzle_temperature_initial_layer",
                                "initial-layer nozzle temperature", maximum=_NOZZLE_MAX)
    nozzle = _config_temp(config, "nozzle_temperature", "steady nozzle temperature",
                          maximum=_NOZZLE_MAX)
    bed_first = _config_temp(config, first_key, "initial-layer bed temperature", maximum=_BED_MAX)
    bed = _config_temp(config, steady_key, "steady bed temperature", maximum=_BED_MAX)

    nozzle_cmds: list[float] = []
    bed_cmds: list[float] = []
    tools: list[int] = []
    # the command-order story: the nozzle target in force, the value of the most recent M109
    # that actually waited there, the temperatures extrusion ran at, and where the file's own
    # extrusions and nozzle cooldowns fell — the last of these needed because an S0 is only
    # honest after the file is done extruding
    nozzle_target: float | None = None
    waited: float | None = None
    extrusion_temps: list[float] = []
    cooldown_at: list[int] = []
    last_extrusion_at: int | None = None
    for idx, raw_line in enumerate(text_lines):
        line = raw_line.split(";", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 1)
        head, rest = parts[0].upper(), (parts[1] if len(parts) > 1 else "")
        if head in ("M104", "M109", "M140", "M190"):
            tokens = rest.split()
            s_tokens = [t for t in tokens if t.startswith("S")]
            if len(s_tokens) != 1:
                raise _refuse(f"the command {line[:40]!r} does not carry exactly one S<temp> "
                              "parameter, so its setpoint cannot be trusted")
            value = _parse_S(s_tokens[0])
            limit = _NOZZLE_MAX if head in ("M104", "M109") else _BED_MAX
            if not 0.0 <= value <= limit:
                raise _refuse(f"the command {line[:40]!r} names {value}, outside any 0–{limit:g} "
                              "°C this printer family could hold")
            others = [t for t in tokens if t is not s_tokens[0]]
            if extra := [t for t in others if _EXTRA_PARAM_RE.search(t)]:
                raise _refuse(f"the command {line[:40]!r} carries {extra[0]!r} beside its S "
                              "setpoint; some dialects of this heater command give such a "
                              "parameter a second, different temperature, and which one the "
                              "heater settles at is not knowable from the file — bare flags "
                              "like the M190's A are fine, a letter with a number is not")
            bucket = nozzle_cmds if head in ("M104", "M109") else bed_cmds
            if value > 0.0 and value not in bucket:
                bucket.append(value)
            if head in ("M104", "M109"):  # bed commands need no order tracking: the extrusion
                if value == 0.0:          # gate below reads the nozzle's state only, and the
                    cooldown_at.append(idx)  # mid-file-S0 rule after the loop is a nozzle rule
                    nozzle_target, waited = 0.0, (0.0 if head == "M109" else waited)
                elif head == "M104":      # a setpoint without a wait — an allowed preheat, but
                    nozzle_target = value  #            the wait must still come before extrusion
                else:                     # M109: the target, and a verified wait at exactly it
                    nozzle_target = waited = value
            continue
        if head == "M6211":  # S carries the new-filament temperature for the purge
            last_extrusion_at = idx
            if nozzle_target is None:
                raise _refuse(f"the purge command {line[:40]!r} extrudes while no nozzle "
                              "temperature has even been commanded — a purge runs hot or not "
                              "at all, and which this file means is not knowable")
            if nozzle_target <= 0.0:
                raise _refuse(f"the purge command {line[:40]!r} extrudes at a nozzle target of "
                              f"{nozzle_target:g} °C — that is the heater off, and a purge "
                              "there would be a cold one")
            if waited != nozzle_target:
                raise _refuse(f"the purge command {line[:40]!r} extrudes at a commanded nozzle "
                              f"target of {nozzle_target:g} °C that no M109 was ever waited at "
                              "(the most recent verified wait was "
                              + (f"{waited:g} °C" if waited is not None else "no M109 at all")
                              + ") — heat and wait at the purge temperature first")
            purge_targets = [token for token in rest.split() if token.startswith("S")]
            if len(purge_targets) != 1 or _parse_S(purge_targets[0]) != nozzle_target:
                raise _refuse("the purge temperature is missing or differs from the waited nozzle target")
            if nozzle_target not in extrusion_temps:
                extrusion_temps.append(nozzle_target)
            continue
        if head in _MOVE_HEADS and any(t.upper().startswith("E") for t in rest.split()):
            where = f"the E move {line[:40]!r}"
            if nozzle_target is None:
                raise _refuse(f"{where} extrudes while no nozzle temperature has even been "
                              "commanded — refusing rather than guessing what the printer "
                              "would run it at")
            if nozzle_target <= 0.0:
                raise _refuse(f"{where} extrudes at a nozzle target of {nozzle_target:g} °C — "
                              "that is the heater off, and an extrusion there would be a cold one")
            if waited != nozzle_target:
                raise _refuse(f"{where} extrudes at a commanded nozzle target of "
                              f"{nozzle_target:g} °C that no M109 was ever waited at (the most "
                              "recent verified wait was "
                              + (f"{waited:g} °C" if waited is not None else "no M109 at all")
                              + ") — every extrusion must follow a verified M109 wait at the "
                              "temperature it runs at, and a target changed after the wait "
                              "needs another M109 at the new value")
            if nozzle_target not in extrusion_temps:
                extrusion_temps.append(nozzle_target)
            last_extrusion_at = idx
            continue
        m = _TOOL_RE.match(line.upper())  # Klipper matches command words case-insensitively
        if m:
            tool = int(m.group(1))
            if tool not in tools:
                tools.append(tool)
    if cooldown_at and last_extrusion_at is not None and min(cooldown_at) < last_extrusion_at:
        raise _refuse("it turns the nozzle off (S0) before the file's last extrusion — a "
                      "cooldown that early contradicts the extrusions still to come (a "
                      "non-extruding preheat below the printing temperature is fine; switching "
                      "the heater off mid-file is not)")
    if not tools:
        raise _refuse("it selects no tool at all (no T<n> line), so its single tool cannot be "
                      "identified — multi-tool and unattributed files are refused until this "
                      "adapter supports them independently")
    if len(tools) > 1:
        raise _refuse(f"it selects {len(tools)} tools ({', '.join(f'T{t}' for t in tools[:4])}); "
                      "this version supports exactly one file tool, mapped to one tray")

    for value, label in ((nozzle_first, "initial-layer nozzle temperature"),
                         (nozzle, "steady nozzle temperature")):
        if value not in nozzle_cmds:
            raise _refuse(f"its config promises a {label} of {value:g} °C but no M104/M109 "
                          "command ever sets it — the file's config and its commands disagree")
    for value, label in ((bed_first, "initial-layer bed temperature"), (bed, "steady bed "
                                                                             "temperature")):
        if value not in bed_cmds:
            raise _refuse(f"its config promises a {label} of {value:g} °C but no M140/M190 "
                          "command ever sets it — the file's config and its commands disagree")
    return Cc2GcodeSettings(material=material,
                            nozzle_first_layer_temp=nozzle_first, nozzle_temp=nozzle,
                            bed_first_layer_temp=bed_first, bed_temp=bed, tool_index=tools[0],
                            nozzle_setpoints=tuple(nozzle_cmds), bed_setpoints=tuple(bed_cmds),
                            extrusion_nozzle_temps=tuple(extrusion_temps),
                            bed_plate=plate_word)


def parse_gcode_text(text: str) -> Cc2GcodeSettings:
    """The same read from a whole text; the upload streams lines instead. Lossy decodes leave
    replacement characters that no sane command survives, so a corrupt file fails the parse
    rather than sneaking through as noise."""
    return parse_gcode_settings(text.splitlines())


# --- destination and address policy ----------------------------------------------------------------


def validate_destination(name: Any) -> str:
    """The basename a CC2 may be asked to hold, checked to the byte: a plain ASCII single name
    ending in ``.gcode``, no separators in either dialect, no control characters (CR/HS among
    them, because this name is going into an HTTP header and a printer path at once), no dotfile
    tricks, at most 255 characters, and at least one character of actual name. Everything the
    1044 listing refuses to report is refused here before anything is asked."""
    if not isinstance(name, str):
        raise PrinterError("protocol_error", "A destination file name must be given as text.",
                           reason="destination_invalid")
    if not name or name != name.strip():
        raise _dest_reject(name, "be empty or carry leading/trailing space")
    if len(name) > _DEST_MAX:
        raise _dest_reject(name, f"be longer than {_DEST_MAX} characters")
    if not name.isascii():
        raise _dest_reject(name, "contain non-ASCII characters (header and listing names stay ASCII)")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        raise _dest_reject(name, "contain control characters")
    if "/" in name or "\\" in name:
        raise _dest_reject(name, "carry a path separator — the destination is one basename")
    if ";" in name:
        raise _dest_reject(name, "carry a semicolon — the field separator this printer family's "
                                 "own file formats think in, and no slicer writes one")
    if name.startswith("."):
        raise _dest_reject(name, "begin with a dot")
    if not name.endswith(".gcode") or name == ".gcode":
        raise _dest_reject(name, "be a .gcode file name")
    return name


def _dest_reject(name: str, why: str) -> PrinterError:
    return PrinterError("protocol_error",
                        f"The destination file name {name[:60]!r} would {why}; refusing before "
                        "anything is sent rather than letting the printer interpret it.",
                        hint="Give one plain basename ending in .gcode, for example "
                             "part-flat-petg-2026-11-04.gcode.", reason="destination_invalid")


def checked_upload_base_url(host: str) -> str:
    """The one URL an upload may speak to, from the pinned address the transport already pinned.
    It must be an IP literal — the literal the policy judged — so no resolver stands between
    this check and the socket, and it must pass the very same private/loopback refusal the MQTT
    session was held to (public, multicast, unspecified, link-local, broadcast: all refused). A
    name — even one that once resolved to the printer — would put a lookup back on the path
    between the policy decision and the packet, which is precisely the rebinding door the
    transport shut; it stays shut here."""
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]  # the bracket form of an IPv6 literal; the brackets are not the address
    shown = host or "<unset>"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise PrinterError("not_configured",
                           f"The upload target {shown!r} is not an IP literal, and uploads go "
                           "only to the pinned literal address of the configured printer — a "
                           "name would put a resolver back on the path.",
                           reason="unsafe_address") from None
    if why := _refusal(ip):
        raise PrinterError("not_configured",
                           f"The upload target {shown} is a {why} address; this adapter uploads "
                           "only to the private LAN or loopback address of the one printer it "
                           "serves.", reason="unsafe_address")
    literal = f"[{ip}]" if ip.version == 6 else str(ip)
    return f"http://{literal}:{UPLOAD_PORT}"


# --- the source, hashed and sent as one story --------------------------------------------------------


def _hash_source(f: Any, size: int) -> tuple[str, str]:
    """(whole-file MD5, whole-file SHA-256) of one open file, read once from the current
    position. The MD5 is the protocol's X-File-MD5 — mandated by Elegoo, not a security use;
    the SHA-256 is the receipt's promise. Returns the digests and leaves the caller to verify
    the size held still; bytes read past what was sized is a mutation this refuses."""
    f.seek(0)
    md5, sha = hashlib.md5(), hashlib.sha256()  # noqa: S324 — MD5 is the protocol's header, not our trust
    read, seen = 0, 0
    while read < size:
        block = f.read(min(_HASH_BLOCK, size - read))
        if not block:
            raise Cc2UploadError("protocol_error",
                                 f"The G-code file ended after {seen} of {size} bytes while it "
                                 "was being hashed — it is changing while this process reads "
                                 "it, and an upload that cannot promise one version of a file "
                                 "promises nothing.", reason="source_changed")
        md5.update(block)
        sha.update(block)
        seen = read = read + len(block)
    if f.read(1):
        raise Cc2UploadError("protocol_error",
                             f"The G-code file is longer than the {size} bytes counted before "
                             "hashing it — it grew while being read, so no single version of it "
                             "was hashed.", reason="source_changed")
    return md5.hexdigest(), sha.hexdigest()


def _stream_lines(f: Any, size: int) -> Iterable[str]:
    """The file as text lines for the settings parse, decoded with replacement so a broken byte
    fails the parse instead of raising a bare UnicodeDecodeError from mid-scan. Streams — a
    100 MB G-code never doubles as a str."""
    f.seek(0)
    seen = 0
    for raw in f:
        seen += len(raw)
        yield raw.decode("utf-8", "replace")
    if seen != size:
        raise Cc2UploadError("protocol_error",
                             f"The G-code file measured {size} bytes but streamed {seen} — it "
                             "is changing while this process reads it.", reason="source_changed")


def _check_chunk(resp: Cc2ChunkResponse, offset: int, name: str) -> None:
    """One chunk's yes, read narrowly: HTTP 200 (the only status the working upload ever saw) and
    a JSON object whose error_code is explicitly 0. 401 gets its own code because it means the
    token, not the transfer. A body that is not a JSON object, or an error_code that is not a
    plain integer, is a printer that has not said yes — treated as refused, like every nonzero
    code, and every one of these failures says the same hard truth: this chunk is not being sent
    again, and a partial file may be on the printer under this name."""
    where = f"chunk at offset {offset} of {name!r}"
    never_retry = (" Nothing was retried, and a partial file may be on the printer under this "
                   "name — check the printer's listing before deciding anything about it.")
    if resp.status_code == 401:
        raise Cc2UploadError("auth_rejected",
                             f"The printer refused the upload token for {name!r} (HTTP 401) — "
                             "the credential, not the transfer, is the problem. Nothing was "
                             "retried; the chunk it came on may or may not have landed.",
                             hint=_TOKEN_HINT, reason="auth_rejected")
    if resp.status_code != 200:
        raise Cc2UploadError("protocol_error",
                             f"The printer answered the {where} with HTTP {resp.status_code}, "
                             "not 200." + never_retry, reason="upload_chunk_failed",
                             http_status=resp.status_code)
    try:
        body = json.loads(resp.body.decode("utf-8", "replace"))
    except ValueError:
        raise Cc2UploadError("protocol_error",
                             f"The printer answered the {where} with HTTP 200 and a body that "
                             f"is not JSON ({resp.body[:48]!r}), so success cannot be read "
                             "from it." + never_retry, reason="upload_chunk_failed") from None
    if not isinstance(body, dict):
        raise Cc2UploadError("protocol_error",
                             f"The printer answered the {where} with a JSON value that is not "
                             "an object, so it carries no readable error_code." + never_retry,
                             reason="upload_chunk_failed")
    code = body.get("error_code")  # only an explicit zero confirms a chunk was accepted
    if isinstance(code, bool) or not isinstance(code, int):
        raise Cc2UploadError("protocol_error",
                             f"The printer answered the {where} with an error_code that is not a "
                             "whole number, so it cannot be called success." + never_retry,
                             reason="upload_chunk_failed")
    if code != 0:
        raise Cc2UploadError("protocol_error",
                             f"The printer declined the {where} (error_code {code})." + never_retry,
                             reason="upload_chunk_failed", error_code=code)


def _chunk_headers(dest: str, file_md5: str, offset: int, length: int, total: int,
                   token: AccessCode) -> dict[str, str]:
    """The headers the SDK sends with every CC2 chunk — inclusive byte range against the whole
    file's length, the destination name, the whole-file MD5 (Elegoo mandates it per chunk; not a
    security use), and the token. Values only ever into headers: nothing that leaves this dict
    is seen by a log, a receipt or an error."""
    return {
        "Content-Type": "application/octet-stream",
        "Content-Range": f"bytes {offset}-{offset + length - 1}/{total}",
        "X-File-Name": dest,
        "X-File-MD5": file_md5,
        "X-Token": token.value,
    }


# --- the operation ------------------------------------------------------------------------------------


async def upload_gcode(transport: Any, path: str | os.PathLike[str], destination: str, *,
                       token: AccessCode | str,
                       store: Cc2ReceiptStore | None = None,
                       http_factory: Callable[[str], Any] | None = None,
                       chunk_size: int = CHUNK_SIZE) -> Cc2UploadReceipt:
    """Move one local G-code file to the transport's printer under a new basename, and prove it.

    In order, with every failure named before the next step runs: validate the destination and
    the pinned address; hash and parse the file from one buffered open and refuse a file that is
    empty, growing, shrinking or unreadable as settings; take the per-printer lock (held, the
    operation refuses — it does not queue); ask the printer for a complete inventory through the
    fail-closed 1044 walk and stop on a name collision or an incomplete listing without sending
    a byte; stream the file in 1 MiB chunks through a second, unbuffered descriptor, each chunk
    exactly once, each answer checked as strictly as ``_check_chunk`` reads success, the bytes as
    actually sent hashed against the pre-computed SHA-256 so a raced source change stops the
    operation mid-stream; then demand the exact name and the exact byte count on a fresh complete
    inventory, and only then write the receipt — atomically, mode 600, into the private store.
    No step is ever attempted twice; a lost chunk answer is reported as lost.

    A completed upload whose receipt cannot be saved raises: the file may well be on the printer,
    but without the receipt this workflow cannot vouch for it, and a start must refuse it —
    honest loss beats a silent gap. Returns the receipt itself; the caller shows it, task 4
    later reads it back by (printer serial, name).
    """
    if not isinstance(token, AccessCode):
        token = AccessCode(str(token))  # strings become credentials on the way in, never the reverse
    dest = validate_destination(destination)
    base_url = checked_upload_base_url(str(transport.host))
    store = store if store is not None else Cc2ReceiptStore()
    file_path = Path(path)
    printer_sn = str(transport.sn)  # the receipt binds to the serial discovery proved

    try:
        st = file_path.stat()
    except OSError:
        raise PrinterError("protocol_error",
                           f"There is no G-code file at '{file_path}' to upload.",
                           reason="source_unreadable") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise PrinterError("protocol_error",
                           f"'{file_path}' is not a regular file, so nothing is uploaded through "
                           "it.", reason="source_unreadable")
    if st.st_size == 0:
        raise PrinterError("protocol_error",
                           f"'{file_path}' is empty; refusing to offer a printer an empty file.",
                           reason="source_unreadable")
    if st.st_size > MAX_SOURCE_BYTES:
        raise PrinterError("protocol_error",
                           f"'{file_path}' is {st.st_size} bytes — beyond the "
                           f"{MAX_SOURCE_BYTES}-byte ceiling this workflow will read and hash.",
                           reason="source_unreadable")

    with file_path.open("rb") as f:
        file_md5, sha256_hex = _hash_source(f, st.st_size)
        settings = parse_gcode_settings(_stream_lines(f, st.st_size))
        if os.fstat(f.fileno()).st_size != st.st_size:
            raise Cc2UploadError("protocol_error",
                                 "The G-code file changed size while it was being hashed and "
                                 "parsed; upload refused against a file that will not hold "
                                 "still.", reason="source_changed")

        with store.lock(printer_sn):
            inventory = await cc2_state.list_files(transport)  # refuses incomplete listings
            if inventory.find_file(dest) is not None:
                raise Cc2UploadError("protocol_error",
                                     f"'{dest}' already exists on the printer, and this workflow "
                                     "never overwrites. Choose a new destination name.",
                                     hint="Upload under a fresh basename, or remove the old file "
                                          "on the printer's own screen first.",
                                     reason="collision")

            http = (http_factory or Cc2UploadHttp)(base_url)
            # the send pass reads through an unbuffered descriptor: page-cache readahead would
            # hand back stale bytes after a mid-upload rewrite and let the closing hash "prove"
            # a version nobody ever re-read — os.read sees the file as it is at every offset
            send_fd = os.open(file_path, os.O_RDONLY)
            try:
                sent_sha = hashlib.sha256()
                offset = 0
                while offset < st.st_size:
                    chunk = os.read(send_fd, chunk_size)
                    if not chunk:
                        raise Cc2UploadError(
                            "protocol_error",
                            f"The source file shrank mid-upload at offset {offset} of "
                            f"{st.st_size} — the bytes promised are not the bytes available. "
                            "Nothing was retried; a partial file may be on the printer.",
                            reason="source_changed")
                    try:
                        resp = await http.put_chunk(
                            body=chunk,
                            headers=_chunk_headers(dest, file_md5, offset, len(chunk),
                                                   st.st_size, token))
                    except (httpx.HTTPError, OSError) as e:
                        raise Cc2UploadError(
                            "not_reachable",
                            f"The connection for chunk at offset {offset} of {dest!r} failed "
                            f"({type(e).__name__}: {str(e)[:120]}); whether it landed is "
                            "unknown. Nothing was retried; a partial file may be on the printer "
                            "under this name.", reason="http_failed") from e
                    _check_chunk(resp, offset, dest)
                    sent_sha.update(chunk)
                    offset += len(chunk)
                if os.read(send_fd, 1):
                    raise Cc2UploadError(
                        "protocol_error",
                        f"The source file grew past its promised {st.st_size} bytes while being "
                        "sent — what was uploaded is not a version anyone agreed to. Nothing "
                        "was retried.", reason="source_changed")
                if sent_sha.hexdigest() != sha256_hex:
                    raise Cc2UploadError(
                        "protocol_error",
                        "The bytes sent do not hash to the bytes that were hashed before the "
                        "first chunk — the file changed content mid-upload. Nothing was "
                        "finalised or received; a partial file may be on the printer under this "
                        "name.", reason="source_changed")
            finally:
                with contextlib.suppress(OSError):  # cleanup never masks the failure it followed
                    os.close(send_fd)
                await http.aclose()  # every path: clean, failed, refused, cancelled

            final = await cc2_state.list_files(transport)  # complete again or the proof is nothing
            entry = final.find_file(dest)
            if entry is None:
                raise Cc2UploadError(
                    "protocol_error",
                    f"Every chunk of {dest!r} was answered, but the printer's complete listing "
                    "that followed does not carry it — the transfer's end is ambiguous. Nothing "
                    "was retried, and no receipt was written.", reason="not_finalized")
            if entry.size != st.st_size:
                raise Cc2UploadError(
                    "protocol_error",
                    f"The printer lists {dest!r} at {entry.size} bytes but {st.st_size} were "
                    "sent — the file on the printer is not the file that was sent, so no receipt "
                    "is written and nothing was retried.", reason="size_mismatch",
                    listed_size=entry.size, sent_size=st.st_size)

            try:
                receipt = Cc2UploadReceipt(
                    printer_sn=printer_sn,
                    printer_display=getattr(transport, "display", None),
                    remote_filename=dest,
                    reported_size_bytes=entry.size,
                    content_sha256=sha256_hex,
                    sent_bytes=st.st_size,
                    material=settings.material,
                    nozzle_first_layer_temp=settings.nozzle_first_layer_temp,
                    nozzle_temp=settings.nozzle_temp,
                    bed_first_layer_temp=settings.bed_first_layer_temp,
                    bed_temp=settings.bed_temp,
                    commanded_nozzle_setpoints=settings.nozzle_setpoints,
                    commanded_bed_setpoints=settings.bed_setpoints,
                    extrusion_nozzle_temps=settings.extrusion_nozzle_temps,
                    tool_index=settings.tool_index,
                    uploaded_at=datetime.now(UTC))
            except PrinterError as e:
                raise Cc2UploadError(
                    "protocol_error",
                    f"The upload of {dest!r} completed and the printer lists it at "
                    f"{entry.size} bytes, but its receipt could not be built — without a receipt "
                    "this workflow will refuse to start it. Nothing was retried.",
                    reason="receipt_invalid") from e
            try:
                store.save(receipt)
            except PrinterError as e:
                raise Cc2UploadError(
                    "not_configured",
                    f"The upload of {dest!r} completed and the printer lists it at "
                    f"{entry.size} bytes, but its private receipt could not be saved, so this "
                    "workflow will refuse to start the file — the proof is the point. Nothing "
                    "was retried.", hint=_RECEIPT_HINT, reason="receipt_save_failed") from e
            return receipt
