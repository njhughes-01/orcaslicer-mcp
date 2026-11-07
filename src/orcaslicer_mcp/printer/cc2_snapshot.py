"""One picture from the CC2's own camera: one bounded HTTP GET of the stream, the first complete JPEG
frame out of it, and the stream released on every path. **It sends the printer no command at all** — no
MQTT session, no method of any kind, nothing toggled.

**Why this module composes the address instead of asking the printer for one.** ``cc2_camera.py`` asks
the printer's camera-address method (1042) and vets whatever address it advertises; that module is still
an internal candidate and still unregistered, because no 1042 reply has ever been captured from this
firmware and the community sources call that same method a **toggle**, so asking it could switch a
stream on or off. Nobody has settled which, and nothing here proposes to settle it by sending it. That
is a reason not to send 1042 — it is not a reason to leave the camera unexposed, because the stream
itself does not depend on the answer. The address used here is built from the address this server is
already configured to serve (``ORCA_CC2_URL``) plus the camera port and path the sources and this
printer's owner agree on:

* the protocol manual the community integration is built from describes the camera's MJPEG stream on
  **port 8080**, answering every request with the stream whatever the path or query says, requiring no
  authentication, and usually allowing **a single viewer**;
* that integration, when the printer's own method answers with no address (or a proxy is configured),
  composes ``f"http://{printer_ip}:8080/?action=stream"`` for itself out of its own
  ``CC2_VIDEO_PORT``/``CC2_VIDEO_PATH`` constants — a third-party fallback, which is only sensible if
  that is where a picture really is on stock firmware;
* **Nathan has opened that exact stream on his own Centauri Carbon 2 repeatedly and captured images
  from it** (2026-11-06), which is the strongest evidence available here, and the reason this is a tool
  rather than one more candidate.

Those three are evidence about the *stream*. Nothing here reads them as evidence about 1042: this module
never sends it, never sends ``enable``/``true``/``false``, never sends the 1054 that appears in Elegoo's
own enum defined and never called, and every reply says that a picture says nothing about that method's
semantics in either direction.

**What a picture does and does not prove.** A frame arriving means a stream answered at that address, on
this network, at that moment. It does not mean the printer's camera-address method is a read, that the
camera is the printer's own, that the image shows the print bed rather than the room, or that whoever was
already watching kept watching: the single-viewer limit means opening this can displace an existing
viewer, and the reply says so instead of finding out by asking twice.

**The address policy is the transport's own, reused rather than re-implemented.** The configured host
goes through ``cc2_transport.resolve_target`` — the pin every MQTT packet here is pinned with: a literal
is judged as it stands and never handed to a resolver, and a name is resolved once before anything
leaves and fails closed on a public, multicast, unspecified, link-local, broadcast, mixed or
multi-answer result — and then through :func:`checked_camera_base_url`, which refuses anything that is
not an IP literal and holds the pinned literal to the transport's own ``_refusal``. No resolver stands
between the policy decision and the socket, so the rebinding door the transport shut stays shut here.
And no printer address appears anywhere in this file: change ``ORCA_CC2_URL`` and this follows it,
including into the refusal that names the address it refused.

**The bounds, because a stream by definition never ends.** One request, never a second one, and no
redirect ever followed (``follow_redirects=False``, so a 3xx is a refusal naming its status rather than a
hop to an address this policy never judged); no proxy, ever (``trust_env=False``, so ``HTTP_PROXY`` and
``NO_PROXY`` cannot route a LAN camera fetch through some third machine); a wall-clock window of
``FRAME_DEADLINE_S`` measured from the moment the request starts, enforced by ``asyncio.timeout`` so even
a stream that trickles one byte per second cannot hold it open; at most ``MAX_READ_BYTES`` consumed from
the socket while hunting for a frame, counted **before** a chunk is appended or accepted so a frame that
only becomes reachable past the ceiling is refused rather than kept; and the kept frame capped at
``MAX_SNAPSHOT_BYTES``, the same ceiling one image reply carries elsewhere on this server. Past any of them the answer is a refusal that
names the number, and **nothing is ever truncated to fit** — half a JPEG is a corrupt picture, not a
small one.

**The release, which is the part a stream makes easy to get wrong.** The response is entered as an async
context manager and the client is closed in a ``finally`` inside a shielded scope, so the connection goes
back — and the printer's single viewer slot with it — on success, on a refusal, on an unexpected status,
on a mid-stream transport failure and on cancellation. Tests count the closes.

**Credentials.** The URL this module builds cannot hold a login: scheme, an IP literal this file
formatted, a fixed port, a fixed path. The response's headers are not a channel for anything either: one
sanitised, control-character-stripped, 100-character ``Content-Type`` word is kept and no other header
value, no body byte before the frame, and no exception's own sentence ever reaches a reply — which is how
this file keeps credentials out of its messages by construction rather than by filtering them afterwards.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anyio
import httpx

from .cc2_info import image_kind
from .cc2_transport import Cc2Config, _refusal, load_config, resolve_target
from .errors import PrinterError

# --- the stream's address, as the sources and this printer's owner draw it ------------------------
# Port 8080 is the camera port the protocol manual describes, and the same port cc2_camera's own
# vetting singles out in a note about a printer-advertised address. `?action=stream` is the query the
# Home Assistant integration composes and the one the printer's own page puts in an <img>. The host is
# never written here: it comes from the configuration, and only from the configuration.
CAMERA_PORT = 8080
STREAM_PATH = "/"
STREAM_QUERY = "action=stream"

# The whole budget for one picture, from the moment the request starts: connect, headers, and as many
# chunks as it takes to land one complete frame. This window is this server's own policy, not a protocol
# figure — no source names a budget for a stream at all — and it is the 10 s every other CC2 read here
# already waits on, so it is the number a caller expects. The client's own read timeout is the same
# number; this one is the hard bound, enforced by asyncio.timeout so a trickle cannot outrun it.
FRAME_DEADLINE_S = 10.0
CONNECT_TIMEOUT_S = 3.0        # the same reachability budget every read client here keeps
# No chunk-size knob: the stream is consumed exactly as the device hands bytes up, because waiting to
# fill a buffer of a chosen size is a second, silent latency the caller would pay for nothing. The two
# real bounds are the window above and MAX_READ_BYTES below, and both are checked on every step.

# The ceiling on what may be KEPT as one image reply: the same 2 MiB `cc2_file_thumbnail` holds, for the
# same reason — the bytes ride inline inside one tool reply, so this limit is about the conversation and
# not the printer. A frame over it is refused with its size named and never cut down.
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
# The ceiling on what may be READ from the socket while hunting. It sits above the kept-frame cap on
# purpose: the first bytes off a live stream are usually the tail of a frame the camera was already
# sending when this server connected, plus part headers, and one MJPEG frame from a 720p camera is a
# fraction of the cap below. Eight times the cap is past any frame that could be real and low enough
# that a device which streams forever without completing one cannot make this server hold memory until
# the kernel takes it.
MAX_READ_BYTES = 8 * 1024 * 1024

# A frame shorter than this is not a picture: it is a marker pair, a keepalive, or the remains of a frame
# this parser could not walk. No camera frame this server has any evidence about is this small.
_MIN_FRAME_BYTES = 128

_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"
_EOI_MARKER = 0xD9             # end of image, as a marker byte
_SOS_MARKER = 0xDA             # start of scan: past its header lies entropy-coded data
_STANDALONE = frozenset({0x01}) | set(range(0xD0, 0xD8))   # TEM and RSTn: markers with no length
_FILL = 0xFF

# The markers that may legitimately appear once a scan's entropy-coded data has begun. A frame with more
# than one scan — every progressive JPEG, and every baseline frame whose encoder emitted restart markers —
# does not end when its first scan runs into a marker, and it is legal for the tables a later scan needs
# to be (re)defined between scans: ``DHT`` (Huffman tables), ``DAC`` (arithmetic tables), ``DRI`` (a new
# restart interval) and ``DNL`` (a row count declared after the fact) all carry a declared length, and
# whatever lives inside those payloads is payload, not structure. ``SOS`` opens the next scan. Anything
# else found behind entropy-coded data is damage or the start of a different image, and is refused rather
# than walked.
_BETWEEN_SCANS = frozenset({0xC4, 0xCC, 0xDD, 0xDC})        # DHT, DAC, DRI, DNL

# The JPEG markers that may legitimately follow a frame's start: SOF0-SOF15, DHT, DRI, DQT, the APPn
# blocks (JFIF, EXIF, Adobe) and COM. Anything else behind an FF D8 is not the beginning of an image, it
# is two bytes that happen to look like one inside somebody else's data — and skipping it is what keeps
# a corrupted frame from being handed over as a picture.
_AFTER_SOI = frozenset(range(0xC0, 0xF0))

_MAX_CONTENT_TYPE = 100       # a header word this short is the only header text ever kept


# --------------------------------------------------------------------------- refusals


class _Unwalkable(Exception):
    """The frame's own segments do not walk: a byte where a marker must be, a stuffed ``FF 00``, a
    segment that declares a length too small to be one. Never a reply — the caller treats it as "this is
    not a frame I can prove", moves on to the next ``FF D8`` candidate, and eventually reports the stream
    as what it turned out to be instead of guessing a picture out of it."""


def _refuse(why: str, reason: str, **details) -> PrinterError:
    """A snapshot that did not happen, in the shape every printer tool speaks. The message is always this
    module's own words about what this server did and did not do: no header value, no device text and no
    exception's own sentence rides along, which is the rule every CC2 adapter keeps."""
    return PrinterError("protocol_error", f"The camera stream {why}.", reason=reason, **details)


