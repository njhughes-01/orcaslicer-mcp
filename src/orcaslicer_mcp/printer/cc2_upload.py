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
any stop a partial file may sit on the printer under the destination name (the protocol describes
a delete, 1047, but none has ever been proven to work on this firmware, so this workflow never
deletes); the message says so, and the untouched name stays claimable only by a human
decision, never by automation.

Immutability, because the receipt promises the printer holds the bytes it hashes: the source file
is hashed from one buffered open (MD5 and SHA-256 before the first byte leaves), the settings
parse re-reads the file and hashes the exact bytes it feeds the parser, the send pass re-reads and
re-hashes the same offsets through a second, unbuffered descriptor, and any divergence — the file
shrinking or growing mid-send, its content changing under the reader — stops the upload before the
final listing or receipt, naming what changed. The three hashes are then demanded to agree: the
bytes hashed, the bytes parsed, and the bytes sent must be one version, checked before anything is
locked, listed or sent (parse hash against initial) and again before the receipt (send hash
against initial). Without the parse hash a same-size rewrite landing after the initial hash and
reverting before the send would let the parser record one version's settings for another version's
bytes — a receipt that says PLA/240 for bytes that leave as PETG/260 — while every downstream size
and send-hash check stayed green; the parse-consumed hash is the only thing that closes that gap.
The bytes sent are the bytes hashed by construction of the send pass; the closing hash comparisons
turn a raced read into a refusal instead of a false receipt. The destination name is refused unless
it is a plain ASCII ``.gcode`` basename — no separators, no control characters, no dotfiles,
nothing that is not a single honest filename — and an existing file of any size under that name
stops the operation before anything is sent: never overwrite, never while the file listing is
incomplete, never after an ambiguous one.

The file itself is read as evidence, not trusted metadata: ``parse_gcode_settings`` takes the
material, the first-layer and steady nozzle and bed setpoints, and the tools the file actually
selects from its own commands cross-checked against its own slicer config block — accepting
the legitimate case of a first layer hotter than the rest, refusing a file whose config promises
temperatures its G-code never commands, and reading a multi-tool file tool by tool: the material
and nozzle setpoints are one per selected tool, every extrusion is attributed to the tool in
force at that line (a purge switches the current tool to its own ``T``), and a purge may only
flush a tool the file itself selects. Only the opened bytes are ever parsed; nothing about the
file is inferred from its name.

