"""One guarded start of one already-received file on the one printer this adapter may reach: every
check first, then at most one method 1020, and never a second command because an answer went
missing. The wire shape is the one measured on this printer and sent by the Home Assistant
integration that printed real jobs on it (v2.16.0-beta.2, firmware 02.01.00.00): params carry
``storage_media`` (always ``"local"`` — the only spelling any request has been answered for), the
exact ``filename``, and a ``config`` of exactly ``slot_map`` and ``printer_check``. ``printer_check``
is the auto-bed-leveling flag, and it is sent explicitly every time, default ``False``, because the
firmware remembers the last value it was given — measured on this machine: after a job that sent
``true``, a following job that omitted the key levelled again. Only an explicit ``false`` turns
levelling off; ``true`` goes out only when a bed-leveling request for this print is explicit.
``slot_map`` carries one entry per G-code tool and every entry must carry ``t``, ``canvas_id`` and
``tray_id`` together — measured: a partial entry or an out-of-range tray is acknowledged with
``error_code`` 0 and the printer silently prints from tray 0, so the reply can never validate a
mapping and this module validates it before sending instead.

What may stand behind that one command, all of it re-read inside the same per-printer lock the
upload holds (the receipt store's lock — one operation at a time per printer, refused rather than
queued): a receipt this very store wrote for this printer and this exact name, with the printer's
listing today reporting that name at exactly the receipted size; an explicit ``bed_clear``
assertion (this call starts a machine into motion above a bed nobody has looked at; ``True`` is
the human looking); a fresh 1002/2005 read saying idle with no error and no file held; the chosen
tray present (raw 1) or active (raw 2) on a connected canvas, reporting the same material the
receipt parsed; the tray's rated nozzle range covering every temperature extrusion was verified
to run at — the receipt's ``extrusion_nozzle_temps``, which deliberately excludes non-extruding
preheats like the mount file's real 140 °C flush hold before its ``M109 S260`` — and no commanded
nozzle setpoint, preheat included, above the tray's rated maximum, because a hold above what the
spool is rated for is heat the spool cannot vouch for. Unknown, absent, half-a-range or
contradictory reports refuse the start; nothing is ever assumed. The mapping sent is built from
the receipt's recorded tool and the tray identity of the fresh 2005 read — the entry's
``canvas_id``/``tray_id`` are the ones that just answered, not numbers the caller retyped.

Honesty about the reply: ``error_code`` 0 means the printer did not refuse the request — it is not
proof the tray was honoured (a silently-downgraded mapping answers 0 too) and not proof the remote
bytes are still the received bytes (CC2 cannot read file content back, and other clients are
ungoverned by this lock; a same-size replacement is undetectable from here). A missing, unreadable
or nonzero code is a refusal or an ambiguity, never a success: 1009 is the printer saying busy; a
lost reply, a dropped session or an unparseable code is reported ambiguous, the command is NOT
resent, and the only follow-up allowed is the one read-only 1002 status the caller did not veto —
whose "printing" means printing, and whose "idle" proves nothing about the vanished command.
Running is claimed only on the observed lowercase wire pair: ``print_status.state`` exactly
``"printing"`` naming exactly this file with machine status 2. Acceptance and observed running are
separate facts in the result and never collapsed into one another.

Bed warnings: the design says a slice's bed warning is checked separately and surfaced, and this
module passes it through rather than inventing a check — the receipt deliberately stores no local
path, plate or warning, and the mount slice actually printed on this printer carries no
bed-warning marker this adapter has ever observed, so no warning format is verified well enough
to parse at start; the caller that read the slice hands its finding over and it comes back on the
result, stated without ever blocking or blessing anything. A valid slice is not proof a warning
was resolved. Privacy: nothing here takes an address, a token or an environment variable —
transport is the pinned session and receipts the explicit store — and no message, note or result
carries credentials or raw replies. This module sends exactly one method and reads four."""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

from . import cc2_state as st
from .cc2_receipts import Cc2ReceiptStore, Cc2UploadReceipt, _plain_name
from .cc2_transport import Cc2TimeoutError, PrinterError

METHOD_START = 1020                 # START_PRINT — the one method this module ever sends
STORAGE_MEDIA = "local"             # the only storage_media ever sent and answered
_WARN_MAX = 4096