def _unreachable(why: str, reason: str, **details) -> PrinterError:
    """A camera that could not be met at all. Named apart from a refusal because the honest difference
    between them is whether the device answered: nothing here contacted, or contacted and said no."""
    return PrinterError("not_reachable", f"The camera at the configured printer's address {why}.",
                        reason=reason, **details)


def _safe_content_type(value: Any) -> str | None:
    """The response's own ``Content-Type``, kept only as a short printable word, and reported rather than
    trusted: the fact that decides whether these bytes are a picture is the bytes. Anything unreadable,
    blank or non-text becomes None instead of an echoed string, and the strip plus the cap mean a hostile
    header cannot use a tool reply as a mailbox."""
    if not isinstance(value, str):
        return None
    text = "".join(ch if 0x20 <= ord(ch) < 0x7F else " " for ch in value).strip()
    if not text:
        return None
    return text[:_MAX_CONTENT_TYPE]


def _content_type_of(response: Any) -> str | None:
    """One header word of the reply, read as defensively as it is written: a fake seam whose headers are
    the wrong shape yields None, never an exception that would escape the tool as a bare failure."""
    headers = getattr(response, "headers", None)
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    try:
        return _safe_content_type(getter("content-type"))
    except Exception:      # any shape a stand-in client may answer with
        return None


