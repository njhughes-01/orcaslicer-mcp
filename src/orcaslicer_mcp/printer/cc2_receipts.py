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

from .errors import PrinterError

# Version 2 added ``extrusion_nozzle_temps``: the nozzle temperatures extrusion was verified to
# run at (each proven by an M109 wait at that exact target, in command order), which a start
# compares against the loaded spool's range. Version 1 recorded only the indiscriminate
# commanded setpoints, which cannot tell a 140 °C non-extruding preheat from a cold extrusion;
# no version-1 receipt was ever written outside a test, and this build reads nothing else.
RECEIPT_VERSION = 2
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
    ``extrusion_nozzle_temps`` is the start's other half: exactly those setpoints extrusion was
    verified to run at — each proven by an M109 wait at that temperature in command order — so a
    non-extruding preheat (a 140 °C flush warm-up before the 260 °C wait) never reads as a
    printing temperature, and every temperature that must sit inside the loaded spool's range
    does. It is a subset of the commanded nozzle setpoints by construction; a receipt where it
    is not was not written by this store."""
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

    def __post_init__(self) -> None:
        # normalise before validating: a receipt read back from JSON carries lists where the
        # in-memory type keeps tuples, and the two spellings must be the one receipt
        for field_name in ("commanded_nozzle_setpoints", "commanded_bed_setpoints",
                            "extrusion_nozzle_temps"):
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
        if rogue := [f"{t:g}" for t in extrusion if t not in nozzle_cmds]:
            raise PrinterError("protocol_error",
                               f"A receipt would claim extrusion at {', '.join(rogue[:4])} °C, "
                               "temperatures its own commanded nozzle setpoints never commanded "
                               "— a receipt the parser could not have written is refused.",
                               reason="receipt_invalid")
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

    # ---- the on-disk shape ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Plain JSON data. Field order here is the order written; nothing that never belongs in
        a receipt (tokens, codes, replies) has a field to arrive in."""
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
        if isinstance(version, bool) or version != RECEIPT_VERSION:
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
        return cls(
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
            printer_display=display if display is None or isinstance(display, str) else None)

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