_BUSY_HINT = ("The printer says it is occupied (error_code 1009). Nothing was retried — read its "
              "status when it reports idle again and start it deliberately.")


class Cc2StartError(PrinterError):
    """A start that was refused: the same codes every printer tool speaks, with a ``reason``
    naming the check that stopped it (``bed_not_clear``, ``tool_map``, ``tool_map_mismatch``,
    ``remote_name``, ``receipt_missing``, ``receipt_invalid``, ``locked``, ``state_unreadable``,
    ``inventory_incomplete``, ``file_missing``, ``size_mismatch``, ``busy``, ``tray_missing``,
    ``tray_unavailable``, ``material_unverified``, ``material_mismatch``, ``range_unverified``,
    ``range_violation``, ``commanded_over_tray_max``) so a caller can act without parsing
    messages. A refusal here always names it before the printer was ever asked; nothing that
    raised this was sent, and nothing that failed is retried."""


class StartStatus(enum.StrEnum):
    """What the printer's answer to the one start command supports: ACCEPTED — the explicit
    ``error_code`` 0 the protocol documents as started; REFUSED — the printer declined on record
    with its code kept; AMBIGUOUS — no readable answer, which is neither failure nor success."""
    ACCEPTED = "accepted"
    REFUSED = "refused"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class Cc2StartObservation:
    """One read-only look (a single 1002) at what the printer says it is doing. ``running`` is
    the design's bar and nothing softer: the observed lowercase wire pair — machine status 2 with
    ``print_status.state`` exactly ``"printing"`` — naming exactly the requested file.
    ``observed_state`` is that wire word, exactly as observed and lowercase (``"printing"`` on
    the frame this printer sends live; ``"idle"`` for its idle word, the empty string read as the
    idle pair it belongs to); None when no observation landed or the words said something this
    adapter cannot name — which stays an uncertainty, never a guess of either outcome."""
    running: bool
    observed_state: str | None = None
    observed_job: str | None = None
    machine_status: int | None = None

    @classmethod
    def from_machine(cls, machine: st.Cc2MachineStatus, remote_filename: str) -> "Cc2StartObservation":
        running = (machine.machine_status == 2
                   and machine.print_state is st.PrintState.PRINTING
                   and machine.job_filename == remote_filename)
        if machine.print_state is st.PrintState.PRINTING:
            state: str | None = "printing"
        elif machine.print_state is st.PrintState.UNKNOWN:
            state = None                      # an unnameable word proves nothing about anything
        elif machine.print_state is st.PrintState.IDLE and machine.machine_status == 1:
            state = "idle"                    # the wire's "" belongs to this observed idle pair
        else:
            state = machine.raw_print_state   # documented words read through exactly
        return cls(running=running, observed_state=state, observed_job=machine.job_filename,
                   machine_status=machine.machine_status)


@dataclass(frozen=True)
class Cc2StartResult:
    """The whole outcome of one guarded start. ``status`` says what the printer answered and
    ``error_code`` keeps its number; ``accepted`` (error_code 0) and ``running`` (an observation
    that says printing, naming this file) are separate facts, never each other. ``receipt_sha256``
    ties the run to the private receipt that made it possible. ``notes`` carry what the caller
    must say out loud: the honesty notes on an ambiguous reply, and always the accepted-path
    limits — a 0 answer is 'the printer did not refuse', never proof the mapping was honoured or
    that no other client replaced the file. ``as_dict()`` is the shape a tool reports."""
    status: StartStatus
    remote_filename: str
    accepted: bool = False
    running: bool = False
    error_code: int | None = None
    observed: Cc2StartObservation | None = None
    receipt_sha256: str = ""
    notes: tuple[str, ...] = ()
    bed_warning: str | None = None

    @property
    def observed_state(self) -> str | None:
        return self.observed.observed_state if self.observed else None

    @property
    def observed_job(self) -> str | None:
        return self.observed.observed_job if self.observed else None

    def as_dict(self) -> dict:
        return {
            "started_accepted": self.accepted,
            "observed_running": self.running,
            "status": str(self.status),
            "filename": self.remote_filename,
            "error_code": self.error_code,
            "observed_state": self.observed_state,
            "observed_job": self.observed_job,
            "receipt_sha256": self.receipt_sha256,
            "notes": list(self.notes),
            "bed_warning": self.bed_warning,
        }