# --------------------------------------------------------------------------- the address, from the config


def checked_camera_base_url(host: str) -> str:
    """The one URL a camera fetch may speak to, from the pinned literal address the transport judged. It
    must be an IP literal — the literal the policy judged — so no resolver stands between that decision
    and the socket, and it must pass the transport's own ``_refusal`` (public, multicast, unspecified,
    link-local, broadcast: all refused) exactly as ``cc2_upload.checked_upload_base_url`` holds the
    upload to it. A name, even one that once resolved to this printer, stays refused: it would put a
    lookup back on the path, and that is the rebinding door the transport shut.

    The URL built here has no room for a login: the host is a literal this function formatted, and the
    port, path and query are constants of this module. The ``@`` test below is a belt for anyone who
    edits this file, not the mechanism."""
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]  # the bracket form of an IPv6 literal; the brackets are not the address
    # A login in anything that reaches here is refused by name alone, with the whole string withheld: this
    # path cannot produce one (the transport's config check refuses a login in the address, and
    # resolve_target answers with an address, never an authority), so this is a belt — and a belt that
    # quotes the thing it is guarding is not a belt.
    if "@" in host:
        raise PrinterError("not_configured",
                           "The camera target was given as an address carrying a user name or password, "
                           "which this server neither uses nor repeats: nothing in it is reported, and "
                           "nothing was contacted.", reason="unsafe_address")
    shown = host or "<unset>"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise PrinterError("not_configured",
                           f"The camera target {shown!r} is not an IP literal, and a camera fetch goes "
                           "only to the pinned literal address of the configured printer: a name would "
                           "put a resolver back on the path between this check and the socket.",
                           reason="unsafe_address") from None
    if why := _refusal(ip):
        raise PrinterError("not_configured",
                           f"The camera target {shown} is a {why} address, and this adapter fetches a "
                           "camera picture only from the private LAN or loopback address of the one "
                           "printer it serves.", reason="unsafe_address")
    literal = f"[{ip}]" if ip.version == 6 else str(ip)
    url = f"http://{literal}:{CAMERA_PORT}{STREAM_PATH}?{STREAM_QUERY}"
    if "@" in url:   # unreachable by construction; a belt so a future edit cannot ship a login
        raise PrinterError("not_configured",
                           "The camera address this server composed carries a login, which it is built "
                           "never to do: refusing before anything is contacted.", reason="unsafe_address")
    return url


async def camera_target(config: Cc2Config | None = None) -> tuple[str, str]:
    """The camera URL for the configured printer and the literal host it points at, in the order that
    leaves no step to chance: read the configuration (``cc2_transport.load_config``, so ``ORCA_CC2_URL``
    is the only source of the address, and a missing one refuses in this process with nothing contacted);
    resolve and pin the host with the transport's own ``resolve_target``, which judges a literal address
    as it stands without any lookup at all; then build the URL from that pinned literal and check it
    again with this module's own guard.

    ``resolve_target`` hands the resolver the MQTT port because that is the shared resolver it is; what
    it returns is the printer's address, and this fetch reaches that address on the camera port. For the
    literal address the README tells a person to configure, no lookup happens on this path at all."""
    # `load_config` is the transport's own reader: no address is `not_configured` with a hint naming
    # where it comes from, and a login or a port in the address is refused there, before this module has
    # a host to build anything from.
    cfg = load_config() if config is None else config
    target = await resolve_target(cfg.host)
    return checked_camera_base_url(target.ip), target.ip


# --------------------------------------------------------------------------- the frame


@dataclass(frozen=True)
class JpegFrame:
    """One complete frame inside a buffer: where it starts and where it ends (one past its ``FF D9``).
    ``start`` is a fact worth reporting as `bytes_skipped_before_frame`: on a live MJPEG stream the first
    bytes are usually the tail of a frame the camera began before this server arrived, and skipping that
    tail instead of reporting it as a truncated picture is why a snapshot here is either a whole frame or
    a refusal, never a half one."""
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