Heater commands are read in the order the file issues them, because what a purge runs at is a
fact of order, not of contents. The parser carries the current nozzle target and the value of
the most recent ``M109`` wait: a non-extruding preheat (the observed ``M104 S140`` warm-up flush
hold before the ``M109 S260``) is legal merely as a preheat; every extrusion — conservatively,
any G0/G1/G2/G3 move that commands E and every ``M6211`` purge, whose own letters are read against
the published firmware rather than left unread (see ``_PURGE_FIELDS``: its ``Q``/``R``/``S`` are the
setpoints it heats to and its ``T`` switches a tool's filament) — is refused unless the most recent
verified ``M109`` waited at the very target now in force. Move parameters are read by tokenizer, not
by a whitespace split, so a compact ``G1 X1E6`` (an E welded inside a token) is counted as the
extrusion it is instead of vanishing behind its leading X, and a move whose parameters will not
tokenise is refused rather than trusted to hide no E. Commands are matched case-insensitively as
Klipper matches them.
Heater codes welded to their number with no space (``M104S0``, ``m109s240``) are normalised to
their spaced form and gated like any other, and one that cannot be cleanly split — a second packed
parameter, ``M109S240R200`` — is refused, never silently skipped. So too is any word-form command
naming a heater (Klipper's ``SET_HEATER_TEMPERATURE HEATER=extruder TARGET=260``): this adapter
cannot tell which physical heater moves nor read a ``WAIT``, so a heater change it cannot resolve
to one M109-verifiable setpoint is refused outright — a state change read as an inert line is
precisely how a cold or unverified extrusion reaches the printer. A target changed after a wait
(``M104`` to a new value) revives the requirement: only another matching ``M109`` re-arms
extrusion — with one exception (design 2): a bare ``M104`` that steps a verified wait at the
current tool's declared ``nozzle_temperature_initial_layer`` up to that tool's declared
``nozzle_temperature`` is the slicer's own layer-two transition, keeps the wait armed, and has
its value recorded as one of that tool's verified extrusion temperatures. A second exception
(design 3): at a wipe-tower tool change a bare ``M104`` to the declared ``nozzle_temperature`` of
a tool other than the one in force is a *pending tool-change setpoint* — it does not revoke the
armed wait for retractions (negative-E moves, which run while the hot end still holds the old
verified temperature), every forward extrusion refuses, and only an ``M6211`` purge consumes the
pending value: its ``Q`` must be the wait still armed, its ``S`` the declared steady print
temperature of the tool it switches in (which the firmware's own ``M109 S`` inside the purge
waits at), and after the purge that ``S`` is the verified temperature the print continues at. A nozzle cooldown (``S0``) before the file's last extrusion contradicts itself — the
filament path is cold while the file still means to extrude — and is refused; the end-block
cooldown after the last extrusion is the normal ending it is. No printing-temperature range is
checked here, at all: what a spool tolerates is the CANVAS tray's answer at start, and the
verified extrusion temperatures — and, apart from them, each tool's flush temperatures — ride on
the receipt so that start can make that comparison. A
heater command this parser cannot pin to one unambiguous setpoint — no S, two S, a second numbered
parameter like ``R200`` beside the S, a value no heater in this family could hold — is ambiguity
about what the printer would do, and ambiguity is refused, never guessed at. A compact heater code
keeps every parameter written after it, because spacing is not what makes two setpoints out of one
command: this printer's firmware cuts letters from numbers and folds the pairs into a map that
overwrites, so ``M109S260 S0`` would hold the *last* S (0, off) while a parser that spaced only the
head would report 260 and never see the cooldown.

Privacy: the upload token is an ``AccessCode`` (the transport's redacting credential type) that
lives only in request headers; no message, error or receipt echoes it, and no G-code text or raw
reply leaves this module in any direction. The HTTP session is created per upload and closed on
every path, exactly like the MQTT session it rides beside.

Commands are an allow list, never a deny list, because skipping an unread line is trusting it. In
Klipper's dialect a word-form name is executable — it may be a macro that heats, extrudes or swaps
filament, in a body no file-level reader can see — and a G or M code this parser has not read can
change a heater target or push filament just as well. So a command is accepted because it appears in
``_SAFE_CODE_COMMANDS`` / ``_SAFE_WORD_COMMANDS`` beside a reason it cannot alter heater or
extrusion state, and everything else refuses the file before a byte is sent. The list holds G0, G1,
G2, G3, G4, G21, G28, G90, G92, M104, M106, M107, M109, M140, M190, M204, M220, M6211, M73, M83,
M84, and the ``T<n>`` selects (several tools are legal; each extrusion is attributed to the tool
in force), beside
the four Klipper words BED_MESH_CALIBRATE, SET_PRESSURE_ADVANCE,
SET_PRINT_STATS_INFO, SET_VELOCITY_LIMIT. Each was admitted the same way: a written reason it cannot
change a heater target or push filament, and a test that says so. The four real Centauri Carbon 2
slices this workflow has handled (2026-11-04) issued G1, G2, G21, G28, G90, G92, M104, M106, M109,
M140, M190, M204, M6211, M73, M83, M84, one ``T0`` and the four words — and no G0, G3, G4 or M107
anywhere in 694,064 command lines of 18.7 MB of slice — so those four are listed for their semantics,
not because a slicer was
seen writing them. G0 and G3 were already read as moves here with their E parameter gated as an
extrusion (see ``_MOVE_HEADS``), which is the only mechanism either has for heating cold, and G4,
M107 and M220 are admitted with their documented parameters enforced (see ``_PARAMETER_LETTERS``)
so a dwell carrying an E, a fan-off carrying anything else, or a speed factor carrying anything
but its S, still refuses the file. Ordinary elsewhere is
still refused here — ``G_CODE_MACRO`` and every other macro name, G10/G11/G12 (what the CC2 firmware
does with them is unverified, and in the dialects it derives from G10 can move filament and G12
purges), M82, SET_FAN_SPEED and every word-form name the list does not hold. Command and parameter
letters are matched whatever their case, because heads, tool selects, compact heater codes
(``m109s240``) and move parameter letters are already matched case-insensitively: ``M104 s140`` sets
the setpoint ``M104 S140`` sets and ``G180 s7`` selects the macro ``G180 S7`` selects. Only the
LETTER folds, never the count, so ``M104 S140 s200`` is still two setpoints and still refuses.
G180 is the exception that proves
the rule rather than an exception to it: G180 is not one command but the CC2 firmware's own macro
dispatcher, so it is allowed by *number* and never by name — see ``_G180_MACROS`` below, where the
three numbers the slicer really issues are each read against the published Elegoo firmware and
config and every other number still refuses the file, because an unread ``[g180]`` body heats, waits,
purges, cuts or withdraws filament. Padded (``M0104``) and welded (``G1X1E6``) spellings refuse too,
because normalising them would admit a heater or move command without the state tracking that is the
only reason those commands are allowed. A refusal costs one re-slice; a wrong guess costs the print,
so widening the list means adding an entry with its reason and a test, never loosening the rule.
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

from . import cc2_state, cc2_stock_templates
from .cc2_receipts import Cc2ReceiptStore, Cc2UploadReceipt, ToolReceipt
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
            trust_env=False,
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
# command grammar is line-initial, as the slicer writes it and Klipper parses it. The purge's own
# `T` is read where M6211 is read (see _PURGE_FIELDS: the firmware runs CANVAS_SWITCH_FILAMENT T=<n>
# inside the purge), and never by this regex: a file whose only tool mention sits inside an M6211 has
# no tool select of its own and still refuses.
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

# The commands whose E parameter makes the move an extrusion: linear moves G0/G1 and arcs
# G2/G3 (the G38.x probing cycle too), each with and without the leading zero. Read
# conservatively: any E parameter on one of these counts as an extrusion, whether the slicer
# wrote ``G1 E6`` or a compact, space-free ``G1 X1E6`` whose E hides inside a token, because a
# move whose extrusion cannot be ruled out is a move to hold behind a verified temperature wait.
# G0, G1, G2 and G3 are on the command list below; the rest (their padded spellings and the G38.x
# probing cycle) are listed so their E parameter is still read as an extrusion when one shows up,
# and refused as untracked in every other case.
_MOVE_HEADS = frozenset({"G0", "G00", "G1", "G01", "G2", "G02", "G3", "G03", "G38", "G038"})

# A move parameter: one letterhead and its (optionally signed, optionally decimal) number. The
# slicer writes them space separated (``X127 Y-1.2 F20000``) but the grammar does not require the
# space (``X1E6`` is X1 followed by E6), and an E anywhere among them is an extrusion however it
# is spaced. The tokenizer consumes letter+number runs so a compact ``E`` that is not the first
# parameter is still seen; anything left over that is not whitespace is a parameter this parser
# cannot cleanly read, and a move with an unreadable parameter is refused rather than trusted to
# be extrusion-free.
_MOVE_PARAM_RE = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")

# A word-form command name — Klipper's extended G-code (``SET_HEATER_TEMPERATURE``) and macro
# names — as opposed to a plain letter+number G/M code (G1, M109, G38.2). Underscore is what a
# word-form name is made of and never appears in a G/M code, so this separates the two families.
_WORD_HEAD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

# Word-form command names that command a heater (and, unlike M104/M109, are invisible to a parser
# that reads only the letter+number codes). Klipper writes extrusion and heater control in an
# extended dialect — ``SET_HEATER_TEMPERATURE HEATER=extruder TARGET=260`` sets the hot end to a
# target no M109 ever waited at, and ``TARGET=0`` turns it off mid-file. This adapter cannot tell
# from ``HEATER=``\ *whatever* which physical heater moves, nor read a ``WAIT`` parameter, so any
# command naming a heater is refused, never half-parsed. Matched only against the command word
# itself, so a variable called ``heater_temp`` inside another command is not a heater command.
_HEATER_KEYWORD_RE = re.compile(r"(?:HEATER|TEMPERATURE|DRYER|CHAMBER|COOL)\d*$|"
                                r"(?:HEATER|TEMPERATURE|DRYER|CHAMBER|COOL)_|"
                                r"_(?:HEATER|TEMPERATURE|DRYER|CHAMBER|COOL)(?:_|$)|"
                                r"(?:SET|GET|START)_(?:HOTEND|NOZZLE)(?:_|$)|"
                                r"^(?:HOTEND|NOZZLE)(?:_|$)")

# A compact heater code — a recognised heat command with its first parameter letter welded to the
# number with no space (``M104S0``, ``m109s240``). Klipper parses these; a parser that keys on
# ``M104`` would not, and a heater change it skips is a heater change it never gated.
_COMPACT_HEATER_RE = re.compile(r"^M0*(104|109|140|190)([A-Za-z])", re.IGNORECASE)

# --- the command policy: an allow list, stated in full -------------------------------------------
#
# A command this parser does not read used to be *skipped*, and skipping is trusting: in Klipper's
# dialect any word-form name is executable — it may be a printer macro that heats, extrudes or runs
# a filament swap — and a G or M code nobody read can still change a heater target or push filament.
# A state change the parser never saw is exactly how a cold extrusion reaches a printer a human
# believes is running an ordinary print. So nothing is allowed because it is unfamiliar; everything
# is allowed because it appears below with the reason it cannot alter heater or extrusion state, and
# everything else refuses the file before a byte is sent.
#
# The list started as the command set of the mount fixture this workflow was built for (the file
# actually uploaded and printed on 2026-11-04) and it has since taken on the commands whose
# *semantics*, not whose observed use, this adapter can state: G0 and G3 (the same moves G1 and G2
# are, and already E-gated here), G4 (a dwell) and M107 (the fan's own off-switch). What is ordinary
# elsewhere and still absent is deliberate — M82, G10-G12 and every macro name the fixture never
# showed — because "usually harmless" is not a fact this adapter can establish, and the cost of a
# refusal is one re-slice while the cost of a wrong guess is a ruined print or worse. Widening the
# list means adding an entry here with its reason and a test, never loosening the rule: an entry for
# a command whose inertness depends on what its parameters mean (_PARAMETER_LETTERS) has to say
# which parameters those are.
_SAFE_CODE_COMMANDS: dict[str, str] = {
    # letter+number codes, in the exact spelling the slicer writes them (see _policy_refusal for
    # why a padded or welded spelling of a special code refuses rather than normalising)
    "G0": "a linear move, the same family as G1, and this parser already reads it as one: its E "
          "parameter is an extrusion gated by the M109 wait below (see _MOVE_HEADS), and a linear "
          "move with no E commands no heater and no filament",
    "G1": "a linear move — and its E parameter is an extrusion, gated by the M109 wait below",
    "G2": "an arc move — gated by the same E rule as G1",
    "G3": "an arc move, the same family as G2, and this parser already reads it as one: its E "
          "parameter is an extrusion gated by the same E rule, and an arc with no E commands no "
          "heater and no filament",
    "G4": "a dwell: the parameters it documents are a duration (P/S/D), and no dialect of this "
          "family gives a duration a mechanism — nothing moves, nothing heats, nothing extrudes — "
          "so the duration letters are enforced by _PARAMETER_LETTERS and a dwell carrying an E or "
          "an unread parameter still refuses the file",
    "G21": "sets millimetre units: a units word, it moves nothing and heats nothing",
    "G28": "homes: axes move, the extruder does not, and no heater target changes",
    "G90": "selects absolute XY coordinates: a coordinate mode, with no mechanism in it",
    "G92": "re-zeroes a coordinate (the fixture's `G92 E0` re-zeroes the E counter); no axis "
           "moves and no filament moves, whatever coordinate it renames",
    "M104": "parsed as a nozzle setpoint and carried into the order check",
    "M106": "part-cooling fan (S0..255); `M106 S0` is how the fixture stops the fan — a fan "
            "cannot heat a heater or advance filament",
    "M107": "the part-cooling fan off — `M106 S0`'s other spelling of the same thing, and the only "
            "meaning this family documents for it; a fan can neither heat a heater nor advance "
            "filament, and its parameters are read by _PARAMETER_LETTERS, so an M107 carrying "
            "anything else refuses",
    "M109": "parsed as a nozzle wait; it is the evidence every extrusion rides on",
    "M140": "parsed as a bed setpoint",
    "M190": "parsed as a bed wait",
    "M204": "sets acceleration: a motion-tuning number, with no extruder and no heater in it",
    "M220": "speed factor override: scales feedrates of moves already gated by the E rules "
            "above; it commands no heater and no filament, and its parameters are read by "
            "_PARAMETER_LETTERS (S only), so an M220 carrying anything else still refuses",
    "M6211": "Elegoo's flush/purge: read as an extrusion and gated by the order check, and read for "
              "its own facts too — its T switches a tool's filament in and its Q, R and S are the "
              "setpoints the purge heats to (see _PURGE_FIELDS)",
    "M73": "reports progress to the display; no mechanism is addressed",
    "M83": "selects relative-E mode: a mode word, no filament moves because it was said",
    "M84": "disables the stepper drivers (the fixture's end block); no heater, no extrusion",
}
# The allowed codes whose *reason* holds only for the parameters their dialect documents, and the
# parameter letters those two commands can carry. A dwell is inert because P/S/D name a wait and
# nothing else, and a fan-off is inert because its letters name a fan and a speed. An E welded into
# either (``G4 P100E6``) or a letter no dialect documents (``G4 U255``, ``M107 R200``) is outside the
# evidence its entry rests on, so it refuses the file instead of riding on "that command cannot
# extrude". Read with the same tokenizer that reads a move, so a compact run hides nothing, and a
# bare ``G4``/``M107`` with no parameters at all is the ordinary case and passes.
_PARAMETER_LETTERS: dict[str, frozenset[str]] = {
    "G4": frozenset({"P", "S", "D"}),      # a dwell's documented durations: P/S (ms), D (s)
    "M107": frozenset({"P", "S"}),         # a fan's index (P) and its speed (S); S0 is a fan off
    "M220": frozenset({"S"}),              # a speed factor's one percent value; nothing else
}

_SAFE_WORD_COMMANDS: dict[str, str] = {
    # Klipper extended-G-code words the fixture issues. A word-form name not listed here is a macro
    # whose body this adapter cannot read, whatever it is called.
    "BED_MESH_CALIBRATE": "runs a bed-probe pass and applies mesh compensation. It can move the Z "
                          "axis and probe the bed, and it pushes no filament by itself. It is NOT "
                          "read as setting no nozzle target: the calibration body is a binary-only "
                          "library whose strings carry heater commands (M109 S140, M140/M190, G180 "
                          "numbers that wait), so it may change nozzle targets, and no verified "
                          "M109 wait is carried across this line — the next extrusion needs a "
                          "fresh M109, which the stock start template issues before any "
                          "extrusion",
    "SET_PRESSURE_ADVANCE": "sets the pressure-advance coefficient, a flow-tuning number: it "
                            "commands no temperature and extrudes nothing by itself",
    "SET_PRINT_STATS_INFO": "records layer/bookkeeping numbers for the display; no mechanism",
    "SET_VELOCITY_LIMIT": "sets maximum speeds; motion limits only, no extruder and no heater",
}

# Commands that are known-by-name and still refused, each for its own stated reason — the message a
# human gets should name the command and what is unknown about it, not just say "unknown". Length is
# a real constraint here, not a style preference: a refusal travels out through the MCP reply, where
# every string is capped, so each message is built to keep its promise that nothing was sent inside the
# first 255 characters even with a 40-character command quoted in it. The fuller argument for each
# entry lives in the allow-list comment above and in the hint below.
_BLOCKED_COMMANDS: dict[str, str] = {
    # RepRap/Marlin-family G10 with R re-primes filament and G12 runs a clean-up purge; whether the
    # CC2 firmware implements them, and what they move there, is unverified. Coordinate bookkeeping
    # in one dialect is filament movement in another, and this parser will not assume which it has.
    "G10": "it may move filament (a retract, and with R a re-primed filament path) on those "
           "dialects, unverified here",
    "G11": "on those dialects it is G10's paired restore, which may carry filament movement, "
           "unverified here",
    "G12": "on the RepRap family it is a clean-up purge: filament movement, unverified here",
}

# --- G180: the CC2's own macro dispatcher, decided by number, never by name -----------------------
#
# G180 was seen in a real Centauri Carbon 2 slice and it is not one command, so it can be neither
# allowed nor blocked by its code name. In the published Elegoo Centauri Carbon 2 firmware
# (github.com/elegooofficial/CentauriCarbon2, branch main, read 2026-11-04) the class ``G180`` in
# ``elegoo/extras/gcode_move.cpp`` registers the name and loads the printer's own ``[g180]`` config
# section as macro templates named ``S1``…``S998``; ``cmd_G180`` then does
# ``select = gcmd->get_int("S", 0); if (select > 0 && select < tws.size()) tws[select - 1]->
# run_gcode_from_command();``. So ``G180 S<n>`` runs the macro body the *printer's config* holds for
# that number, and the uploaded file alone never says what it does — the ``[g180]`` section does.
# The published section is ``debugfile/printer_dsp.cfg`` lines 644-662, and reading it number by
# number is the only honest way to widen this: S1 runs ``M109 S200``/``M104 S140`` and a hot wipe,
# S2 probes and touches the nozzle at ``M109 S140``, S3 and S5 home and run S1 plus S2, S6 switches
# every heater off, ``S800`` (which the unload chain calls) waits at ``M109 S140``, S812 pushes some
# 80 mm of filament through a hot end, S820/S821 drive the CANVAS filament motors. A blanket pass on
# "G180" would be a blanket pass on all of those; a blanket block makes the printer's own start and
# end blocks unuploadable. So the table is closed: one entry per number, each naming the body it was
# read against and the accounting it forces. Nothing selects a macro this table has not read.
#
# What that evidence does NOT establish, stated plainly because it is a real limit: it is the
# published source, not this printer's running firmware. The CC2 protocol offers no way to read a
# printer's ``[g180]`` section back, so the guard cannot prove a given machine's S7/S8/S9 match the
# published ones, nor that its firmware matches this revision. A number is accepted because the
# published firmware and config say what it does; a printer whose config was edited elsewhere can
# still differ, and only a human can establish that on a given machine.


@dataclass(frozen=True)
class _G180Macro:
    """One read number of the CC2's ``[g180]`` dispatcher table: the body it was read against
    (``body``), why accepting it cannot let a cold extrusion through (``reason``), and whether its
    body can heat, wait, cut or withdraw filament (``ends_print``) so the parser must stop carrying
    the nozzle state across it."""
    body: str
    reason: str
    ends_print: bool = False


_G180_MACROS: dict[int, _G180Macro] = {
    7: _G180Macro(
        body="SET_CONDITION J=0 A=BED_MESH_PROFILE B=0 / BED_MESH_PROFILE LOAD=default / "
             "SET_CONDITION_END, the same pair for B=1 loading default1, then M400",
        reason="it selects a bed-mesh profile and synchronises the move queue: SET_CONDITION is the "
               "firmware's own if over runtime printer state (elegoo/gcode.cpp "
               "cmd_SET_CONDITION), BED_MESH_PROFILE loads saved mesh data, and M400 only waits for "
               "moves already queued. Mesh compensation moves Z, it commands no nozzle or bed "
               "target and pushes no filament, so every M109 rule still binds the next extrusion"),
    8: _G180Macro(
        body="BED_MESH_PROFILE LOAD=ADAPTIVE FROM_SLICER=1, then M400",
        reason="it loads the adaptive mesh the slicer already put in the file — the same "
               "BED_MESH_PROFILE command the file's own BED_MESH_CALIBRATE line belongs to, which "
               "this list already carries — plus a move-queue sync. No S parameter reaches a heater "
               "and no E parameter exists, so nothing about the extrusion gate changes"),
    9: _G180Macro(
        body="SET_CONDITION J=0 A=ENABLE_CANVAS B=1 / M400 / PRINTER_UNLOAD_FILAMENT / M400 / "
             "SET_CONDITION_END, then M204 S5000, G1 X51 Y245 F20000, G1 Y264 F3000, G92 E0, M400, "
             "the two conditional BED_MESH_PROFILE loads again, M400",
        reason="it is the slicer's end-of-print unload, and it is conditional on runtime state this "
               "file cannot show: SET_CONDITION ENABLE_CANVAS is an if over whether the CANVAS "
               "filament system is connected, and PRINTER_UNLOAD_FILAMENT "
               "(elegoo/extras/mmu.cpp CMD_printer_unload_filament) returns early when that link is "
               "down or the head filament sensor is empty, and otherwise runs G180 S800 — which "
               "waits at M109 S140 whenever the nozzle is under 100 °C, per cmd_SET_CONDITION "
               "NOZZLE_TEMP — then cuts the filament and withdraws it by device routine before the "
               "moves and mesh reloads above. The guard cannot tell a run from a skip, so it assumes "
               "the run: the line ends the print for accounting purposes (ends_print), the nozzle "
               "target and its verified wait are dropped at it, and no extrusion or purge after it "
               "can be gated. That is why it is only safe as the file's last act, and why a file "
               "that extrudes after it is refused rather than re-accounted",
        ends_print=True),
}

_G180_HINT = (
    "G180 runs a macro from the printer's own [g180] config section, so a number may be used only "
    "once its body is read there and accounted for: add it to _G180_MACROS in printer/cc2_upload.py "
    "with its body, its accounting and a test, or re-slice without it.")

# An S number the firmware could select: the letter, then at most three decimal digits (``S7``,
# ``S07``). The firmware reads it with get_int, so leading zeros select the same macro. It loads
# templates S1..S998, but its select runs only ``0 < S < tws.size()`` and tws.size() is 998, so
# the usable range is S1–S997 (S998 is loaded but can never be selected); anything longer is not
# a number that dispatcher can run. Everything else — ``S7.5``,
# ``S-7``, ``S+7``, ``S0x7``, ``S{nozzle_temperature}`` (an unresolved template), a bare ``S`` or a
# missing parameter — cannot be pinned to one macro and is refused.
_G180_S_RE = re.compile(r"^S([0-9]{1,3})$")


def _g180_macro(line: str, rest: str) -> _G180Macro:
    """The read entry for a ``G180`` line, or a refusal naming the line and what is unread about it.

    The whole parameter list has to be one S number: the firmware's macros ignore extra parameters,
    but a line that carries them is not the documented call and this guard does not guess what the
    number resolves to. An unknown number refuses whatever its body might be — the published
    ``[g180]`` section is full of numbers that heat, wait, purge, cut or withdraw filament, and the
    table above is the only evidence this adapter holds about any of them."""
    raw = rest.split()
    # the S letter folds like every other parameter letter (see _parse_S), so `G180 s7` selects the
    # macro `G180 S7` selects; the refusals below still quote the line exactly as the file wrote it
    tokens = [t.upper() for t in raw]
    quote = repr(line[:20])
    if not tokens:
        raise _refuse(f"the command {quote} is G180 with no S number, and G180 only ever selects a "
                      "macro by number, so this file says nothing about what it would run",
                      hint=_G180_HINT)
    if len(tokens) != 1:
        raise _refuse(f"the command {quote} carries parameters beyond a single S number, which is "
                      "not the documented G180 call this guard read", hint=_G180_HINT)
    m = _G180_S_RE.match(tokens[0])
    if not m:
        raise _refuse(f"the command {quote} selects G180 macro {raw[0]!r}, not an S<number> "
                      "within the S1-S997 the firmware's [g180] dispatcher can select", hint=_G180_HINT)
    number = int(m.group(1))
    if macro := _G180_MACROS.get(number):
        return macro
    known = "/".join(f"S{n}" for n in sorted(_G180_MACROS))
    raise _refuse(
        f"the command {quote} selects G180 macro S{number}, whose [g180] body is unread here; "
        f"only {known} are read; unread numbers may heat, purge, cut or withdraw filament",
        hint=_G180_HINT)


def _after_g180_unload(where: str) -> PrinterError:
    """A refusal for anything that would extrude after a ``G180 S9``. The unload can have pulled the
    filament out and can have waited at its own temperature, so no later extrusion has a temperature
    history this file can vouch for; refusing costs one re-slice and never a cold purge."""
    return _refuse(
        f"{where} comes after a G180 S9 filament unload, whose published body can cut and withdraw "
        "the filament and can wait at its own temperature (it calls S800, which runs M109 S140), so "
        "nothing after it can be temperature-accounted",
        hint="A CC2 file may only run its G180 S9 unload after its last extrusion or purge, the way "
             "the slicer's own end block does; re-slice, or move the unload after the extrusions.")


# What to do about a refusal, said once rather than in every message. Kept inside the reply cap on
# purpose: a hint a client truncates drops the sentence that says how to move, and which commands
# are accepted lives where a change has to be made anyway — _SAFE_CODE_COMMANDS and
# _SAFE_WORD_COMMANDS above, each with its reason, plus the README.
_COMMAND_POLICY_HINT = (
    "Either re-slice without that command, or establish from the firmware what it does to heater and "
    "extrusion state and add it to the allow list in printer/cc2_upload.py with its reason and a "
    "test. Nothing outside that list is ever sent.")

# M600 is not on the allow list and stays off it — the parser does not read it and the file is
# refused — but "an unread code" alone under-sells what it is: the published firmware makes it a
# filament-change pause that runs its load macro and an M109 whose temperature defaults to 250 °C
# (CentauriCarbon2 firmware, filament_load_unload.cpp#L25,L45-L66; Orca's machine_pause_gcode).
# The hint says so, so the human re-slicing knows what the pause would have done.
_M600_HINT = (
    "M600 is the CC2 firmware's filament-change pause: it runs its filament-load macro and an "
    "M109 whose temperature defaults to 250 °C — a wait at a setpoint the file never commands. "
    "It stays off the allow list; re-slice without a pause.")

# The character cap server._cc2_out puts on every string of a reply, mirrored here because the parser
# cannot import the server (the layering runs the other way). tests/test_cc2_upload.py pins the two
# numbers together and checks that what a refusal must never lose — the promise that nothing was sent,
# and, for the command policy, the whole message and hint — sits inside what a client will show.
_REPLY_CAP = 255


def _parameter_policy(head: str, line: str, rest: str) -> PrinterError | None:
    """None when an entry in ``_PARAMETER_LETTERS`` still means what its entry says it means, a
    refusal when its parameters have gone outside that. The reason a dwell or a fan-off is on the
    allow list is a reason about its documented parameters, so the letters are read, folded to upper
    case like every other parameter letter, and compared against that entry's set; anything the
    dialects do not document for that command — including an E — refuses the file rather than being
    waved through on the strength of what the command usually means."""
    if head not in _PARAMETER_LETTERS:
        return None
    letters = _PARAMETER_LETTERS[head]
    named = "/".join(sorted(letters))
    params = _move_params(rest)
    if params is None:
        return _refuse(f"the command {line[:32]!r} carries a parameter this parser cannot read, and "
                       f"{head} is allowed because its documented parameters are {named}",
                       hint=_COMMAND_POLICY_HINT)
    for letter, _ in params:
        if letter not in letters:
            return _refuse(
                f"the command {line[:32]!r} carries {letter} on {head}, which no dialect this "
                f"adapter read gives a meaning on that command (its parameters are {named})",
                hint=_COMMAND_POLICY_HINT)
    return None


def _command_policy(head: str, line: str, rest: str | None = None) -> PrinterError | None:
    """None when the file may go on issuing commands, a refusal when this one may not.

    Looked up on the head exactly as the file wrote it, upper-cased, and reached only for commands
    the loop did not consume. That matters for the padded (``M0104``, ``G001``) and welded
    (``G1X1E6``) spellings Klipper would accept: they are refused rather than normalised into the
    list, because normalising would let a heater or move command count as allowed without ever
    passing through the state tracking that is the whole reason those commands are allowed. A
    spelling this parser cannot follow is a refusal that names it, never an assumption.

    ``rest`` is the command's own parameter text; a caller that has it passes it, and a caller that
    does not (a bare policy lookup) has it taken from ``line`` here, so the parameter rule below can
    never be skipped by a caller who forgot to hand it over."""
    if rest is None:
        parts = line.split(None, 1)
        rest = parts[1] if len(parts) > 1 else ""
    if head in _SAFE_CODE_COMMANDS or head in _SAFE_WORD_COMMANDS:
        # an entry whose inertness depends on what its parameters mean (_PARAMETER_LETTERS) is only
        # allowed while its parameters are the documented ones; every other entry is allowed by its
        # own stated reason and needs nothing further here
        return _parameter_policy(head, line, rest)
    if head == "G180":
        # the dispatcher is decided by its S number, never by the code name, so the accurate refusal
        # is the dispatcher's own (_g180_macro refuses every spelling it cannot pin to one macro).
        # The parse loop consumes a G180 line before it reaches here, so this path exists to tell the
        # truth about a bare G180 handed straight to the policy check.
        try:
            _g180_macro(line, line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else "")
        except PrinterError as refusal:
            return refusal
        return None
    if why := _BLOCKED_COMMANDS.get(head):
        return _refuse(f"the command {line[:40]!r} is {head}, and {why}", hint=_COMMAND_POLICY_HINT)
    canon = re.fullmatch(r"([GM])0*(\d+)", head)
    unread_code = ("is a code this parser does not read, and an unread code may change a heater "
                   "target or the extruder just as well as a macro can")
    if canon:
        plain = f"{canon.group(1)}{int(canon.group(2))}"
        if plain != head and plain in _TRACKED_CODE_SPELLINGS:
            return _refuse(
                f"the command {line[:32]!r} is a padded or welded spelling of {plain}; this parser "
                "cannot track its state changes", hint=_COMMAND_POLICY_HINT)
        if plain == "M600":
            # unread and refused like any other off-list code; only the hint is more specific, so
            # the refusal names what the pause really is (see _M600_HINT)
            return _refuse(f"the command {line[:32]!r} {unread_code}", hint=_M600_HINT)
        # a code this parser simply does not read at all (M141's chamber heater, M82, a G38.2 probe
        # move, a padded G00)
        return _refuse(f"the command {line[:32]!r} {unread_code}", hint=_COMMAND_POLICY_HINT)
    if welded := _WELDED_CODE_RE.match(head):   # ``G180S7``: a code welded to a packed parameter
        plain = f"{welded.group(1)}{int(welded.group(2))}"
        if plain in _TRACKED_CODE_SPELLINGS:
            return _refuse(
                f"the command {line[:32]!r} is a welded spelling of {plain}; this parser cannot "
                "track its state changes", hint=_COMMAND_POLICY_HINT)
    if re.match(r"[GM]\d", head):  # a decimal code (a G38.2 probe move) — a code, not a macro word
        return _refuse(f"the command {line[:32]!r} {unread_code}", hint=_COMMAND_POLICY_HINT)
    return _refuse(
        f"the command {line[:32]!r} is a word-form name this parser does not read, and a macro may "
        "heat or extrude where this file shows nothing", hint=_COMMAND_POLICY_HINT)


# The codes whose handling is state-tracking (a heater setpoint, an extrusion, a tool select, the
# CC2's G180 macro dispatcher), in the only spellings the loop reads. Used solely to tell a human
# that a padded or welded cousin of one of these was refused because it could not be tracked — never
# to let it through.
_TRACKED_CODE_SPELLINGS = frozenset({"M104", "M109", "M140", "M190", "M6211", "G180"}) | _MOVE_HEADS

# A letter+number code welded to a packed parameter (``G180S7``, ``G1X1E6``) — the shape Klipper's
# legacy parser accepts and a name-keyed parser would not even look up. Named here so the refusal
# says which tracked command it is a spelling *of*, rather than calling it an unread code.
_WELDED_CODE_RE = re.compile(r"([GM])0*(\d+)[A-Za-z][-+.0-9]")

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
class ToolSettings:
    """What the file says about one of its own tools: the material it prints and the nozzle
    setpoints it prints at — first layer may legitimately differ from the rest, and both are
    kept — plus the nozzle temperatures that tool's extrusion was verified to run at, with the
    same meaning ``Cc2GcodeSettings.extrusion_nozzle_temps`` has. Every tool the file selects
    carries its own entry: its E moves and the purges that flushed it attribute their verified
    temperatures to it. An accepted flush R — the vendor purge's declared range-high, which the
    flush really extruded at but no M109 ever waited at — is not a printing temperature and is
    kept apart in ``flush_nozzle_temps`` (design amendment 9), recorded for both the tool being
    left and the tool switched to, distinct, in first order seen."""
    tool_index: int
    material: str
    nozzle_first_layer_temp: float
    nozzle_temp: float
    extrusion_nozzle_temps: tuple[float, ...]
    flush_nozzle_temps: tuple[float, ...] = ()


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
    setpoint — which is what a start compares against the loaded spool's range. An accepted
    flush R (the purge's declared range-high, design amendment 9) is neither: it is no printing
    temperature and no commanded setpoint, and it is carried per tool in
    ``ToolSettings.flush_nozzle_temps`` instead. Heater-off commands (S0) are cooldowns, not
    setpoints, and read out. ``tool_index`` is the first selected tool, the one the flat fields
    describe; ``tools`` carries every selected tool with its own material, setpoints, verified
    extrusion temperatures and flush temperatures. ``printer_model`` is the file's own
    ``; printer_model =`` config value, which the upload gate demands name this printer exactly —
    an unequal or missing value refuses before this dataclass ever exists, so a value here is
    always the accepted one. ``vendor_templates`` is design 5's report on the file's five
    machine-level G-code template config keys — ``"stock"`` when every one the file carries
    matches the shipped OrcaSlicer 2.4.2 CC2 template it should be, ``"modified: key, key"``
    naming the ones that do not (never a refusal; a key the file never wrote is skipped, not
    counted against it) — and ``templates_compared`` is how many of the five the file actually
    carried, so ``("stock", 0)`` means nothing was compared, not that anything was proven."""
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
    tools: tuple[ToolSettings, ...]
    printer_model: str
    vendor_templates: str
    templates_compared: int


_REFUSE_PREFIX = ("The G-code file cannot be uploaded through this workflow. Nothing was sent "
                  "to the printer. ")


def _refuse(what: str, hint: str | None = None) -> PrinterError:
    """A refusal, ordered so the sentence a model must not miss is the one a capped reply cannot cut:
    the promise that nothing was sent leads, and the complaint — which command, and what about it —
    follows. Messages are not clipped to fit: the long ones carry a diagnosis (which wait was missing,
    which two values disagree) that a human needs, and the parser's own command-policy refusals are
    written short enough to arrive whole. What a client that shows 255 characters loses is the advice at
    the end, which the hint repeats."""
    return PrinterError("protocol_error", _REFUSE_PREFIX + what[:1].upper() + what[1:],
                        hint=hint, reason="gcode_unreadable")


def _model_refuse(what: str) -> PrinterError:
    """A refusal whose reason is ``printer_model``: the file's own printer_model line says it was
    not sliced for the one printer this workflow serves. Same promise-first ordering as every
    refusal; a distinct reason so a caller can tell "wrong printer" from "unreadable file"."""
    return PrinterError("protocol_error", _REFUSE_PREFIX + what[:1].upper() + what[1:],
                        hint=_MODEL_HINT, reason="printer_model")


def _config_value(raw: str) -> list[str]:
    """One config value, split the way the slicer writes multi-slot lists — comma **or**
    semicolon separated (the real two-tool slice writes ``filament_type = PLA;PLA`` beside
    comma lists like ``nozzle_temperature = 210,210``, so both delimiters are the slicer's
    own, not a guess). A bracketed list is a slicer dump of the same list; read it the same
    way. Positions are kept: an empty piece (``PLA;;PETG``) stays an empty string at its index,
    because for a per-tool key position *is* the tool number and dropping it would hand tool 2's
    material to tool 1 (review finding, PR #11). Callers decide what an empty entry means:
    ``_config_single`` ignores them as a bare comma always was, ``_config_per_tool`` refuses."""
    text = raw.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text.strip():
        return []
    return [piece.strip() for piece in re.split(r"[,;]", text)]


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


# The one printer this adapter reads the published firmware of and uploads to (design 1): a
# file's own printer_model config value must name it exactly, or the file is refused at upload.
CC2_PRINTER_MODEL = "Elegoo Centauri Carbon 2"
_MODEL_HINT = ("Re-slice with the stock Elegoo Centauri Carbon 2 printer profile: this workflow "
               "reads that printer's published firmware and uploads only to it.")

# The last five are the stock-template keys design 5 reports on: their raw (escaped) values
# are collected for cc2_stock_templates.compare_verdict, which never refuses — see that
# module for what is compared and how loosely.
_WANTED_KEYS = frozenset({"filament_type", "nozzle_temperature", "nozzle_temperature_initial_layer",
                          "nozzle_temperature_range_high", "printer_model",
                          "machine_start_gcode", "machine_end_gcode", "change_filament_gcode",
                          "layer_change_gcode", "before_layer_change_gcode",
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
        pieces = [piece for piece in _config_value(raw) if piece]
        if not pieces:
            raise _refuse(f"its '{key} =' config line carries no value")
        for piece in pieces:
            if piece not in values:
                values.append(piece)
    if len(values) > 1:
        raise _refuse(f"its {label} is not one value but {values[:4]} — a file that lists more "
                      "than one cannot be identified with a single setting, and this is a plate "
                      "key: every tool the file selects shares one bed")
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


def _config_merged_values(config: dict[str, list[str]], key: str, label: str) -> list[str]:
    """One key's values merged across its config-section repeats, position by position, before
    any tool-number rule is applied: entry ``i`` is position ``i`` of the slicer's list. The
    slicer writes a per-tool key as one list — comma or semicolon separated (``_config_value``)
    — and repeats the line across config sections; a missing key, an empty value or position, or
    repeats that disagree for the same position (on value or on length) are the same inconsistent
    file ``_config_single`` refuses for a single key. ``_config_per_tool`` is this plus the
    tool-aware tail; the design 2 and design 3 in-scan ``M104`` checks reach for this half alone,
    refusal-free, through ``_declared_list_or_none``."""
    occurrences = config.get(key)
    if not occurrences:
        raise _refuse(f"it carries no {label} (no '; {key} =' config line), and print settings "
                      "are read from the file, never guessed")
    values: list[str] = []
    for raw in occurrences:
        pieces = _config_value(raw)
        if not pieces:
            raise _refuse(f"its '{key} =' config line carries no value")
        for i, piece in enumerate(pieces):
            if not piece:
                raise _refuse(f"its {label} (key '{key}') leaves position {i} empty, and for a "
                              f"per-tool key position {i} is tool {i}'s value — a tool whose "
                              "value the file leaves blank is refused, never filled from a "
                              "neighbour")
        if values and len(pieces) != len(values):
            raise _refuse(f"its '{key} =' config lines disagree on how many tools there are "
                          f"({len(values)} and {len(pieces)} values), and a file that disagrees "
                          "with itself cannot be identified — every repeat must carry the same "
                          "list in full, never a shorter one borrowing the other's tail")
        for i, piece in enumerate(pieces):
            if i >= len(values):
                values.append(piece)
            elif values[i] != piece:
                raise _refuse(f"its {label} is not one value per tool: the '{key} =' config "
                              f"lines name {values[i]!r} and {piece!r} for tool {i}, and a file "
                              "that disagrees with itself cannot be identified")
    return values


def _config_per_tool(config: dict[str, list[str]], key: str, label: str,
                     tools: list[int]) -> list[str]:
    """``_config_merged_values`` held to the file's own tool selects: the result is indexable by
    tool number, every index up to the file's highest selected tool must exist (a list shorter
    than that is a tool whose value the file never says, refused with the key named), and one
    value with one tool is the ordinary single-tool case and works whatever that tool's number
    is (a T1-only file's ``; filament_type = PLA`` is that tool's). More values than the file's
    highest tool are the spare filaments of a larger project: tools the file never selects, read
    and left alone. Callers pass the file's own selected tool numbers (non-empty)."""
    values = _config_merged_values(config, key, label)
    highest = max(tools)
    if len(tools) == 1 and len(values) == 1:
        values = values * (highest + 1)  # the file's one tool, whatever its number, prints at it
    elif len(values) < highest + 1:
        raise _refuse(f"its {label} (key '{key}') lists {len(values)} value(s) ({values[:4]}) "
                      f"while the file selects tool {highest}, so tool {highest}'s value is not "
                      "in the file — refusing rather than guessing which listed value a tool "
                      "prints at")
    # A list LONGER than the highest selected tool is the ordinary shape of a project with spare
    # filaments (a two-colour print from a four-filament project, a one-colour print from a
    # two-filament one — the CANVAS sync fills all four slots): the extra entries belong to tools
    # the file never selects, which never extrude and are never mapped, and a purge must still
    # name a selected tool (checked below in the scan). So they are read and left in place,
    # never refused (review finding, PR #11): the whole list is returned, callers index it by
    # tool number, and the spare entries are only ever range-checked as numbers, never mapped.
    return values


def _config_temp_per_tool(config: dict[str, list[str]], key: str, label: str,
                          tools: list[int], *, maximum: float) -> list[float]:
    """``_config_per_tool`` with each value then held to the same temperature sanity
    ``_config_temp`` applies to a single value."""
    temps: list[float] = []
    for raw in _config_per_tool(config, key, label, tools):
        try:
            value = float(raw)
        except ValueError:
            raise _refuse(f"its {label} is {raw!r}, which is not a temperature") from None
        if not 0.0 < value <= maximum:
            raise _refuse(f"its {label} is {value}, outside the 0–{maximum:g} °C a printer of "
                          "this family could hold — refusing rather than trusting it")
        temps.append(value)
    return temps


def _declared_list_or_none(config: dict[str, list[str]], key: str) -> list[str] | None:
    """``_config_merged_values`` for the command scan, WITHOUT the refusal: a key the file does
    not carry, leaves empty, or writes contradictorily is simply *no declared list* here, because
    the refusing, tool-aware read stays after the scan exactly where it always was and refuses
    such a file naming the key there. The scan only ever asks this list a question; a refusal
    before the commands are read would move which refusal fires first for files that are
    refused anyway, and that ordering is existing tested behaviour."""
    try:
        return _config_merged_values(config, key, key)
    except PrinterError:
        return None


def _declared_for_tool(values: list[str] | None, tool: int) -> float | None:
    """One position of a merged per-tool list as a temperature, or None when the file declares
    nothing usable for this tool: position ``tool`` when the list reaches that far; a one-value
    list stands for whatever tool the file selects, because the only files that pass the
    after-scan ``_config_per_tool`` with one value select exactly one tool, and there that value
    is replicated to it; a value that is not a number is no declared temperature (the after-scan
    read refuses such a list anyway)."""
    if values is None:
        return None
    if tool < len(values):
        raw = values[tool]
    elif len(values) == 1:
        raw = values[0]
    else:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _other_tool_declares(values: list[str] | None, current_tool: int, value: float) -> bool:
    """Design 3a: does some tool **other than** the one in force declare a steady
    ``nozzle_temperature`` of exactly ``value``? Read refusal-free off the merged position list
    like design 2's transition check (``_declared_list_or_none``): a value that is not a number
    declares nothing (the after-scan read refuses such a list anyway), and a one-value list
    declares the file's single selected tool — never a second one — so it can never arm a
    pending change; the file that selects two tools beside one value is refused after the scan
    naming the key, exactly as it always was."""
    if values is None or len(values) == 1:
        return False
    for index, raw in enumerate(values):
        if index == current_tool:
            continue
        try:
            declared = float(raw)
        except ValueError:
            continue
        if declared == value:
            return True
    return False


def _parse_S(token: str) -> float:
    """One ``S<temperature>`` parameter, whatever case its letter arrived in. This is the one place a
    parameter letter's case is folded, so every caller reads one spelling: Klipper matches a command
    and its parameters case-insensitively, which is why ``G1 x1 e6`` is already an extrusion here and
    ``m109s240`` is already a wait. Folding the letter never folds the count — a command carrying two
    S parameters still reaches its caller carrying two, and two is a refusal."""
    if token[:1].upper() != "S" or len(token) < 2:
        raise _refuse(f"a heater command names {token!r}, which is not an S<temperature> parameter")
    try:
        value = float(token[1:])
    except ValueError:
        raise _refuse(f"a heater command names S{token[1:]!r}, which is not a number") from None
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf: not a temperature
        raise _refuse("a heater command names a temperature that is not a finite number")
    return value


def _move_params(rest: str) -> list[tuple[str, float]]:
    """A move line's parameters, tokenised so an E is seen whether the slicer spaced it
    (``X127 E6``) or welded it into a compact run (``X1E6``). Returns the (letter, value) pairs
    with letters upper-cased, or None when a character that is neither whitespace nor a
    letter+number run remains — a parameter string this parser cannot cleanly read, which a move
    must not be trusted to have passed extrusion-free. Dots inside a number are fine
    (``R0.4``, ``F131.773``, a ``G38.2`` head never reaches here); a bare letter with no number
    (an ``A`` flag) is not a move parameter and lands in the leftover, which is the safe reading
    for a move: an unparseable move is refused, never assumed inert."""
    params: list[tuple[str, float]] = []
    pos = 0
    while pos < len(rest):
        if rest[pos].isspace():
            pos += 1
            continue
        m = _MOVE_PARAM_RE.match(rest, pos)
        if not m:
            return None
        params.append((m.group(1).upper(), float(m.group(2))))
        pos = m.end()
    return params


def _is_heater_word(head: str) -> bool:
    """True for a word-form command (Klipper extended G-code) whose name commands a heater —
    ``SET_HEATER_TEMPERATURE`` above all, so a state-changing command this adapter cannot resolve
    to one unambiguous, M109-verifiable setpoint is refused rather than skipped."""
    return bool(_WORD_HEAD_RE.fullmatch(head)) and bool(_HEATER_KEYWORD_RE.search(head))


def _compact_heater(line: str, head: str, rest: str) -> tuple[str, str] | None:
    """Normalise a welded, space-free heat command (``M104S0``, ``m109s240``) into its spaced
    form so the ordinary parser sees it. Returns (head, rest) or None when the line is not a
    compact heater code. Raises on a compact code whose parameters cannot be cleanly spaced
    (``M109S240R200``): that is the same second-numbered-parameter ambiguity the spaced form is
    refused for, and silently skipping a compact heater change is the very fail-open this closes.
    The parameters after the welded one are **kept** — ``M109S260 S0`` is one command carrying two S
    parameters, which is how this printer's own firmware reads it too (its parameter splitter
    ``elegoo/common/utilities.cpp::regex_split`` cuts letters from numbers and ignores the spacing, and
    ``gcode.cpp::process_commands`` folds the pairs into a map that simply overwrites, so the machine
    would hold the LAST S: 0, off). Dropping the tail read the hottest heater in the file as a
    cooldown-free ``M109 S260``; preserving it lets the ordinary spaced rules refuse both the duplicate
    S and a second numbered parameter, exactly as they already do when the same line is spaced.
    A clean code normalises to (head, rest); the spaced parser then applies its own S/extras
    rules unchanged."""
    m = _COMPACT_HEATER_RE.match(head)
    if not m:
        return None
    code, letter = m.group(1), m.group(2).upper()
    head_norm = f"M{code}"
    tail = head[m.end():]  # the value welded to the first parameter letter, plus any trailing junk
    vm = re.match(r"[-+]?(?:\d+\.?\d*|\.\d+)", tail)
    if not vm:
        raise _refuse(
            f"the compact heater command {line[:40]!r} carries {letter} with no temperature after "
            "it, so its setpoint cannot be read — refusing rather than skipping a command that "
            "changes heater state")
    remainder = tail[vm.end():]
    if remainder.strip():  # an unspaced second parameter (``S240R200``): the refused ambiguity
        raise _refuse(
            f"the compact heater command {line[:40]!r} packs its parameters with no spaces "
            f"({head!r}), which this parser cannot unambiguously split into the setpoints the "
            "heater would actually hold — refusing rather than skipping a command that changes "
            "heater state")
    spaced = f"{letter}{vm.group(0)}"
    if rest.strip():       # ``M109S260 S0``: the welded S is not the command's only parameter
        spaced = f"{spaced} {rest.strip()}"
    return head_norm, spaced


# --- M6211, Elegoo's flush/purge: what the firmware reads, and in what order it heats --------------
#
# Read against Elegoo's published firmware for this printer (ElegooOfficial/CentauriCarbon2,
# ``elegoo/extras/mmu.cpp::CMD_M6211`` lines 3229-3383, its clamp helper ``limit_check`` at 3222, the
# filament switch it runs at ``CMD_canvas_switch_filament`` 2811, and the ``[g180]`` macro bodies in
# ``debugfile/printer_dsp.cfg`` 644-675). The purge's letters are not decoration and they are not
# unread: they name the setpoints the purge heats to and the tool whose filament it flushes. What the
# firmware then commands, in order, when the purge's channel is not already the one feeding (when it
# is, it returns having done nothing at all, and nothing in a file can say which case a print hits):
#
#   1. ``M204``, a Z travel, ``G92 E0`` and a ``G1 E-<C>`` retract — at the temperature already in
#      force, before the purge heats anything;
#   2. ``A=1``: ``G180 S10`` (a travel to the flush bin; the published body moves X/Y only), then
#      ``M109 S<Q>`` — a **wait** at Q — and ``M106 S<I>``.
#      ``A=0``: ``M104 S<Q>`` — **no wait** — and ``M106 S<I>``;
#   3. ``CANVAS_SWITCH_FILAMENT T=<T>``: cut, unload and load tool T's filament — a real tool select;
#   4. ``M104 S<R>`` — **no wait** — and every flush extrusion of the purge then runs at R;
#   5. ``M109 S<S>`` — a **wait** at S — the purge's final extrusion, then ``G180 S818`` (travels).
#
# So the purge's extrusions run at Q, at R and at S, and the purge's temperature story is whole
# only when (design 3) Q is the verified wait still in force — the outgoing tool's own temperature,
# which the purge's retracts, cut and channel-check steps run behind — and S is either that same
# wait (an equal-temperature change, today's shape) or the steady ``nozzle_temperature`` the file
# declares for the tool the purge switches in (a mixed-temperature change, where the purge's own
# ``M109 S`` is the wait that arms the print's continuation at S). An R elsewhere is an extrusion at
# a target no wait ever proved (the same rule that refuses ``M104 S220`` followed by ``G1 E6``,
# beside the vendor's declared-range-high exception below), and a Q elsewhere moves the hot end, in
# the ``A=0`` case with no wait at all, immediately before a filament switch that pushes filament.
_PURGE_FIELDS: dict[str, tuple[str, float, float, bool]] = {
    # letter: (what the firmware calls it, its clamp low, its clamp high, must be a whole number?)
    "T": ("the purge's tool (next_extruder)", 0.0, 31.0, True),
    "Q": ("the purge's first setpoint (old_filament_temp)", 200.0, 350.0, False),
    "R": ("the purge's flush-target setpoint (nozzle_temperature_range_high)", 200.0, 350.0, False),
    "S": ("the purge's final waited setpoint (new_filament_temp)", 200.0, 350.0, False),
    # A picks which of the two step-2 spellings runs, i.e. whether Q is waited at. It is not required:
    # with Q pinned to an already-waited target, both branches leave the target and the wait where
    # they are, and neither heats anything else. The published S10/S818 macro bodies travel only.
    "A": ("the purge's extrude-check flag (need_extrude_check)", 0.0, 1.0, True),
}
# The purge's other letters, read by the same source with the same clamp-and-default: K and L are
# flush lengths, M and N extrusion feedrates, I and J two fan speeds, P one ``G4`` dwell, C and D two
# retract lengths. None commands a heater and none selects a tool, and the purge as a whole is already
# counted as one gated extrusion, so these are read for being numbers and never for their values: the
# receipt claims what the hot end provably ran at, never how much filament the purge pushed. These
# nine beside the five setpoint letters of ``_PURGE_FIELDS`` are the fourteen letters and no others
# that ``CMD_M6211`` looks up, so a fifteenth is refused rather than assumed inert — unread is not
# inert, which is the rule every other command in this file is held to.
_PURGE_OTHERS = frozenset({"K", "L", "M", "N", "I", "J", "P", "C", "D"})

# Why each setpoint letter matters, in the refusal that names it.
_PURGE_LETTER_WHY = {
    "Q": "the firmware commands Q first — an M109 wait when A=1, a bare M104 otherwise — so it moves "
         "the hot end off the verified temperature before the purge's filament switch runs",
    "R": "the firmware commands R with a bare M104 and never waits at it, and every flush extrusion "
         "of the purge runs at it — an extrusion at a target no wait ever proved, unless it is the "
         "file's own declared range high (the one exception the vendor's change_filament_gcode "
         "writes, and it is read per tool and recorded as a flush temperature — see the "
         "flush-R rule in parse_gcode_settings)",
    "S": "S is the purge's own final M109 and its last extrusion runs at it, so it is the "
         "temperature the print continues at: either the wait the file has already verified, or "
         "the incoming tool's own declared steady print temperature, which this very command "
         "waits at (design 3)",
}

_PURGE_PARAM_RE = re.compile(r"([A-Za-z])([-+]?(?:\d+\.?\d*|\.\d+))")


def _purge_pairs(rest: str) -> list[tuple[str, str]] | None:
    """A purge line's parameters as ``(letter, value)`` pairs, letters folded to upper case, packed
    runs (``A1L200T0``) read like the spaced form because the firmware's own splitter cuts letters from
    numbers and pays no attention to the spaces. ``None`` for anything that is not a clean run of
    letter+number pairs — a bare flag letter, an ``=``, a value that is not a number. That is a
    refusal, not a shrug: ``GCodeCommand::get_double`` (gcode.cpp:200-227) catches the conversion
    failure and uses the uninitialised double anyway, so on such a line not even the purge's own
    temperatures are knowable."""
    pairs: list[tuple[str, str]] = []
    pos = 0
    while pos < len(rest):
        if rest[pos].isspace():
            pos += 1
            continue
        m = _PURGE_PARAM_RE.match(rest, pos)
        if not m:
            return None
        pairs.append((m.group(1).upper(), m.group(2)))
        pos = m.end()
    return pairs


@dataclass(frozen=True)
class _Purge:
    """One ``M6211`` flush read the way the firmware reads it: the tool whose filament it switches in
    (``T``) and the three setpoints it commands, in the order it commands them — ``first`` (Q) first,
    ``flush`` (R) for the flush extrusions with no wait, ``final`` (S) last behind its own M109."""
    tool: int
    first: float
    flush: float
    final: float


def _purge_parameters(line: str, rest: str) -> _Purge:
    """The purge's own tool and setpoints, each one read the way the firmware reads it or the file is
    refused. A letter the firmware requires is missing means the value comes from the printer's stored
    flush config, which no reader of this file can see; a value outside the firmware's window is one
    its ``limit_check`` silently moves inside the window (so the purge would not run at the number
    printed); a letter named twice is two claims about one setpoint, which the firmware resolves by
    overwriting and this parser will not."""
    pairs = _purge_pairs(rest)
    if pairs is None:
        raise _refuse(
            f"the purge command {line[:36]!r} carries a parameter that is not one letter+number pair "
            "(a bare flag letter, a stray sign, a value that is not a number), and the firmware pairs "
            "its purge parameters positionally and converts them with std::sto*, which on such a "
            "value leaves an uninitialised number its get_double accepts and continues with — so none "
            "of the purge's temperatures can be read from this file")
    if unknown := sorted({letter for letter, _ in pairs
                          if letter not in _PURGE_FIELDS and letter not in _PURGE_OTHERS}):
        raise _refuse(
            f"the purge command {line[:36]!r} carries {unknown[0]}, a parameter letter "
            "CMD_M6211 never looks up; it may be inert and it may be a typo for one of the fourteen "
            "that are not, and this parser does not guess which about a hot end that is about to "
            "extrude")
    letters = [letter for letter, _ in pairs]
    if dupes := sorted({letter for letter in letters if letters.count(letter) > 1}):
        raise _refuse(
            f"the purge command {line[:36]!r} names {dupes[0]} twice; the firmware keeps one of them "
            f"(its parameter table simply overwrites), and this parser will not choose which of the "
            f"file's two {dupes[0]} values the purge heats to")
    got = dict(pairs)
    values: dict[str, float] = {}
    for letter, (label, low, high, whole) in _PURGE_FIELDS.items():
        if letter not in got:
            if letter != "A":
                raise _refuse(
                    f"the purge command {line[:36]!r} carries no {letter}, {label}, and with no "
                    "parameter the firmware takes that value from the printer's own stored flush "
                    "config — so which tool it flushes, or what it heats to, is not knowable from the "
                    "file")
            continue
        raw = got[letter]
        number = float(raw)
        if number != number or number in (float("inf"), float("-inf")):
            raise _refuse(f"the purge command {line[:36]!r} names {letter}{raw}, which is not a "
                          "finite temperature or tool number")
        if whole and number != int(number):
            raise _refuse(
                f"the purge command {line[:36]!r} names {letter}{raw}, and the firmware reads that "
                f"one with an integer conversion that truncates, so {label} as the firmware uses it "
                "is not the value written")
        if not low <= number <= high:
            raise _refuse(
                f"the purge command {line[:36]!r} names {letter}{raw}: the firmware clamps {label} "
                f"into {low:g}–{high:g} and does so silently, so the purge would not run at the value "
                "written")
        values[letter] = int(number) if whole else number
    return _Purge(tool=int(values["T"]), first=values["Q"], flush=values["R"], final=values["S"])


def _purge_tool_refusal(tool: int, tools: list[int]) -> PrinterError:
    """A purge whose ``T`` names a tool the file never selected. The firmware does not file that T
    away as a note about the flush: mid-purge it runs ``CANVAS_SWITCH_FILAMENT T=<n>``, which cuts,
    unloads and loads that tool's filament (mmu.cpp:3276, and CMD_canvas_switch_filament at 2811
    setting ``feed_channel_request = get_T_channel(T)``). Several tools are legal now, but every
    tool whose filament the file switches must be one the file's own ``T<n>`` lines select — a
    purge naming a tool none of them names runs a filament switch the file nowhere declares."""
    selects = ", ".join(f"T{t}" for t in tools[:4])
    return _refuse(
        f"it flushes tool {tool} (an M6211 T{tool}) while its own tool select reads {selects}; that "
        "T is not a comment about the flush — the firmware runs CANVAS_SWITCH_FILAMENT T="
        f"{tool} inside the purge, cutting, unloading and loading that tool's filament — so the "
        "file switches in a tool it never selects, and every tool a file flushes it must also "
        "select")


def _flush_range_refusal(quote: str, value: float, target: float, bad: PrinterError) -> PrinterError:
    """A purge whose R leans on the declared-range-high exception, met by a file whose
    ``nozzle_temperature_range_high`` lines cannot answer (missing, empty, self-contradictory or
    unphysical). The inner refusal names the key; this wrapper adds the purge line that needed it,
    so one message carries both which flush cannot be vouched for and which config failed it."""
    inner = bad.message
    if inner.startswith(_REFUSE_PREFIX):
        inner = inner[len(_REFUSE_PREFIX):]
        inner = inner[0].lower() + inner[1:]
    return _refuse(f"the purge command {quote!r} sets R to {value:g} °C while the nozzle target "
                   f"verified by an M109 wait at that point in the file is {target:g} °C, and the "
                   f"file cannot establish the one exception this adapter reads for R: {inner}")


def parse_gcode_settings(lines: Iterable[str]) -> Cc2GcodeSettings:
    """Read one CC2 file's print settings — single- or multi-tool — from its own bytes, fail-closed.

    Commands first: only comment-stripped, line-initial G-code counts (the file's config block
    quotes start G-code templates full of M-codes; none of those were ever issued). The nozzle
    commands are M104/M109 and the bed's M140/M190; every non-zero S setpoint is kept in first
    order seen, S0 read out as the cooldown it is, and a heater command without a sane S is an
    unparseable file. The same scan walks the temperature commands **in order**, carrying the
    nozzle target and the most recent verified ``M109`` wait: every extrusion (a G0/G1/G2/G3 move
    with E, and
    every ``M6211`` purge) must find a wait at exactly the target in force, or the file is
    refused — which is what keeps the real 140 °C non-extruding preheat legal while no cold
    extrusion can slip through, and no printing-temperature range is hardcoded anywhere here.
    The purge is read for its own facts as well, against the published firmware
    (``_PURGE_FIELDS``): its ``Q``/``R``/``S`` are the three setpoints it heats to, in that order
    and with ``R`` never waited at. ``Q`` must equal the verified wait in force — the outgoing
    tool's temperature, which the purge's retracts, cut and channel-check steps run behind. ``S``
    is the temperature the print continues at (the firmware's own ``M109 S`` inside the purge
    waits at it), so it must be either that same wait (an equal-temperature change) or the steady
    ``nozzle_temperature`` the file declares for the purge's tool (a mixed-temperature change,
    design 3); after a purge the wait in force is ``S``, the current tool is the purge's ``T``,
    and a pending wipe-tower setpoint — a tower ``M104`` to a second tool's declared temperature,
    behind which only retractions may run — is retired. ``R`` alone carries one exception,
    because the vendor's own ``change_filament_gcode`` writes ``R=[nozzle_temperature_range_high]``:
    an off-target R is allowed only when it equals the file's declared ``nozzle_temperature_range_high``
    of **both** the tool being left and the tool switched to (read per tool, below), and that value
    is then recorded as a flush temperature of both tools (``ToolSettings.flush_nozzle_temps``,
    design amendment 9) — never as a printing temperature in ``extrusion_nozzle_temps`` and never
    as a commanded setpoint, so a start can report it against each tray's rated maximum without
    mistaking the vendor's flush for the print. A purge's ``T``
    is a filament switch, so it must name a tool the file itself selects, and after a purge the
    current tool is the purge's ``T``. Several ``T<n>`` selects are legal — the tools are the
    distinct selects in first-seen order, and each extrusion is attributed to the tool in force at
    its line; an extrusion before any select or purge cannot be attributed to a spool and refuses,
    and none at all is missing tool metadata (a purge's ``T`` is not a substitute for the file's own
    ``T<n>`` line). ``G180`` is the CC2
    firmware's own macro dispatcher and is decided
    one number at a time (``_G180_MACROS``): the numbers whose published ``[g180]`` bodies this
    adapter has read pass, every other number refuses the file, and the end-of-print unload (S9)
    drops the nozzle accounting it cannot carry across, so nothing extrudes after it.

    Config second: the plate's first-layer and steady bed temperatures, then — once the file's
    own tool selects are known — the material and the nozzle first-layer and steady setpoints
    read per selected tool (``_config_per_tool``), each refusing to be missing, ambiguous or
    unphysical; the same per-tool read decides any deferred flush-R exception, against the file's
    declared ``nozzle_temperature_range_high`` list. Also from config, and never a refusal: the
    stock-template report (design 5) — ``vendor_templates`` says whether the five machine-level
    template config keys the file carries match the shipped OrcaSlicer 2.4.2 CC2 templates
    (``cc2_stock_templates``), and ``templates_compared`` counts how many of the five the file
    actually wrote. The config reads that reach into the scan
    are design 2's (the layer-two ``M104`` transition) and design 3's (the pending tool-change
    ``M104``): the two nozzle-setpoint keys are merged refusal-free before the scan (``_declared_list_or_none``) and indexed per tool lazily
    (``_declared_for_tool``) — the choice over hoisting the refusing per-tool reads above the
    scan, which would need a tool list the scan has not built yet and would move which refusal
    fires first; a missing or broken list there simply means no transition fires, and the
    unchanged after-scan read still refuses the file naming the key.
    Cross-check last: every one of the four print setpoints must also appear among the commands —
    a file whose config promises 260 while its G-code never heats to 260 is an inconsistent file,
    and inconsistency is exactly what this parse exists to catch. Command setpoints beyond the
    print setpoints (the observed 140 °C flush hold) stay listed as
    evidence and never block: what the printer holds during a purge step is its own business, and
    the start is what checks them against spool ranges. The file's own declared filament ranges
    (``nozzle_temperature_range_*``) are never used to accept a print temperature — the authority
    for a spool is the CANVAS tray the printer reports at start, not text in a file someone may
    have edited — with the single exception the flush-R rule above reads the declared range high
    for, which ends up recorded as each tool's flush temperature for the start to report."""
    text_lines = list(lines)  # small files in tests, real files streamed by the upload; either way once
    config = _collect_config(text_lines)

    # The printer-model gate (design 1) reads first, before any other setting: this workflow
    # judges every command against one printer's published firmware and uploads to the one
    # printer it is configured to reach, so a file sliced for any other machine is refused for
    # the fact that matters whatever its other settings would have said. A missing line refuses
    # naming the absence, a list that disagrees with itself refuses naming the values, and a
    # value that is not the one model refuses naming what the file said. The gate lives at the
    # upload; the receipt is unchanged.
    try:
        printer_model = _config_single(config, "printer_model", "printer model")
    except PrinterError as bad:
        raise PrinterError("protocol_error", bad.message, hint=_MODEL_HINT,
                           reason="printer_model") from None
    if printer_model != CC2_PRINTER_MODEL:
        raise _model_refuse(f"its printer model is {printer_model!r}, not {CC2_PRINTER_MODEL!r}: "
                            "this workflow reads one printer's published firmware and uploads to "
                            "that one printer, so a file sliced for another machine refuses no "
                            "matter how sane its other settings are")

    # the plate decides the bed keys and needs no tool knowledge, so it is read up front; the
    # per-tool keys (material, the two nozzle setpoints) are read after the scan below, because
    # "one value per tool" is only knowable once the file's own tool selects have been seen
    plate_word = _config_single(config, "curr_bed_type", "build-plate type").lower()
    if plate_word not in _PLATE_BED_KEYS:
        raise _refuse(f"it prints on plate {plate_word!r}, whose bed-temperature config keys this "
                      "adapter does not know, so its bed setpoints cannot be identified")
    first_key, steady_key = _PLATE_BED_KEYS[plate_word]

    bed_first = _config_temp(config, first_key, "initial-layer bed temperature", maximum=_BED_MAX)
    bed = _config_temp(config, steady_key, "steady bed temperature", maximum=_BED_MAX)

    # designs 2 and 3 read these two keys inside the scan below, where the tool in force is known
    # but the refusing per-tool read still is not (the tool list is only complete when the scan
    # ends): merged position lists, refusal-free, indexed by tool lazily — see
    # _declared_list_or_none and _declared_for_tool for why neither the refusal nor the tool
    # rules move up here. nozzle_temperature_range_high is not one of them: neither design 2 nor
    # design 3 touches the flush-R rule, and reading it up front would make a file that never
    # purges answer for a key it has never needed — the flush-R read stays the after-scan,
    # only-when-used exception it is.
    firsts_pre = _declared_list_or_none(config, "nozzle_temperature_initial_layer")
    nozzles_pre = _declared_list_or_none(config, "nozzle_temperature")

    nozzle_cmds: list[float] = []
    bed_cmds: list[float] = []
    tools: list[int] = []
    # the tools the file's purges switch filament to (their M6211 T parameters). Kept apart from
    # ``tools`` on purpose: a purge is not a substitute for the file's own ``T<n>`` select — a file
    # whose only tool mention is inside an M6211 has no tool, exactly as before — but a purge naming a
    # tool the file never selects is a second tool, because the firmware switches that filament.
    purge_tools: list[int] = []
    # the command-order story: the nozzle target in force, the value of the most recent verified
    # wait (an M109 at exactly that target, or a purge's own firmware-waited S), the temperatures
    # extrusion ran at, and where the file's own extrusions and nozzle cooldowns fell — the last
    # of these needed because an S0 is only honest after the file is done extruding
    nozzle_target: float | None = None
    waited: float | None = None
    # design 3a's pending tool-change setpoint: a wipe-tower ``M104`` to another tool's declared
    # steady temperature arms it while the old wait stands; retractions run behind that wait, any
    # forward extrusion refuses, and an ``M6211`` retires it (the firmware waits at its ``S``)
    pending_tool_temp: float | None = None
    extrusion_temps: list[float] = []
    # per-tool verified-extrusion story: tool index -> temperatures that tool's extrusion ran
    # behind a verified wait, first order seen (the file-wide ``extrusion_temps`` stays the union
    # of every tool's story, which is what the v2 receipt's flat field carries until receipt v3
    # gives each tool its own)
    per_tool_temps: dict[int, list[float]] = {}
    # per-tool flush story (design amendment 9): tool index -> every accepted flush R that tool
    # was left or entered at, distinct, first order seen. Kept apart from the extrusion story:
    # a flush R was never waited at, so it is no printing temperature
    per_tool_flush: dict[int, list[float]] = {}
    # whose filament the file is running right now: a ``T<n>`` select sets it, and after a purge
    # it is the purge's ``T`` (the firmware switches that filament mid-purge). An extrusion
    # before either cannot be attributed to a spool and refuses.
    current_tool: int | None = None
    # design 3's flush-R exceptions held for after the scan: (quote, R, tool being left, tool
    # switched to). Each tool's declared nozzle_temperature_range_high is read per tool through
    # _config_per_tool, which needs the file's complete tool list — and a real slice's purge
    # names its next tool one line before its own ``T<n>`` select reaches it.
    flush_r: list[tuple[str, float, float, int | None, int]] = []
    cooldown_at: list[int] = []
    last_extrusion_at: int | None = None
    unload_seen = False   # a G180 S9 has run (or may have): nothing past it is knowable
    mesh_seen = False     # a BED_MESH_CALIBRATE has dropped the wait since the last M109 (design 4)

    def _wait_lost() -> str:
        # how the two extrusion gates below name a missing wait: the value an M109 last proved,
        # the mesh line that dropped it (design 4: the calibration body is a binary-only library
        # that may heat, so no wait carries across it), or no M109 at all
        if waited is not None:
            return f"{waited:g} °C"
        if mesh_seen:
            return ("reset by the BED_MESH_CALIBRATE above it, whose binary-only calibration "
                    "library may heat — no M109 wait is carried across that line")
        return "no M109 at all"

    for idx, raw_line in enumerate(text_lines):
        line = raw_line.split(";", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 1)
        head, rest = parts[0].upper(), (parts[1] if len(parts) > 1 else "")
        if head not in ("M104", "M109", "M140", "M190") and (norm := _compact_heater(line, head, rest)):
            # a heat command welded to its number with no space (M104S0) is the same heater change
            # the spaced form is; normalise it here so it is gated, never skipped as an unknown
            head, rest = norm
        if _is_heater_word(head):
            raise _refuse(f"the command {line[:40]!r} names a heater this adapter cannot resolve "
                          "to one M109-verifiable setpoint — Klipper extended heater commands are "
                          "refused rather than skipped, so no extrusion can ride on a heater "
                          "change the file never proved it waited at")
        if head in ("M104", "M109", "M140", "M190"):
            # parameter letters fold to upper case (see _parse_S), so `M104 s140` is the setpoint
            # `M104 S140` is. Only letters fold, never the count: the S parameters are still counted
            # one by one, so `M104 S140 s200` is two setpoints in any mixture of cases and refuses
            params = [(i, t.upper()) for i, t in enumerate(rest.split())]
            s_at = [i for i, t in params if t.startswith("S")]
            if len(s_at) != 1:
                raise _refuse(f"the command {line[:40]!r} does not carry exactly one S<temp> "
                              "parameter, so its setpoint cannot be trusted")
            value = _parse_S(params[s_at[0]][1])
            limit = _NOZZLE_MAX if head in ("M104", "M109") else _BED_MAX
            if not 0.0 <= value <= limit:
                raise _refuse(f"the command {line[:40]!r} names {value}, outside any 0–{limit:g} "
                              "°C this printer family could hold")
            others = [t for i, t in params if i != s_at[0]]
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
                    pending_tool_temp = None  # a cooldown replaces any pending tool-change setpoint
                elif head == "M104":      # a setpoint without a wait — an allowed preheat, but
                    if (waited is not None and current_tool is not None and value != waited
                            and waited == _declared_for_tool(firsts_pre, current_tool)
                            and value == _declared_for_tool(nozzles_pre, current_tool)):
                        # design 2: the slicer's own layer-two transition on every Klipper
                        # printer — from a verified wait at the current tool's declared
                        # initial-layer temperature, a bare M104 up to that same tool's declared
                        # steady temperature. Both numbers are this file's own per-tool config
                        # (the after-scan read range-checks them) and the firmware's M109
                        # tolerates ±5 °C, so the wait armed at the first carries to the second:
                        # the target moves, the wait follows it, and the new temperature joins
                        # this tool's verified-extrusion story and the file-wide one — recorded
                        # here, not merely allowed, because both temperatures must then fit the
                        # tray at start. Any other bare M104 keeps today's rule below.
                        nozzle_target = waited = value
                        pending_tool_temp = None  # an armed wait retires any pending change
                        if value not in extrusion_temps:
                            extrusion_temps.append(value)
                        if value not in per_tool_temps.setdefault(current_tool, []):
                            per_tool_temps[current_tool].append(value)
                    elif (waited is not None and current_tool is not None and value != waited
                            and _other_tool_declares(nozzles_pre, current_tool, value)):
                        # design 3a: the wipe-tower tool change as the slicer writes it — the
                        # observed order is M104 S<next>, a G1 E-2 retract, travel, then the
                        # change_filament M6211 whose S is this very number. The target moves
                        # toward the incoming tool's declared print temperature while the old
                        # verified wait stays armed for retractions only (the hot end still
                        # holds it, and the firmware's own M109 tolerance is ±5 °C); any
                        # forward extrusion before the purge refuses, and the value joins no
                        # temperature story until the purge waits at it. Any other bare M104
                        # keeps today's rule below.
                        nozzle_target = value
                        pending_tool_temp = value
                    else:                  # any other bare M104: a target moved without a wait
                        nozzle_target = value  # revives the requirement; only a matching M109
                        # re-arms extrusion (the wait must still come before the extrusion)
                        pending_tool_temp = None
                else:                     # M109: the target, and a verified wait at exactly it
                    nozzle_target = waited = value
                if head == "M109":
                    mesh_seen = False     # a fresh wait now stands behind the target again
                    pending_tool_temp = None  # and no tool-change setpoint is pending any more
            continue
        if head == "G180":
            # The CC2's macro dispatcher, decided number by number: _g180_macro has already refused
            # every number whose published body this adapter has not read, so what is left is a body
            # whose heater and filament effects are known. S9 is the one that ends the print — its
            # conditional unload can wait at its own temperature and pull the filament out — so the
            # nozzle state the extrusion gates read is dropped at it, and those gates refuse outright
            # past it rather than trusting a target the macro may have moved.
            if _g180_macro(line, rest).ends_print:
                unload_seen = True
                nozzle_target, waited = None, None
                pending_tool_temp = None  # nothing past the unload is knowable, pending included
            continue
        if head == "BED_MESH_CALIBRATE":
            # design 4 (and the rewritten reason on _SAFE_WORD_COMMANDS): no verified M109 wait
            # is carried across this line — the calibration body is a binary-only library whose
            # strings carry heater commands, so it may heat. The commanded target stands (the
            # wait gates compare against it, not against None), but the wait in force is
            # dropped: the next extrusion or purge needs a fresh M109 at the target, which the
            # stock start template issues right after the mesh. Head-folded like every command
            # word here, so the lowercase spelling resets it just the same.
            waited, mesh_seen = None, True
            continue
        if head == "M6211":  # Elegoo's flush: its T selects filament and its Q/R/S heat the nozzle
            if unload_seen:
                raise _after_g180_unload(f"the purge command {line[:36]!r}")
            last_extrusion_at = idx
            if nozzle_target is None:
                raise _refuse(f"the purge command {line[:40]!r} extrudes while no nozzle "
                              "temperature has even been commanded — a purge runs hot or not "
                              "at all, and which this file means is not knowable")
            if nozzle_target <= 0.0:
                raise _refuse(f"the purge command {line[:40]!r} extrudes at a nozzle target of "
                              f"{nozzle_target:g} °C — that is the heater off, and a purge "
                              "there would be a cold one")
            if pending_tool_temp is None:
                if waited != nozzle_target:
                    raise _refuse(f"the purge command {line[:40]!r} extrudes at a commanded "
                                  f"nozzle target of {nozzle_target:g} °C that no M109 was ever "
                                  "waited at (the most recent verified wait was "
                                  + _wait_lost()
                                  + ") — heat and wait at the purge temperature first")
            elif waited is None:
                raise _refuse(f"the purge command {line[:40]!r} runs mid-tool-change at a "
                              f"pending {pending_tool_temp:g} °C setpoint with no verified wait "
                              "standing (the most recent verified wait was "
                              + _wait_lost() + ") — the purge retracts, cuts and channel-checks "
                              "the outgoing filament behind that wait, so it must still stand")
            # the purge's own parameters, read the way this printer's firmware reads them: its T is
            # a filament switch and its Q, R and S are the setpoints it heats to (see _PURGE_FIELDS
            # for the order the firmware commands them in). Q must be the verified wait in force —
            # the outgoing tool's own temperature, which the purge's first steps run behind. S is
            # the temperature the print continues at (the firmware waits at it inside the purge),
            # so it must be either that same wait — an equal-temperature change, today's shape — or
            # the steady nozzle_temperature the file declares for the tool being switched in — a
            # mixed-temperature change (design 3), where a pending wipe-tower setpoint retires
            # here. R keeps the one exception the vendor's own change_filament_gcode relies on.
            purge = _purge_parameters(line, rest)
            if purge.first != waited:
                raise _refuse(
                    f"the purge command {line[:36]!r} sets Q to {purge.first:g} °C while the "
                    f"verified nozzle wait in force at this point in the file is {waited:g} °C, "
                    f"and {_PURGE_LETTER_WHY['Q']} — the purge retracts, cuts and channel-checks "
                    "the outgoing filament behind that wait, so Q must be the temperature that "
                    "filament printed at")
            steady_dest = _declared_for_tool(nozzles_pre, purge.tool)
            if purge.final != waited and purge.final != steady_dest:
                dest_text = "declared for it" if steady_dest is None else \
                    f"{steady_dest:g} °C the file declares for tool {purge.tool}"
                raise _refuse(
                    f"the purge command {line[:36]!r} sets S to {purge.final:g} °C, which is "
                    f"neither the verified nozzle wait in force here ({waited:g} °C) nor the "
                    f"steady nozzle temperature {dest_text}, the tool it switches in; "
                    f"{_PURGE_LETTER_WHY['S']} — every extrusion must follow a verified M109 "
                    "wait at the temperature it runs at")
            if purge.flush != waited:
                # R alone may stand off the verified wait: the vendor's own change_filament_gcode
                # writes R=[nozzle_temperature_range_high], so every real multi-colour file flushes
                # hotter than it prints. The exception (design amendment 9) is decided per tool,
                # and that is only safely readable once the scan has seen the file's whole tool
                # list, so this flush waits for the check below; R already sits inside the
                # firmware's own 200–350 °C clamp (_PURGE_FIELDS), the sanity fence this file
                # holds every nozzle number to. Any R that fails the check after the scan refuses
                # the file.
                flush_r.append((line[:36], purge.flush, waited, current_tool, purge.tool))
            if purge.tool not in purge_tools:
                purge_tools.append(purge.tool)
            # design 3b: the purge's final extrusion runs behind the firmware's own M109 S (step 6
            # of the _PURGE_FIELDS order), so after the purge S is both the target in force and
            # the verified wait, and any pending wipe-tower setpoint is moot — the purge's own
            # Q/R/S sequence replaced it. The filament being switched out ran its retract, cut
            # and channel-check steps at Q, i.e. at the wait that was in force: that, not S, is
            # the temperature recorded on the outgoing tool when a mixed-temperature change moves
            # the print from one number to the other.
            leaving_temp = waited
            nozzle_target = waited = purge.final
            pending_tool_temp = None
            if purge.final not in extrusion_temps:
                extrusion_temps.append(purge.final)
            # the purge's real extrusions run on the filament it switched in (its own flush and
            # final extrusion, step 3 of the _PURGE_FIELDS order), so the verified temperature is
            # that tool's; after the purge, that tool is the file's current one.
            if purge.final not in per_tool_temps.setdefault(purge.tool, []):
                per_tool_temps[purge.tool].append(purge.final)
            # and the purge first retracts and cuts the filament it switches OUT at the wait that
            # was in force (step 1 of the _PURGE_FIELDS order), so the outgoing tool ran at that
            # too: recorded on that tool as well, so a start compares its tray against it
            # (review finding, PR #11; with Q pinned to the wait and an equal-temperature change
            # this is the same number S the incoming tool continues at — the temperature the
            # outgoing tool was already printing at, recorded now even when it never extruded)
            if current_tool is not None and leaving_temp not in per_tool_temps.setdefault(
                    current_tool, []):
                per_tool_temps[current_tool].append(leaving_temp)
            if leaving_temp not in extrusion_temps:
                extrusion_temps.append(leaving_temp)
            current_tool = purge.tool
            continue
        if head in _MOVE_HEADS:
            # tokenise the parameters so a compact ``G1 X1E6`` (an E welded inside a token) reads
            # as an extrusion just like the spaced ``G1 X1 E6``; a parameter string that will not
            # tokenise could hide an E, so it is refused rather than trusted to be extrusion-free
            params = _move_params(rest)
            if params is None:
                raise _refuse(f"the move {line[:40]!r} carries a parameter this parser cannot "
                              "read, so it cannot rule out an extrusion hidden in it — refusing "
                              "rather than assuming a move it cannot tokenise does not extrude")
            e_values = [v for letter, v in params if letter == "E"]
            if e_values:
                where = f"the E move {line[:40]!r}"
                if unload_seen:
                    raise _after_g180_unload(where)
                if nozzle_target is None:
                    raise _refuse(f"{where} extrudes while no nozzle temperature has even been "
                                  "commanded — refusing rather than guessing what the printer "
                                  "would run it at")
                if nozzle_target <= 0.0:
                    raise _refuse(f"{where} extrudes at a nozzle target of {nozzle_target:g} °C — "
                                  "that is the heater off, and an extrusion there would be a cold one")
                if waited != nozzle_target:
                    if (pending_tool_temp is not None and waited is not None
                            and all(v < 0.0 for v in e_values)):
                        # design 3a: the wipe-tower retract behind a pending tool-change setpoint.
                        # The hot end still holds the verified wait (the M104 only moved the
                        # target toward the incoming tool's declared temperature, which the
                        # purge's own M109 waits at), and a negative-E move carries no material
                        # forward onto it. The retraction passes, and joins neither temperature
                        # story: the pending target is unverified until the purge retires it, and
                        # the old wait was recorded by the extrusion or purge that proved it.
                        last_extrusion_at = idx
                    else:
                        pending_says = ("" if pending_tool_temp is None else
                                        f"; {pending_tool_temp:g} °C is a tool-change setpoint the "
                                        "wipe tower commanded before its purge, and only "
                                        "retractions (negative-E moves) may run before the M6211 "
                                        "whose purge waits at it")
                        raise _refuse(f"{where} extrudes at a commanded nozzle target of "
                                      f"{nozzle_target:g} °C that no M109 was ever waited at (the "
                                      "most recent verified wait was "
                                      + _wait_lost()
                                      + ") — every extrusion must follow a verified M109 wait at "
                                      "the temperature it runs at, and a target changed after the "
                                      "wait needs another M109 at the new value" + pending_says)
                else:
                    if nozzle_target not in extrusion_temps:
                        extrusion_temps.append(nozzle_target)
                    if current_tool is None:
                        raise _refuse(f"{where} extrudes before the file has selected any tool or "
                                      "flushed one in with a purge, so this parse cannot say which "
                                      "spool runs it — refusing rather than recording an extrusion "
                                      "no tool's story can answer for")
                    if nozzle_target not in per_tool_temps.setdefault(current_tool, []):
                        per_tool_temps[current_tool].append(nozzle_target)
                    last_extrusion_at = idx
            # and a move with no E still has to be a command the list carries: the G38.x probe move
            # and the padded spellings are read here for their E parameter, but they are not commands
            # this workflow accepts, so a travel on one of them refuses rather than riding on "it was
            # only a move"
            if denied := _command_policy(head, line, rest):
                raise denied
            continue
        m = _TOOL_RE.match(line.upper())  # Klipper matches command words case-insensitively
        if m:
            tool = int(m.group(1))
            if current_tool is not None and tool != current_tool:
                # A bare T<n> that names a different tool than the one in force is a filament
                # switch this parser has never read on this firmware: the vendor's own slicer
                # only ever changes tool through M6211 (whose T switches the filament, then the
                # T<n> line restates it). If a bare T<n> switches filament it is an unread
                # filament command; if it does nothing, the old spool keeps extruding at a
                # temperature only the new tool's tray is checked against. Either way refused
                # (review finding, PR #11): a tool change arrives only through M6211 T<n>.
                raise _refuse(f"the tool select {line[:20]!r} changes the tool from tool "
                              f"{current_tool} (T{current_tool}) to tool {tool} (T{tool}) with no "
                              "M6211 purge switching it first — a bare tool "
                              "change is a filament switch this parser has not read on this "
                              "firmware, and the vendor's own slicer only changes tool through "
                              "its M6211 purge, so one without it is refused",
                              hint="Re-slice with the stock Centauri Carbon 2 change_filament_gcode, "
                                   "whose M6211 T<n> precedes every T<n>.")
            if tool not in tools:
                tools.append(tool)
            current_tool = tool
            continue
        # Nothing above consumed this line, so this is a command the parser does not read — and
        # unread is not inert. The allow list decides: an entry goes if it is justified, and
        # anything else refuses the whole file before a byte is sent.
        if denied := _command_policy(head, line, rest):
            raise denied
    if cooldown_at and last_extrusion_at is not None and min(cooldown_at) < last_extrusion_at:
        raise _refuse("it turns the nozzle off (S0) before the file's last extrusion — a "
                      "cooldown that early contradicts the extrusions still to come (a "
                      "non-extruding preheat below the printing temperature is fine; switching "
                      "the heater off mid-file is not)")
    if not tools:
        raise _refuse("it selects no tool at all (no T<n> line), so its tools cannot be "
                      "identified — a file whose only tool mention sits inside an M6211 says "
                      "nothing dependable about its spools")
    if any(tool not in tools for tool in purge_tools):
        # a flush that switches in a tool the file never selected switches a spool the file
        # itself never names, whatever else about it is multi-tool
        raise _purge_tool_refusal(next(t for t in purge_tools if t not in tools), tools)

    # per-tool config, read here because "one value per tool" is decided by the tool selects the
    # scan above collected; a single tool's single value works whatever that tool's number is
    materials = _config_per_tool(config, "filament_type", "filament type", tools)
    for value in materials:
        if (len(value) > 64 or not value.isascii()
                or not value.isprintable() or any(c.isspace() for c in value)):
            raise _refuse(f"its filament type {value[:20]!r} is not a plain printable word")
    nozzle_firsts = _config_temp_per_tool(config, "nozzle_temperature_initial_layer",
                                          "initial-layer nozzle temperature", tools,
                                          maximum=_NOZZLE_MAX)
    nozzles = _config_temp_per_tool(config, "nozzle_temperature", "steady nozzle temperature",
                                    tools, maximum=_NOZZLE_MAX)

    if flush_r:
        # design 3, decided now that the tool list above is complete: an off-target flush R may
        # stand only as the vendor's own change_filament_gcode writes it — equal to the declared
        # nozzle_temperature_range_high of BOTH the tool being left and the tool switched to, the
        # file's own numbers read the same per-tool way as every other per-filament key (missing,
        # short, contradictory or unphysical lists refuse naming the key here). The purge's
        # firmware clamp already holds R inside the sanity fence; the declared values, which R
        # must equal, are fence-checked by the same read.
        try:
            ranges = _config_temp_per_tool(config, "nozzle_temperature_range_high",
                                           "declared nozzle temperature range high", tools,
                                           maximum=_NOZZLE_MAX)
        except PrinterError as bad:
            quote, value, target, _leaving, _dest = flush_r[0]
            raise _flush_range_refusal(quote, value, target, bad) from None
        for quote, value, target, leaving, dest in flush_r:
            if leaving is None:
                raise _refuse(
                    f"the purge command {quote!r} sets R to {value:g} °C, which is not the "
                    "verified wait in force there, and at that point the file had selected no "
                    "tool and flushed in none, so there is no tool being left whose declared "
                    "nozzle_temperature_range_high could authorise the flush — refusing rather "
                    "than flushing hotter than any spool in this file is read for")
            high_left, high_dest = ranges[leaving], ranges[dest]
            if value != high_left or value != high_dest:
                raise _refuse(
                    f"the purge command {quote!r} sets R to {value:g} °C while the nozzle target "
                    f"verified by an M109 wait at this point is {target:g} °C; R may differ from "
                    "it only the way the vendor's own change_filament_gcode "
                    "writes it — equal to the declared nozzle_temperature_range_high of BOTH the "
                    f"tool being left (T{leaving}: {high_left:g} °C) and the tool switched to "
                    f"(T{dest}: {high_dest:g} °C) — and the flush really extrudes at R, so any "
                    "other R is an extrusion at a target no wait ever proved and no mapped "
                    "spool is read for")
            # accepted: the flush pushed both spools' filament at R, so R is recorded as a flush
            # temperature of each tool (design amendment 9) — not as a printing temperature,
            # because no M109 ever waited there, and not in the file-wide extrusion story either
            for tool in (leaving, dest):
                if value not in per_tool_flush.setdefault(tool, []):
                    per_tool_flush[tool].append(value)

    # design 5's report, read from the same config dict and never a refusal: how the file's
    # template config keys compare to the stock OrcaSlicer 2.4.2 CC2 templates (a file that
    # carries none of the keys compares as "stock" with zero keys actually compared — nothing
    # guessed at, nothing proven)
    vendor_templates, templates_compared = cc2_stock_templates.compare_verdict(
        {key: raws for key, raws in config.items()
         if key in cc2_stock_templates.STOCK})

    for index in tools:  # every selected tool's promises must appear among the commands
        for value, label in ((nozzle_firsts[index], "initial-layer nozzle temperature"),
                             (nozzles[index], "steady nozzle temperature")):
            if value not in nozzle_cmds:
                raise _refuse(f"its config promises a {label} of {value:g} °C but no M104/M109 "
                              "command ever sets it — the file's config and its commands disagree")
    for value, label in ((bed_first, "initial-layer bed temperature"), (bed, "steady bed "
                                                                             "temperature")):
        if value not in bed_cmds:
            raise _refuse(f"its config promises a {label} of {value:g} °C but no M140/M190 "
                          "command ever sets it — the file's config and its commands disagree")
    first_tool = tools[0]
    # every selected tool carries its own verified-extrusion story — the temperatures its own E
    # moves and its purges ran behind a verified wait — and, apart from it, its flush story: any
    # accepted flush R (recorded for both tools that flush touched, because the flush extruded
    # on both spools). The flat field stays the file-wide union of the verified-extrusion stories
    # in first order seen, so a reader that knows only it still sees every printing temperature
    tool_settings = tuple(
        ToolSettings(tool_index=index, material=materials[index],
                     nozzle_first_layer_temp=nozzle_firsts[index], nozzle_temp=nozzles[index],
                     extrusion_nozzle_temps=tuple(per_tool_temps.get(index, [])),
                     flush_nozzle_temps=tuple(per_tool_flush.get(index, [])))
        for index in tools)
    return Cc2GcodeSettings(material=materials[first_tool],
                            nozzle_first_layer_temp=nozzle_firsts[first_tool],
                            nozzle_temp=nozzles[first_tool],
                            bed_first_layer_temp=bed_first, bed_temp=bed, tool_index=first_tool,
                            nozzle_setpoints=tuple(nozzle_cmds), bed_setpoints=tuple(bed_cmds),
                            extrusion_nozzle_temps=tuple(extrusion_temps),
                            bed_plate=plate_word,
                            tools=tool_settings,
                            printer_model=printer_model,
                            vendor_templates=vendor_templates,
                            templates_compared=templates_compared)


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


class _ParseStream:
    """The file as decoded text lines for the settings parse, and — the reason this is a class and
    not a bare generator — the SHA-256 of the exact raw bytes it handed the parser.

    The upload's honesty rests on one claim: the settings on the receipt are the settings of the
    bytes that were hashed and the bytes that were sent. When those three reads are separate, a
    same-size rewrite between them (content mutated to PLA/240 after the hash, reverted to
    PETG/260 before the send) lets the parser read one version while the hash and the wire carry
    another — a receipt that records PLA/240 for bytes that leave as PETG/260. Closing that hole
    means hashing the exact bytes the parser consumed and refusing unless that hash equals the
    hash taken before parsing (and, downstream, the hash of the bytes actually sent): all three
    then name one version or nothing is believed.

    It reads in the very blocks ``_hash_source`` reads, so the digest here is computed over the
    same version ``_hash_source`` saw for a file that holds still, and lines are split on ``\n``
    exactly as iterating the binary file did (the parser strips each line, so ``\r\n`` and a
    missing trailing newline behave the same). Decoding uses replacement so a broken byte fails
    the parse, not the scan; a corrupt file leaves no command for a parser to survive. The stream
    also refuses a file whose total length drifts from the promised ``size``, as before."""

    def __init__(self, f: Any, size: int) -> None:
        self._f = f
        self._size = size
        self.sha256_hex: str | None = None

    def __iter__(self):
        self._f.seek(0)
        sha = hashlib.sha256()
        read = 0
        leftover = b""
        while read < self._size:
            block = self._f.read(min(_HASH_BLOCK, self._size - read))
            if not block:
                raise Cc2UploadError(
                    "protocol_error",
                    f"The G-code file ended after {read} of {self._size} bytes while it was being "
                    "parsed — it is changing while this process reads it.", reason="source_changed")
            sha.update(block)  # hash the exact bytes the parser is fed, in the order fed
            read += len(block)
            buffer = leftover + block
            *lines, leftover = buffer.split(b"\n")
            for piece in lines:
                yield piece.decode("utf-8", "replace")
        if self._f.read(1):
            raise Cc2UploadError(
                "protocol_error",
                f"The G-code file is longer than the {self._size} bytes counted before parsing it "
                "— it grew while being read, so no single version of it was parsed.",
                reason="source_changed")
        if leftover:
            yield leftover.decode("utf-8", "replace")
        self.sha256_hex = sha.hexdigest()


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
        parse_stream = _ParseStream(f, st.st_size)
        settings = parse_gcode_settings(parse_stream)
        # Binding: the settings on the receipt must belong to the bytes that were hashed and the
        # bytes that will be sent. The parse re-reads the file, so hash the exact bytes it consumed
        # and refuse unless they are the bytes hashed before parsing — a same-size rewrite that
        # lands after the hash and reverts before the send would otherwise let the parser record
        # one version's settings for another version's bytes. This is the same SHA-256 the send
        # pass is later checked against, so hash / parse / send all name one version or nothing is
        # believed; nothing is locked, listed, or sent until the three agree.
        if parse_stream.sha256_hex is None or parse_stream.sha256_hex != sha256_hex:
            raise Cc2UploadError(
                "protocol_error",
                "The G-code file changed content between being hashed and being parsed; the "
                "settings read are not provably the settings of the bytes that would be sent, so "
                "upload refused against a file that will not hold still. Nothing was sent to the "
                "printer.", reason="source_changed")
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
                    # every tool the file selected carries its own entry — its material, its own
                    # setpoints, the temperatures its extrusion ran at, and apart from them any
                    # accepted flush R both spools it touched were pushed through (design
                    # amendment 9); the flat fields above stay the first tool's story beside them
                    tools=tuple(
                        ToolReceipt(tool_index=tool.tool_index, material=tool.material,
                                    nozzle_first_layer_temp=tool.nozzle_first_layer_temp,
                                    nozzle_temp=tool.nozzle_temp,
                                    extrusion_nozzle_temps=tool.extrusion_nozzle_temps,
                                    flush_nozzle_temps=tool.flush_nozzle_temps)
                        for tool in settings.tools),
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