# The accepted-path honesty notes: every acceptance carries them, because they are properties of
# what error_code 0 can and cannot mean on this firmware, measured in the integration's own words
# ("the response cannot be used to validate the mapping") and in the receipt module's provenance
# limit ("a receipt and remote size cannot prove remote bytes were not replaced").
_MAP_NOTE = ("An accepted start is the printer not refusing the request — measured, a partial or "
             "out-of-range slot_map is acknowledged with error_code 0 too and prints silently from "
             "tray 0. Every slot_map entry was sent complete (t, canvas_id, tray_id together) with "
             "the tray identity taken from the fresh 2005 read, so the mapping is only as good as "
             "that read; nothing about the reply validates the tray it actually feeds.")
_PROVENANCE_NOTE = ("The receipt and a matching listing size prove this adapter moved these bytes; "
                    "CC2 cannot read file content back, and other clients are not governed by this "
                    "lock, so a same-size replacement after the listing cannot be detected from here.")
_RUNNING_NOTE = ("Observed printing: the printer's status names this file with the lowercase "
                 "state word \"printing\" under machine status 2.")
_BED_WARNING_NOTE = ("A bed warning was carried from the slice — a valid slice is not proof the "
                     "warning is resolved. The receipt stores no local file, so this module can "
                     "pass the warning along but never re-derive or resolve it.")
_NOT_PRINTING_NOTE = ("The read-only status did not say printing. That does not mean the start "
                      "failed: the command may have landed and the job may begin, or never have "
                      "been accepted. The printer's screen decides what to do next.")
_NO_STATUS_NOTE = ("No read-only status could be read after the unanswered start command, so the "
                   "state is unknown — nothing was resent, and only the printer itself knows "
                   "whether the command landed.")
_NO_OBSERVE_NOTE = ("No read-only status was requested after the unanswered start command; the "
                    "state is unknown and nothing was resent.")
_UNPARSEABLE_NOTE = ("The printer's answer to the start command carried no readable error code, "
                     "which is neither success nor refusal — nothing was resent.")


def _plain_int(value: Any) -> bool:
    """A whole number as a mapping index reads: an int, and never a bool, which is an int in
    Python and not a tool or tray number on any wire."""
    return isinstance(value, int) and not isinstance(value, bool)


def _int_or_refuse(value: Any, label: str) -> int:
    if not _plain_int(value) or value < 0:
        raise Cc2StartError("protocol_error",
                            f"The tool mapping carries {label} as {value!r}, which is not a "
                            "whole-number {label} — the one start command is never sent against "
                            "a mapping that could silently fall back to tray 0.",
                            reason="tool_map")
    return int(value)


def _load_receipt_or_refuse(store: Cc2ReceiptStore, printer_sn: str, remote_filename: str
                            ) -> Cc2UploadReceipt:
    try:
        receipt = store.load(printer_sn, remote_filename)
    except PrinterError as e:
        raise Cc2StartError("protocol_error",
                            f"The stored upload receipt for '{remote_filename}' cannot be "
                            f"trusted: {e.message} Nothing was sent.",
                            hint=e.hint, reason="receipt_invalid") from e
    if receipt is None:
        raise Cc2StartError("protocol_error",
                            f"There is no upload receipt on file for '{remote_filename}', so "
                            "this workflow cannot vouch for the file's bytes — start refused. "
                            "Upload it through this workflow first.",
                            reason="receipt_missing")
    return receipt


async def _observe_once(transport: Any, remote_filename: str
                        ) -> tuple[Cc2StartObservation | None, str | None]:
    """One read-only 1002, sent exactly once, or the honest note saying it could not be read.
    This is the only follow-up an ambiguous start may ever cause, and it asks for nothing but
    status — the start command itself is never reconsidered here."""
    try:
        machine = await st.fetch_machine_status(transport)
    except PrinterError as e:
        why = getattr(e, "message", None) or str(e)
        return None, f"{_NO_STATUS_NOTE} (the read-only status read failed: {why})"
    return Cc2StartObservation.from_machine(machine, remote_filename), None