def _marker_at(buf: bytearray, pos: int) -> tuple[int, int] | None:
    """``(marker byte, its index)``, or None when the buffer has not got the answer yet. A segment boundary
    must hold an ``FF``; any number of extra ``FF`` fill bytes may follow it before the marker byte, and
    JPEG says so in as many words, so a walk that assumes one fill byte gets the whole rest of the frame
    wrong. Raises ``_Unwalkable`` for a byte where a marker is required and is not one: a non-``FF`` at a
    segment boundary, or the stuffed ``FF 00`` that means a literal ``FF`` inside data and is never a
    marker.

    The index comes back with the marker because the length field lives **after** the marker byte: with
    two fill bytes in front of a ``DB`` the length is three bytes along, not two, and a walker that
    counted fills as part of the marker reads somebody else's payload as a length."""
    n = len(buf)
    if pos >= n:
        return None
    if buf[pos] != _FILL:
        raise _Unwalkable
    i = pos
    while i < n and buf[i] == _FILL:
        i += 1
    if i >= n:
        return None                      # a run of fill bytes with nothing after them yet
    if buf[i] == 0x00:
        raise _Unwalkable                # FF 00 is a stuffed byte, not a marker
    return buf[i], i


def _segment_length(buf: bytearray, at: int) -> int:
    """The declared length of the segment whose length field starts at ``at``, or None when the buffer
    has not got it or all of it yet. A length under 2 counts itself impossible: a JPEG segment's length
    covers its own two bytes, so 0 or 1 is damage, not a short segment."""
    if at + 2 > len(buf):
        return None
    length = int.from_bytes(buf[at:at + 2], "big")
    if length < 2:
        raise _Unwalkable
    return length


def _walk_to_scan(buf: bytearray, soi: int) -> int | None:
    """Walk the frame's marker segments from ``soi + 2`` up past its **first** start-of-scan header, and
    return the offset where that scan's entropy-coded data begins: ``-1`` for a frame that is degenerate or
    un-walkable (harmless: the caller moves on to the next ``FF D8``), and None when the buffer is too
    short to know yet, which is the "need more bytes" answer, not a refusal.

    Walking, rather than searching for the first ``FF D9``, is what makes this correct rather than
    usually-right: an ``APP1``/EXIF block can carry a nested thumbnail JPEG, and that thumbnail ends with
    a marker of its own. A plain search for ``FF D9`` after the start of image would then cut the frame in
    half inside the EXIF block and hand over a corrupt picture as if it were fine. Segment lengths say
    where each payload ends, so the nested end marker is skipped with the rest of the block, and only
    after the scan starts does an end marker mean the frame is over.

    This is the walk of the segments in front of the **first** scan only: what happens after that scan's
    data begins belongs to :func:`_walk_scans`, because a frame's first scan is not necessarily its last.
    """
    pos = soi + 2
    while True:
        found = _marker_at(buf, pos)
        if found is None:
            return None
        marker, at = found
        if marker == _EOI_MARKER:
            return -1                              # an image with no scan: a marker pair, not a picture
        if marker in _STANDALONE:
            pos = at + 1                           # TEM/RSTn carry no length field
            continue
        head = at + 1                              # the length field begins after the marker byte
        length = _segment_length(buf, head)
        if length is None:
            return None
        if marker == _SOS_MARKER:
            entropy = head + length
            return entropy if entropy <= len(buf) else None
        end_of_segment = head + length
        if end_of_segment > len(buf):
            return None                            # the payload is still arriving
        pos = end_of_segment


def _walk_scans(buf: bytearray, entropy: int) -> int | None:
    """From where one scan's entropy-coded data begins, walk **every remaining scan of that frame** and
    return the offset one past the frame's own terminating ``FF D9``: ``-1`` for a frame that cannot be
    walked (the caller moves on to the next ``FF D8``), and None when the buffer has not got the end of it
    yet ("need more bytes", never a refusal).

    Stopping at the first ``FF D9`` after the first start-of-scan is the second thing a naive hunt gets
    wrong — the first is the nested EXIF thumbnail, handled up in :func:`_walk_to_scan`. A frame with more
    than one scan (every progressive JPEG, and every baseline frame carrying restart markers) is not over
    when its first scan runs into a marker, and what lives between those scans is ordinary JPEG segments
    with payloads of their own: a ``DHT`` redefining the Huffman tables a later scan needs, a ``COM``
    comment, an ``APPn`` block. Any of those payloads can contain ``FF D9``, so searching forward through
    the scans for it cuts a real, multi-scan frame in half and hands its head over as a picture — exactly
    the failure this module exists not to have.

    What makes the walk possible is a guarantee rather than a guess: inside entropy-coded (scan) data every
    ``FF`` byte is **stuffed** as ``FF 00``, so the first ``FF`` followed by anything other than ``00`` is
    a real marker and not data. The walk therefore reads forward for that first real marker: ``FF 00`` is a
    stuffed byte and is stepped over, ``RSTn``/``TEM`` are standalone and are stepped over, ``EOI`` is the
    frame's end, ``SOS`` opens the next scan, and ``DHT``/``DAC``/``DRI`` sit between scans — each of those
    last four is skipped **by its declared length**, which is what keeps the bytes inside it out of the
    search. A marker that may not live there at all, or a segment whose declared length is not a length, is
    ``-1``: that candidate is refused, never truncated.
    """
    pos = entropy
    while True:
        i = buf.find(b"\xff", pos)
        if i < 0:
            return None                              # entropy data with no marker in it yet
        j = i
        while j < len(buf) and buf[j] == _FILL:
            j += 1
        if j >= len(buf):
            return None                              # a run of fill bytes: cannot be judged yet
        marker = buf[j]
        if marker == 0x00:
            pos = j + 1                              # a stuffed FF: ordinary data, keep walking
            continue
        if marker == _EOI_MARKER:
            return j + 1                             # one past the frame's own end-of-image marker byte
        if marker in _STANDALONE:
            pos = j + 1                              # RSTn/TEM carry no length field
            continue
        if marker != _SOS_MARKER and marker not in _BETWEEN_SCANS:
            return -1                                # cannot follow a scan: not a frame we can prove
        length = _segment_length(buf, j + 1)         # raises _Unwalkable for a length that is not one
        if length is None:
            return None
        end_of_segment = j + 1 + length
        if end_of_segment > len(buf):
            return None                              # its payload, and maybe the frame's end, is still in
        pos = end_of_segment                         # the whole payload goes by: none of it is structure


def find_jpeg_frame(buf: bytearray) -> JpegFrame | None:
    """The first complete JPEG frame in ``buf``, or None when nothing complete is in it yet — including
    when the only frame present is still arriving.

    What it refuses to do is the point of it. It never returns a frame it has not seen end; it never
    returns the bytes after a partial frame's end marker, which is why a buffer that begins mid-frame
    yields the next whole frame rather than a truncated one; and it steps over an ``FF D8`` whose next
    byte is not a marker that can open a frame, because those two bytes appear inside ordinary data often
    enough that trusting them would eventually ship somebody's bytes as a picture.

    The end is found by walking, in two parts: :func:`_walk_to_scan` steps over the header segments, so a
    nested thumbnail inside EXIF cannot end the frame, and :func:`_walk_scans` steps over every scan after
    the first, so a multi-scan frame ends at **its** end-of-image and not at the first ``FF D9`` that
    happens to sit inside a later segment's payload."""
    soi = 0
    while True:
        soi = buf.find(_SOI, soi)
        if soi < 0:
            return None
        if soi + 2 >= len(buf):
            return None                              # FF D8 with nothing after it yet
        probe = soi + 2
        while probe < len(buf) and buf[probe] == _FILL:
            probe += 1
        if probe >= len(buf):
            return None                              # a run of fill bytes: cannot be judged yet
        if buf[probe] not in _AFTER_SOI:
            soi = soi + 2                            # not the start of an image after all
            continue
        try:
            entropy = _walk_to_scan(buf, soi)         # the segments in front of the first scan
            if entropy is None:
                return None                          # this frame is not fully here yet
            if entropy < 0:
                soi = soi + 2                        # degenerate or un-walkable: next candidate
                continue
            end = _walk_scans(buf, entropy)          # every scan of it, the first included
        except _Unwalkable:
            soi = soi + 2
            continue
        if end is None:
            return None                              # a scan's data, or a later scan, is still arriving
        if end < 0:
            soi = soi + 2                            # damage mid-frame: try the next candidate
            continue
        return JpegFrame(start=soi, end=end)


# --------------------------------------------------------------------------- the picture