def _build_result(status: StartStatus, remote_filename: str, receipt: Cc2UploadReceipt, *,
                  bed_warning: str | None, error_code: int | None = None,
                  note: str | None = None, observation: Cc2StartObservation | None = None,
                  extra_notes: tuple[str, ...] = ()) -> Cc2StartResult:
    notes: list[str] = []
    if status is StartStatus.ACCEPTED:
        notes.extend((_MAP_NOTE, _PROVENANCE_NOTE))
    elif note:
        notes.append(note)
    if observation is not None and observation.running:
        notes.append(_RUNNING_NOTE)
    notes.extend(extra_notes)
    if bed_warning:
        notes.append(_BED_WARNING_NOTE)
    return Cc2StartResult(
        status=status, remote_filename=remote_filename,
        accepted=status is StartStatus.ACCEPTED,
        running=observation is not None and observation.running,
        error_code=error_code, observed=observation,
        receipt_sha256=receipt.content_sha256,
        notes=tuple(notes), bed_warning=bed_warning)


async def start_print(transport: Any, remote_filename: str, tool_to_tray: dict[int, int], *,
                      bed_clear: bool,
                      store: Cc2ReceiptStore | None = None,
                      printer_check: bool = False,
                      read_only_status: bool = True,
                      observe_running: bool = False,
                      bed_warning: str | None = None,
                      timeout_s: float | None = None) -> Cc2StartResult:
    """Start one received file printing, once, after every check passes — or refuse, naming the
    check, without the printer ever being asked.

    In order, with every failure named before the next step runs: validate the caller's
    assertions (the exact ``bed_clear is True`` that says a human looked at the bed, an explicit
    single-tool mapping, a plain remote name, a plain bool for leveling sent as ``printer_check``
    because the firmware remembers the last value); load the private receipt this store wrote for
    this printer and name, and prove its recorded tool is the tool mapped; take the same
    per-printer lock the upload takes — held means somebody else is mid-operation and this call
    refuses rather than queueing; then, inside the lock, read afresh what the printer claims:
    status and canvas (1002/2005) and a complete file inventory (1044). The inventory must name
    this exact file at the receipted size or the start dies; the machine must be idle with no
    error and no file held; the mapped tray must exist on the connected canvas and be present or
    active; the tray's material must equal the receipt's; the tray must carry a complete rated
    range covering every verified extrusion temperature (non-extruding preheats are not extrusion
    targets and are not range-checked) with no commanded nozzle setpoint above the tray maximum.
    Only then is the single 1020 sent — a complete ``slot_map`` entry built from the receipt's
    tool and the fresh tray identity, ``printer_check`` explicit, and nothing else in ``config``
    because nothing else has been verified to mean anything.

    The answer decides the report, never the hope: explicit integer ``error_code`` 0 is accepted
    (with the honesty notes), any other number is a refusal with its code kept, and a lost,
    dropped or unparseable answer is ambiguous — never retried, optionally followed by exactly
    one read-only 1002 (``read_only_status``; ``observe_running`` adds the same single look even
    after an acceptance). Running is claimed only on the observed lowercase "printing" naming
    this file under machine status 2. No step is ever attempted twice, and every refusal happens
    before the first write method leaves the wire."""
    # ---- the caller's own assertions, checked before anything is read or locked ---------------
    if bed_clear is not True:
        raise Cc2StartError(
            "protocol_error",
            "Starting prints demands an explicit physical bed-clear assertion: the caller must "
            "pass bed_clear=True after looking at the bed. "
            + ("Nothing was sent." if bed_clear is False else
               f"bed_clear={bed_clear!r} is not that assertion; only the bool True is. "
               "Nothing was sent."),
            reason="bed_not_clear")
    if not isinstance(printer_check, bool):
        raise Cc2StartError(
            "protocol_error",
            f"printer_check must be the plain bool True or False — it is sent explicitly either "
            f"way because the firmware remembers the last value it was given — not "
            f"{printer_check!r}. Nothing was sent.",
            hint="Leave it False (no bed leveling) unless leveling for this print was explicitly "
                 "requested, in which case pass True.",
            reason="printer_check")
    name = _plain_name(remote_filename)        # the exact name, or a refusal that says why
    if not isinstance(tool_to_tray, dict) or not tool_to_tray:
        raise Cc2StartError("protocol_error",
                            "A start needs an explicit tool-to-tray mapping like {0: 3} — the "
                            "one tool this adapter can speak, mapped to the tray the caller "
                            "chose. Nothing was sent.",
                            reason="tool_map")
    if len(tool_to_tray) != 1:
        raise Cc2StartError("protocol_error",
                            f"The mapping names {len(tool_to_tray)} tools; this adapter speaks "
                            "exactly one until multi-tool starts are independently supported. "
                            "Nothing was sent.",
                            reason="tool_map")
    (mapped_tool, mapped_tray), = tool_to_tray.items()
    _int_or_refuse(mapped_tool, "tool index")
    _int_or_refuse(mapped_tray, "tray id")
    if bed_warning is not None:
        ok_warning = False
        if isinstance(bed_warning, str):
            bed_warning = bed_warning.strip() or None
            ok_warning = bed_warning is None or (
                len(bed_warning) <= _WARN_MAX
                and not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in bed_warning))
        if not ok_warning:
            raise Cc2StartError("protocol_error",
                                "A carried bed warning must be one short plain line of words, "
                                f"not {type(bed_warning).__name__} of it. Nothing was sent.",
                                reason="bed_warning")

    store = store if store is not None else Cc2ReceiptStore()
    printer_sn = str(transport.sn)             # receipts and lock bind to the serial discovery proved

    # ---- the private proof, before the lock: no receipt, no start ------------------------------
    receipt = _load_receipt_or_refuse(store, printer_sn, name)
    if receipt.tool_index != mapped_tool:
        raise Cc2StartError("protocol_error",
                            f"The mapping maps tool {mapped_tool}, but the receipt for "
                            f"'{name}' records tool {receipt.tool_index} — the mapping must "
                            "match the tool that was received, not another one's. Nothing was "
                            "sent.",
                            reason="tool_map_mismatch")

    # ---- the shared per-printer lock; held means refused, never queued -------------------------
    try:
        lock = store.lock(printer_sn)   # the store's own context manager, entered and released
        lock.__enter__()               # by hand: acquiring must be distinguishable from failing
    except PrinterError as e:
        reason = "locked" if e.details.get("reason") == "locked" else "state_unreadable"
        raise Cc2StartError(e.code,
                            f"The start of '{name}' refused without touching the printer: "
                            f"{e.message}", hint=e.hint, reason=reason) from e
    try:
        # ---- fresh reads inside the lock: what the printer claims right now ---------------------
        try:
            status = await st.fetch_status(transport, timeout_s=timeout_s)
        except Cc2TimeoutError as e:
            raise Cc2StartError("protocol_error",
                                f"The pre-start status read timed out, so the printer's state is "
                                f"unknown and the one start command is not being risked: "
                                f"{e.message} Nothing was sent.",
                                reason="state_unreadable") from e
        except PrinterError as e:
            raise Cc2StartError("protocol_error",
                                f"The pre-start status read refused, so nothing can be claimed "
                                f"about the bed: {e.message} Nothing was sent.",
                                reason="state_unreadable") from e
        machine, canvas = status.machine, status.canvas
        ok, why = machine.ready_for_job()
        if not ok:
            raise Cc2StartError("protocol_error",
                                f"'{name}' stays queued: the printer is not startable — {why}.",
                                reason="busy")
        if not canvas.connected:
            raise Cc2StartError("protocol_error",
                                f"'{name}' stays queued: the printer reports its canvas not "
                                "connected, so no tray can be verified for a job.",
                                reason="tray_unavailable")

        try:
            inventory = await st.list_files(transport, timeout_s=timeout_s)
        except st.Cc2IncompleteDataError as e:
            raise Cc2StartError("protocol_error",
                                f"The start of '{name}' refused: the printer's file listing "
                                f"could not be proven complete, so a file on it cannot be "
                                f"verified against its receipt: {e.message} Nothing was sent.",
                                reason="inventory_incomplete") from e
        except Cc2TimeoutError as e:
            raise Cc2StartError("protocol_error",
                                f"The pre-start file listing timed out, so the file's presence "
                                f"and size are unknown and the start command is not being "
                                f"risked: {e.message} Nothing was sent.",
                                reason="state_unreadable") from e
        except PrinterError as e:
            raise Cc2StartError("protocol_error",
                                f"The pre-start file listing refused: {e.message} Nothing was "
                                "sent.",
                                reason="state_unreadable") from e
        entry = inventory.find_file(name)
        if entry is None:
            raise Cc2StartError("protocol_error",
                                f"The printer's complete listing does not carry '{name}' — the "
                                "received file is not where the receipt says it is, so it is "
                                "not being started. Nothing was sent.",
                                hint="Upload it through this workflow again, deliberately.",
                                reason="file_missing")
        if entry.size != receipt.reported_size_bytes:
            raise Cc2StartError("protocol_error",
                                f"The printer lists '{name}' at {entry.size} bytes but the "
                                f"receipt records {receipt.reported_size_bytes} — the file on "
                                "the printer is not the file that was received, so it is not "
                                "being started. Nothing was sent.",
                                reason="size_mismatch")

        # ---- the chosen tray, as the fresh 2005 read reports it ---------------------------------
        same_id = [t for t in canvas.trays if t.tray_id == mapped_tray]
        if not same_id:
            raise Cc2StartError("protocol_error",
                                f"The printer reports no tray {mapped_tray} on its canvas, so "
                                "the mapping points at nothing. Nothing was sent.",
                                reason="tray_missing")
        if len(same_id) > 1:
            raise Cc2StartError("protocol_error",
                                f"{len(same_id)} canvas units report a tray {mapped_tray}, and "
                                "this adapter's canvas handling is verified for one unit only — "
                                "a tray id that means two trays means no start. Nothing was "
                                "sent.",
                                reason="tray_missing")
        chosen = same_id[0]
        blocked = st.supply_decision(chosen, canvas=canvas, canvas_connected=canvas.connected)
        if blocked:
            raise Cc2StartError("protocol_error",
                                f"'{name}' stays queued: {blocked}. Nothing was sent.",
                                reason="tray_unavailable")
        if chosen.material is None:
            raise Cc2StartError("protocol_error",
                                f"Tray {mapped_tray} reports no material, so the receipt's "
                                f"{receipt.material!r} cannot be matched against what is "
                                "actually loaded — an unmatched filament is the print the "
                                "nozzle eats. Nothing was sent.",
                                reason="material_unverified")
        if chosen.material.casefold() != receipt.material.casefold():
            raise Cc2StartError("protocol_error",
                                f"Tray {mapped_tray} reports {chosen.material}, but the "
                                f"received file is {receipt.material} — the mapping and the "
                                "material must be the same decision, and the spool is the "
                                "printer's word, not the caller's. Nothing was sent.",
                                reason="material_mismatch")
        if chosen.min_nozzle_temp is None or chosen.max_nozzle_temp is None:
            raise Cc2StartError("protocol_error",
                                f"Tray {mapped_tray} carries no usable rated nozzle range (it "
                                f"reported {chosen.min_nozzle_temp!r}-{chosen.max_nozzle_temp!r}), "
                                "so nothing about this spool vouches for the heat the file runs "
                                "at. Nothing was sent.",
                                reason="range_unverified")
        lo, hi = float(chosen.min_nozzle_temp), float(chosen.max_nozzle_temp)
        for temp in receipt.extrusion_nozzle_temps:
            if not (lo <= temp <= hi):
                raise Cc2StartError("protocol_error",
                                    f"Extrusion was verified at {temp:g} °C, outside tray "
                                    f"{mapped_tray}'s rated range {lo:g}-{hi:g} °C "
                                    f"({chosen.material}) — printing this file from this spool "
                                    "is refused before the fact. Nothing was sent.",
                                    reason="range_violation")
        # The non-extruding preheats among the commanded setpoints (the mount file's real 140 °C
        # flush hold sits far below any PETG range) are NOT range-checked — they never extrude —
        # but none of them may command more heat than the loaded spool is rated to hold.
        for temp in receipt.commanded_nozzle_setpoints:
            if temp > hi:
                raise Cc2StartError("protocol_error",
                                    f"The file commands a nozzle target of {temp:g} °C, above "
                                    f"tray {mapped_tray}'s rated maximum {hi:g} °C "
                                    f"({chosen.material}) — a hold above what the spool is "
                                    "rated for is heat the spool cannot vouch for, preheat or "
                                    "not. Nothing was sent.",
                                    reason="commanded_over_tray_max")

        # ---- the one command: the measured shape, built from what just answered -----------------
        # Entry completeness is load-bearing: measured, a partial entry is acknowledged 0 and the
        # printer prints silently from tray 0. canvas_id/tray_id come from the tray that just
        # reported, never from the caller's re-typed numbers.
        slot_map = [{"t": receipt.tool_index, "canvas_id": chosen.canvas_id,
                     "tray_id": chosen.tray_id}]
        params = {"storage_media": STORAGE_MEDIA, "filename": name,
                  "config": {"slot_map": slot_map, "printer_check": printer_check}}

        try:
            result = await transport.request(METHOD_START, params, timeout_s=timeout_s)
        except Cc2TimeoutError as e:
            # The command went out and the answer was lost — the one ambiguity this protocol
            # hands you. Never a resend; at most the one read-only 1002 the caller did not veto.
            note = (f"The printer did not answer the one start command: {e.message} The command "
                    "was not resent.")
            if not read_only_status:
                return _build_result(StartStatus.AMBIGUOUS, name, receipt,
                                     bed_warning=bed_warning, note=note,
                                     extra_notes=(_NO_OBSERVE_NOTE,))
            observation, lost_note = await _observe_once(transport, name)
            extra = (lost_note,) if observation is None else (
                () if observation.running else (_NOT_PRINTING_NOTE,))
            return _build_result(StartStatus.AMBIGUOUS, name, receipt, bed_warning=bed_warning,
                                 note=note, observation=observation, extra_notes=extra)
        except PrinterError as e:
            # The session died (or was closed) — no answer, and by design the command is not
            # resent, not even once, not through a re-opened session: that decision is a human's.
            return _build_result(
                StartStatus.AMBIGUOUS, name, receipt, bed_warning=bed_warning,
                note=(f"The session ended without an answer to the start command ({e.message}) "
                      "— the job may have started or never been accepted, and nothing was "
                      "resent, not even a read-only status, because this session cannot carry "
                      "one."))

        # ``error_code``: only an explicit integer 0 is the documented started. An absent field,
        # a non-integer, a bool (an int in Python and not a protocol code), or any other number
        # is never success: "no acknowledgement" is an ambiguity, and a real code is the printer
        # declining on record.
        if not isinstance(result, dict) or "error_code" not in result:
            return _build_result(StartStatus.AMBIGUOUS, name, receipt, bed_warning=bed_warning,
                                 note=("The printer's answer to the start command carried no "
                                       f"error code at all. {_UNPARSEABLE_NOTE}"))
        code = result["error_code"]
        if not _plain_int(code):
            return _build_result(StartStatus.AMBIGUOUS, name, receipt, bed_warning=bed_warning,
                                 note=(f"The printer's answer to the start command carried an "
                                       f"error code that cannot be read ({type(code).__name__}), "
                                       "which is neither success nor refusal. "
                                       f"{_UNPARSEABLE_NOTE}"))
        if code != 0:
            note = (f"The printer refused the start command (error_code {code})."
                    + (" " + _BUSY_HINT if code == 1009 else ""))
            return _build_result(StartStatus.REFUSED, name, receipt, bed_warning=bed_warning,
                                 error_code=code, note=note)

        # error_code 0: the documented started — accepted, never running, until observed.
        if observe_running:
            observation, lost_note = await _observe_once(transport, name)
            extra = (lost_note,) if lost_note else (_NOT_PRINTING_NOTE,)
            if observation is None:
                return _build_result(StartStatus.ACCEPTED, name, receipt,
                                     bed_warning=bed_warning, extra_notes=(lost_note,))
            return _build_result(StartStatus.ACCEPTED, name, receipt, bed_warning=bed_warning,
                                 observation=observation,
                                 extra_notes=() if observation.running else extra)
        return _build_result(StartStatus.ACCEPTED, name, receipt, bed_warning=bed_warning)
    finally:
        lock.__exit__(None, None, None)