@dataclass(frozen=True)
class Cc2Snapshot:
    """One frame, read. ``data`` is the printer's own image bytes, and it exists to be put in exactly one
    MCP image block — never a string in a result, never cut short. Everything else is what a reader needs
    to judge where the picture came from and how hard it was to get."""
    data: bytes
    host: str
    url: str
    http_status: int
    content_type: str | None
    bytes_consumed: int
    bytes_skipped: int
    read_window_s: float
    elapsed_s: float

    @property
    def kind(self) -> str:
        """The image type the bytes declare for themselves, read with the same signature table the file
        thumbnails use. ``capture_snapshot`` only ever returns a frame whose bytes start at a JPEG start
        of image, so this is a fact from the bytes rather than a label applied to them."""
        return image_kind(self.data) or "unknown"

    @property
    def base64_chars(self) -> int:
        """The exact base64 length these bytes occupy on the wire, for a reader weighing a reply."""
        return -(-len(self.data) // 3) * 4

    def as_dict(self) -> dict:
        """Metadata only, never the image: the half that rides beside the image block, and the whole half
        a caller gets when their client renders nothing. Every claim here is about this fetch — that the
        address was composed rather than advertised, that no command and no session were involved, that the
        window and the caps were the ones this module names, and that the picture proves a stream answered
        and nothing else."""
        notes = [
            "this address was composed by this server from the configured printer address, the camera "
            f"port {CAMERA_PORT} and the path {STREAM_PATH}{STREAM_QUERY}: the printer was never asked "
            "for it and never advertised it",
            f"the picture is the first complete JPEG frame in the stream: {self.bytes_consumed} bytes "
            f"were read and {self.bytes_skipped} skipped ahead of it, inside a "
            f"{self.read_window_s:.1f} second window, this one taking {self.elapsed_s:.1f}",
            "the stream was read once and released, so this server is not holding the camera open",
            "the picture is the printer's own bytes, handed over as it sent them: nothing here decodes, "
            "inspects, re-encodes or vouches for what is drawn in it",
        ]
        return {
            "image_kind": self.kind,
            "bytes": len(self.data),
            "base64_chars": self.base64_chars,
            "stream_url": self.url,
            "stream_url_source": "composed_by_this_server",
            "stream_host": self.host,
            "stream_port": CAMERA_PORT,
            "stream_path": STREAM_PATH,
            "stream_query": STREAM_QUERY,
            "http_status": self.http_status,
            "content_type": self.content_type,
            "bytes_consumed": self.bytes_consumed,
            "bytes_skipped_before_frame": self.bytes_skipped,
            "read_window_s": round(self.read_window_s, 3),
            "elapsed_s": round(self.elapsed_s, 3),
            "frame_complete": True,
            "requests_sent": 1,
            "command_sent": False,
            "mqtt_session_used": False,
            "camera_method_sent": False,
            "printer_advertised_url": False,
            "notes": notes,
            "warnings": [
                "the protocol manual describes this camera stream as usually allowing a single viewer, so "
                "opening it may have been refused or may have displaced whoever was already watching",
                "a picture proves a stream answered at this address; it says nothing about the printer's "
                "own camera-address method, whose semantics — including whether it toggles the stream — "
                "remain unverified on this firmware, and nothing was sent to it either way",
                "the camera may not show the whole bed: nothing here establishes what is in the "
                "picture, so inspect the image before relying on it",
            ],
            "unverified": (
                "The camera address is composed from the configured host; the image alone does not "
                "verify which printer supplied it. The "
                "the reply shape of the printer's own camera-address method is still unestablished, which "
                "is why this fetch composes the address instead of asking for it and sends no command of "
                "any kind."),
        }


# --------------------------------------------------------------------------- the HTTP session (the seam)


class Cc2StreamHttp:
    """The production stream session, met with the one absolute URL :func:`camera_target` built for the
    pinned private literal. There is deliberately no ``base_url`` here: an absolute target leaves httpx no
    URL-merging rules to apply, so the string the policy judged is byte for byte the string that goes on
    the wire, and the reply can name the same one. Owns one httpx connection pool; ``aclose`` is mandatory
    and :func:`capture_snapshot` calls it on every path.

    Three settings on this client are the security policy, stated rather than inherited:
    ``trust_env=False`` so no ``HTTP_PROXY``/``HTTPS_PROXY``/``NO_PROXY`` in the environment can route a LAN
    camera fetch through a third machine (nor ``.netrc`` or a CA bundle be picked up from it);
    ``follow_redirects=False`` so a 3xx is a refusal naming its status instead of a hop to an address this
    server's LAN policy never judged; and a timeout of the whole window, with the standard connect budget,
    so a stalled read cannot keep a slot open past its own deadline either. No authentication of any kind
    is sent: the manual describes this stream as unauthenticated, and a camera that wants a login gets a
    refusal rather than a credential."""

    def __init__(self, url: str):
        self.url = url
        self._http = httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(FRAME_DEADLINE_S, connect=CONNECT_TIMEOUT_S))

    def stream(self):
        """The one GET, as an async context manager yielding the response. Entering it sends the request;
        httpx closes the response and returns its connection on the way out of the ``async with``, which is
        what hands the printer's camera back to whoever asks next."""
        return self._http.stream("GET", self.url)

    async def aclose(self) -> None:
        await self._http.aclose()


def _default_http_factory(url: str) -> Cc2StreamHttp:
    """The seam: production builds the real client here, and a test stands a scripted stream in its place.
    Nothing else in this module decides how bytes are fetched, and the only thing the seam is handed is the
    one absolute URL the policy judged — no headers, no auth, no proxy."""
    return Cc2StreamHttp(url)


# --------------------------------------------------------------------------- the capture


def _status_refusal(status: Any, content_type: str | None) -> PrinterError:
    """A stream that answered something other than 200. The status number is kept and nothing else from
    the reply is: no body is read, no location is followed, and no header is echoed. A 401/403 earns the
    extra sentence because it is the one case where a caller might be tempted to hand over a credential,
    which this server does not do to a camera."""
    shown = status if isinstance(status, int) and not isinstance(status, bool) else "of an unreadable kind"
    refusal = _refuse(f"answered HTTP {shown} instead of the stream", "camera_status",
                      http_status=status if isinstance(status, int) else None,
                      content_type=content_type)
    if status in (401, 403):
        return PrinterError("auth_rejected",
                            f"The camera at the configured printer's address answered HTTP {status} to "
                            "the stream request, which is a refusal to be seen. The protocol manual "
                            "describes this stream as needing no authentication, so this server sent no "
                            "credential and will not start.",
                            reason="camera_auth",
                            hint="Read the printer's own state with cc2_status, which speaks the printer's "
                                 "protocol with its access code. A camera that asks for a login belongs "
                                 "in a person's own viewer configuration, not in this server.")
    return refusal


async def capture_snapshot(*, config: Cc2Config | None = None,
                           http_factory: Callable[[str], Any] | None = None,
                           deadline_s: float = FRAME_DEADLINE_S,
                           max_bytes: int = MAX_READ_BYTES,
                           max_frame_bytes: int = MAX_SNAPSHOT_BYTES,
                           clock: Callable[[], float] = time.monotonic) -> Cc2Snapshot:
    """Take one picture from the configured printer's camera stream and return the first complete JPEG
    frame in it.

    In order, with every stop named before the next step runs: read the configuration and refuse inside
    this process when there is no address (``not_configured``, ``printer_contacted`` false in the tool's
    hands, nothing contacted); resolve and pin the host through the transport's own policy and refuse again
    if it is not a private or loopback literal; send exactly one GET of the composed stream URL; then read
    chunks until a complete frame lands, the window expires, the read ceiling is passed, the device
    answers anything other than 200, or the transport fails.

    **One request, always.** A window that expires, a mid-stream failure and a stream that never completes
    a frame are each reported as themselves and never retried: a stream that produced nothing may already
    have displaced another viewer, and nothing on this path is safe to repeat the way a request-only
    protocol pretends its reads are. The response and the client are released on every path including
    cancellation, the frame is never cut to fit a limit, and nothing of the device's reply reaches the
    caller except its status, one content-type word, and the frame's own bytes.
    """
    url, host = await camera_target(config)
    factory = http_factory or _default_http_factory
    http = factory(url)
    window = max(0.1, float(deadline_s))
    cap = max(_MIN_FRAME_BYTES, int(max_bytes))
    frame_cap = max(_MIN_FRAME_BYTES, int(max_frame_bytes))
    started = clock()
    consumed = 0
    answered = False            # did the device give us headers at all?
    content_type: str | None = None
    dropped = 0                 # bytes let go of while hunting, so "skipped" is a stream total, not a buffer one
    try:
        try:
            async with asyncio.timeout(window), http.stream() as response:
                answered = True
                status = getattr(response, "status_code", None)
                content_type = _content_type_of(response)
                if status != 200:
                    # Nothing else is read from a reply that is not the stream: no body, no Location, and
                    # of course no second request following it.
                    raise _status_refusal(status, content_type)
                buf = bytearray()
                async for chunk in response.aiter_bytes():
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise _refuse(f"delivered its stream as {type(chunk).__name__} rather than as "
                                      "bytes, so nothing was kept and nothing was guessed from it",
                                      "stream_shape")
                    if not len(chunk):
                        continue
                    consumed += len(chunk)
                    # The read ceiling is checked **before** the bytes are appended, searched or accepted,
                    # so an oversized chunk cannot buy a frame: a complete frame riding inside a chunk that
                    # takes this hunt past its ceiling is refused with the ceiling named, exactly like a
                    # stream that trickles past it. That ordering is also what bounds the buffer — every
                    # chunk that reaches `buf` has already been counted against `cap`, so what is held at any
                    # moment is at most `cap`. No chunk-size knob is passed to the read: the stream is
                    # consumed as httpx hands bytes up (what one transport read delivered, in practice), and
                    # waiting to fill a buffer of a chosen size is the silent latency the module docstring
                    # already refuses to pay for.
                    if consumed > cap:
                        raise _refuse(
                            f"gave {consumed} bytes inside {clock() - started:.1f} seconds, past this "
                            f"server's {cap} byte ceiling for one hunt. A frame is not accepted from a hunt "
                            "that overran its ceiling: the stream was closed, nothing was kept and nothing "
                            "was asked again",
                            "stream_cap_reached", bytes_consumed=consumed, max_bytes=cap,
                            read_window_s=round(window, 3))
                    buf += chunk
                    frame = find_jpeg_frame(buf)
                    if frame is not None:
                        if frame.length > frame_cap:
                            raise _refuse(
                                f"sent a complete frame of {frame.length} bytes, over this server's "
                                f"{frame_cap} byte limit for one image reply. Nothing was truncated and "
                                "nothing was asked again",
                                "snapshot_too_large", bytes=frame.length, max_bytes=frame_cap)
                        if frame.length < _MIN_FRAME_BYTES:
                            raise _refuse(
                                f"sent a frame of {frame.length} bytes between its start-of-image and its "
                                f"end-of-image markers, under the {_MIN_FRAME_BYTES} bytes below which a "
                                "frame is a marker pair rather than a picture. Nothing was kept",
                                "frame_too_small", bytes=frame.length, minimum_bytes=_MIN_FRAME_BYTES)
                        data = bytes(buf[frame.start:frame.end])
                        if image_kind(data) != "jpeg":
                            # Reachable only if the start of image turned out not to be a signature this
                            # server will name. Refusing beats labelling: a picture is sent out under a
                            # type its own bytes support, or not at all.
                            raise _refuse(
                                f"sent {len(data)} bytes beginning {bytes(data[:4]).hex()!r} between a "
                                "start-of-image and an end-of-image marker, which is none of the image "
                                "types this server names by signature. Nothing was sent on under a "
                                "guessed type",
                                "snapshot_type_unknown", bytes=len(data))
                        return Cc2Snapshot(data=data, host=host, url=url,
                                           http_status=200, content_type=content_type,
                                           bytes_consumed=consumed, bytes_skipped=dropped + frame.start,
                                           read_window_s=window, elapsed_s=clock() - started)
                    if buf.find(_SOI) < 0:
                        # Nothing here can begin a frame. Keep the last byte so a start-of-image split
                        # across two chunks is still seen, and let go of the rest: a stream that streams
                        # forever must not be able to make this server hold it. What is dropped is counted,
                        # because `bytes_skipped_before_frame` is a fact about the stream and not about
                        # whatever happens to be in the buffer when the frame lands.
                        dropped += len(buf) - 1
                        del buf[:-1]
                    elif len(buf) > frame_cap:
                        # Past the size a keepable frame could ever be, whatever is in front of the buffer
                        # is worthless: a candidate that large can only end over the cap, and a later frame
                        # may already be whole in here. Keep from the last start-of-image onward, which
                        # both bounds the hunt harder than the read ceiling does and walks past a frame the
                        # camera sent damaged.
                        last = buf.rfind(_SOI)
                        if last > 0:
                            dropped += last
                            del buf[:last]
            # The stream ended by itself with no complete frame in it. That is the device's own sentence,
            # and it is not a reason to open it a second time.
            raise _refuse(
                f"closed after {consumed} bytes without a complete frame in them. Nothing was kept, "
                "nothing was asked again, and the connection is released",
                "stream_closed_early", bytes_consumed=consumed, requests_sent=1)
        except TimeoutError as e:
            # The window is the one bound that always holds, so this is the ordinary way a camera that is
            # busy, off, or full to its viewer limit reports itself over HTTP.
            if not answered:
                raise _unreachable(f"did not answer the stream request within {window:.1f} seconds, so "
                                   "no stream was opened and nothing was kept from it",
                                   "camera_timeout", read_window_s=round(window, 3),
                                   requests_sent=1) from e
            raise _refuse(
                f"answered but sent no complete frame within its {window:.1f} second window "
                f"({consumed} bytes read). Nothing was kept, nothing was asked again, and the connection "
                "is released",
                "no_frame_in_window", bytes_consumed=consumed, read_window_s=round(window, 3),
                content_type=content_type, requests_sent=1) from e
        except httpx.TimeoutException as e:
            # The client's own read timeout, which normally loses the race to the window above and so is
            # reported here only when it arrives first. Same two answers.
            if not answered:
                raise _unreachable("did not answer the stream request before its own read timeout, so no "
                                   "stream was opened", "camera_timeout", requests_sent=1) from e
            raise _refuse("stopped delivering bytes mid-stream before any complete frame arrived. "
                          "Nothing was kept, nothing was asked again, and the connection is released",
                          "stream_stalled", bytes_consumed=consumed,
                          content_type=content_type, requests_sent=1) from e
        except httpx.TransportError as e:
            if not answered:
                raise _unreachable("did not answer the stream request at all: the connection failed "
                                   "before a reply arrived, so nothing was read and nothing was kept",
                                   "camera_unreachable", requests_sent=1) from e
            raise _refuse(f"failed mid-stream after {consumed} bytes, before any complete frame arrived. "
                          "Nothing was kept, nothing was asked again, and the connection is released",
                          "stream_failed", bytes_consumed=consumed,
                          content_type=content_type, requests_sent=1) from e
        except httpx.InvalidURL as e:
            # httpx raises this for an address or URL it cannot form. The address was checked before it was
            # handed over, so this is a configuration fault, told without echoing the device or the
            # client's own internal target.
            raise PrinterError("not_configured",
                               "The camera address this server composed was refused by its own HTTP "
                               "client before anything was contacted.", reason="unsafe_address") from e
    finally:
        # Shielded, because a caller cancelled on the way out of a tool still has to give the printer its
        # camera back; suppressed, because a close that fails must not mask the refusal already on its way
        # out. The cancellation, if there was one, is still delivered when this scope ends.
        with anyio.CancelScope(shield=True), contextlib.suppress(Exception):
            await http.aclose()
