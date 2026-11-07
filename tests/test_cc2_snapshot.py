"""The CC2 camera snapshot against a scripted stream, in the house style of the camera-candidate and
history tests: no socket is opened, the HTTP seam records everything it was asked for, and the guards
count their own hits so "one request", "never retried" and "the stream was released" are measured zeros
rather than claims.

**The address, which is the security surface.** Every fetch goes to the configured printer's literal
pinned address on the camera port with the stream query, and the tests prove three separate things about
it: that the host follows ``ORCA_CC2_URL`` (nothing is hardcoded — a different configured host produces a
different URL at the seam), that the transport's own address policy is what decides (a public, multicast,
unspecified, link-local, broadcast or non-literal target is refused as ``unsafe_address`` with nothing
contacted), and that with a literal address configured **no lookup happens at all** (the network guards
count zero hits while a picture still comes back). A login cannot ride the composed URL, and the refusal
that catches one if it ever could withholds the whole string rather than quoting it.

**The bounds.** One request per call, proven by counting the seam entries, including on every failure
path. The 10-second window is exercised at 0.25 s so the test is fast and still proves the window is what
stops the read, not the fixture. The read ceiling and the kept-frame ceiling each refuse with their
number named and return no bytes, because a snapshot is either a whole frame or a refusal — including a
whole frame that arrives inside a single chunk that breaches the read ceiling, which is refused too
(the ceiling is counted before a chunk is appended, which is also what bounds what the hunt may hold).

**The frame hunt.** The parser is tested on the cases that make MJPEG a trap: the tail of a frame already
in flight when this server connects (skipped, not truncated), a frame split across chunks, part headers
before the picture, two frames (the first wins), fill bytes between markers, a start-of-image lookalike
inside ordinary data (skipped), a declared segment length of zero (skipped), a truncated frame (None,
never a half picture), and the two cases a plain ``find`` gets wrong: an EXIF block carrying a nested
thumbnail that ends with an end-of-image marker of its own, and a **multi-scan (progressive)** frame whose
scan 2 tables are redefined by a later segment whose payload holds an end-of-image of its own. Walking the
segment lengths, past the first scan and to the frame's own end marker, is what stops either from cutting
the real frame in half and the halves being handed over as if they were fine; a marker that cannot legally
follow a scan refuses the candidate, and a multi-scan frame missing its end is still None.

**The release.** Every path closes the client exactly once: success, a frame that is too big, a frame that
is too small, a non-200, a stalled stream, a mid-stream transport failure, a stream that ends by itself, a
chunk that is not bytes, the window expiring — and cancellation, which is the one an ``async with`` alone
does not guarantee.

**And what the client is handed.** The tool layer is tested on the reply a caller can act on and on the
MCP-visible result: the image block, a summary line that carries the single-viewer warning beside the
size/type/origin sentence, and the adapter's whole sanitised metadata as JSON, asserted through the
server's own tool manager with the conversion the server itself performs, base64 decoded back to the
frame's bytes — so a metadata dict that is built and then dropped fails here instead of shipping.

**And what is never sent.** This module speaks no printer protocol: no camera-address method, no enable
flag, no 1054, no MQTT, no auth header, no second try. Those are pinned by scanning the recorded seam
calls and this module's own source, so a later edit that "just" toggles the stream fails here.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import ipaddress
import json
import re
import socket
from pathlib import Path

import httpx
import pytest

from orcaslicer_mcp.printer import cc2_snapshot as snapp
from orcaslicer_mcp.printer.cc2_transport import Cc2Config, PrinterError

ROOT = Path(__file__).resolve().parents[1]

HOST = "192.0.2.10"                  # documented TEST-NET, like the whole lane; never a live address
CONF_HOST = "198.51.100.7"           # a second documented TEST-NET address, for the follows-config test
EXPECTED_URL = f"http://{HOST}:8080/?action=stream"


# --------------------------------------------------------------------------- synthetic JPEGs


def _segment(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker, (2 + len(payload)) >> 8, (2 + len(payload)) & 0xFF]) + payload


def jpeg(*, body: bytes = b"\x11\x22\x33" * 40, exif_nested: bool = True, fills: int = 0,
         pad_to: int | None = None) -> bytes:
    """A small, structurally honest JPEG: SOI, an optional APP1/EXIF block whose payload carries a
    **nested thumbnail that ends with its own ``FF D9``**, DQT, SOF0, SOS, then scan data with every
    ``FF`` byte-stuffed (so it holds no real marker), then the frame's own ``FF D9``. ``pad_to`` inflates
    the scan data, for the size-ceiling tests, without making the structure any less real.
    """
    if pad_to:
        body = (b"\x11\x22\x33" * (pad_to // 3 + 1))[:pad_to]
    out = bytearray(b"\xff\xd8")
    if exif_nested:
        out += _segment(0xE1, b"Exif\x00\x00" + b"\xff\xd8\xff\xdb\x00\x05\x01\x02\xff\xd9")
    out += b"\xff" * fills + _segment(0xDB, b"\x00\x01\x02\x03")
    out += _segment(0xC0, b"\x08\x00\x10\x00\x10\x01\x11\x00")
    out += _segment(0xDA, b"\x01\x01\x00\x00\x3f\x00")
    out += body.replace(b"\xff", b"\xff\x00")
    out += b"\xff\xd9"
    return bytes(out)


def multi_scan_jpeg(*, scans: int = 2, restarts: bool = False, trap: bool = True,
                    tail: bool = True) -> bytes:
    """A structurally honest **multi-scan** JPEG, which is what every progressive JPEG is: SOI, DQT, an
    ``SOF2`` (progressive) frame header, a DHT, the first ``SOS`` and its entropy-coded data (every ``FF``
    stuffed), then — between the scans — a **second DHT whose payload contains ``FF D9``**, the next ``SOS``
    and its data, and only then the frame's own ``FF D9``. ``scans=3`` adds a ``DAC`` and a ``DRI`` between
    the second and third scans; ``restarts`` puts a real ``RST0`` inside the first scan's data, the way a
    restart-marked stream carries one; ``tail=False`` leaves the frame unfinished.

    The embedded ``FF D9`` is the point. A hunt that stops at the first end-of-image bytes after the first
    start-of-scan cuts this frame in half inside that later DHT segment and hands the head over as a
    picture — the same class of failure the EXIF-nested thumbnail catches on the other side of the frame,
    and the reason the walk keeps going past the first scan instead of searching through it.
    """
    out = bytearray(b"\xff\xd8")
    out += _segment(0xDB, b"\x00\x01\x02\x03")                    # DQT
    out += _segment(0xC2, b"\x08\x00\x10\x00\x10\x01\x11\x00")    # SOF2: progressive, one component
    out += _segment(0xC4, b"\x00\x00\x01\x05\x01\x01\x01\x01")    # DHT before the first scan
    out += _segment(0xDA, b"\x01\x01\x00\x01\x00\x00")            # SOS: scan 1 (progressive DC)
    first = b"\x22\x33\x44" * 30
    if restarts:
        first = first[:60] + b"\xff\xd0" + first[60:]             # a real RST0 marker inside the scan
    out += first.replace(b"\xff", b"\xff\x00")
    if scans >= 2:
        payload = (b"\xff\xd9\x55\x66" * 8) if trap else (b"\x55\x66\x77\x88" * 8)
        out += _segment(0xC4, payload)                            # DHT between scans: the embedded EOI
        out += _segment(0xDA, b"\x01\x01\x00\x3f\x00\x10")        # SOS: scan 2 (successive approximation)
        out += (b"\x99\xaa" * 30).replace(b"\xff", b"\xff\x00")
    if scans >= 3:
        out += _segment(0xCC, b"\x01\x00")                        # DAC between scans
        out += _segment(0xDD, b"\x00\x04")                        # DRI: a new restart interval
        out += _segment(0xDA, b"\x01\x01\x00\x01\x3f\x00")        # SOS: scan 3 (AC spectral selection)
        out += (b"\x11\x22" * 30).replace(b"\xff", b"\xff\x00")
    if tail:
        out += b"\xff\xd9"
    return bytes(out)


GOOD = jpeg()
PART_HEAD = b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(GOOD)


# --------------------------------------------------------------------------- the scripted stream


class FakeResponse:
    """The response the seam hands back: a status, one header dict, and the byte chunks the test scripted.
    Reading it records that bytes were asked for at all, so "nothing was read from a reply that is not the
    stream" is a measurement and not an assumption."""

    def __init__(self, fake: FakeStreamHttp):
        self._fake = fake
        self.status_code = fake.status
        self.headers = dict(fake.headers)

    async def aiter_bytes(self):
        fake = self._fake
        fake.body_read = True
        if fake.error_at == -1:
            raise fake.error
        for index, chunk in enumerate(fake.chunks):
            if fake.hang_at == index:
                await asyncio.sleep(3600)            # cancelled by the caller's own timeout or task
            await fake.step()
            if chunk is not None:
                yield chunk
            if fake.error_at == index:
                raise fake.error
        if fake.endless:
            while True:                              # a stream that trickles forever: only a window stops it
                await asyncio.sleep(0.005)
                yield fake.trickle
        if fake.error_after is not None:
            raise fake.error_after


class FakeStreamHttp:
    """The seam a test stands in place of ``Cc2StreamHttp``.

    It counts the things this module promises about its own network behaviour: how many times the stream
    was entered (one per call, on every path), whether the response was exited, whether the client was
    closed and how many times, and whether any bytes were read. ``hang_at`` freezes the read mid-stream so
    a real cancellation can be aimed at it, and ``endless`` trickles forever so the window, not the
    fixture, is what ends the read."""

    def __init__(self, *, status: int = 200, headers: dict | None = None, chunks=(), error=None,
                 error_at: int | None = None, error_after=None, endless: bool = False,
                 trickle: bytes = b"\x00", hang_at: int | None = None, hang_on_enter: bool = False,
                 enter_error=None, close_error=None):
        self.status, self.headers = status, dict(headers or {"content-type": "multipart/x-mixed-replace; boundary=frame"})
        self.chunks = list(chunks)
        self.error, self.error_at, self.error_after = error, error_at, error_after
        self.endless, self.trickle = endless, trickle
        self.hang_at, self.hang_on_enter, self.enter_error = hang_at, hang_on_enter, enter_error
        self.close_error = close_error
        self.entered = 0
        self.exited = 0
        self.closed = 0
        self.body_read = False
        self.url: str | None = None

    def factory(self):
        """The callable the module's seam expects: built with the pinned URL and nothing else."""
        def build(base_url: str) -> FakeStreamHttp:
            self.url = base_url
            return self
        return build

    def stream(self):
        fake = self

        class _Ctx:
            async def __aenter__(self):
                fake.entered += 1
                if fake.enter_error is not None:
                    raise fake.enter_error
                if fake.hang_on_enter:
                    await asyncio.sleep(3600)
                return FakeResponse(fake)

            async def __aexit__(self, *exc):
                fake.exited += 1
                return False

        return _Ctx()

    async def step(self) -> None:
        await asyncio.sleep(0)

    async def aclose(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error

    @property
    def counts(self) -> tuple[int, int, int]:
        return (self.entered, self.exited, self.closed)


async def capture(fake: FakeStreamHttp, *, host: str = HOST, **kwargs):
    """Run one capture against the scripted stream, returning the snapshot (or letting the refusal
    escape) and having recorded the URL the seam was handed."""
    kwargs.setdefault("config", Cc2Config(host=host))
    kwargs.setdefault("http_factory", fake.factory())
    return await snapp.capture_snapshot(**kwargs)


# --------------------------------------------------------------------------- the address, and the policy on it


def test_the_stream_url_is_the_configured_host_on_the_camera_port():
    assert snapp.checked_camera_base_url(HOST) == EXPECTED_URL
    assert snapp.CAMERA_PORT == 8080 and snapp.STREAM_QUERY == "action=stream"


def test_an_ipv6_literal_is_bracketed_and_its_brackets_are_not_part_of_the_address():
    literal = "fd00::1234"
    url = snapp.checked_camera_base_url(literal)
    assert url == f"http://[{literal}]:8080/?action=stream"
    assert snapp.checked_camera_base_url(f"[{literal}]") == url


@pytest.mark.parametrize("bad", ["100.64.0.1", "169.254.5.5", "224.0.0.1", "0.0.0.0", "255.255.255.255",
                                 "printer.local", "", "192.0.2.10:8080"])
def test_only_a_pinned_private_literal_is_ever_a_target(bad):
    # 100.64.0.1 (shared CGNAT space) stands in for "a public address": the transport judges it public and
    # refuses it, and unlike a real host's address it cannot route anywhere if the policy ever broke.
    with pytest.raises(PrinterError) as caught:
        snapp.checked_camera_base_url(bad)
    assert caught.value.code == "not_configured"
    assert caught.value.details["reason"] == "unsafe_address"


@pytest.mark.parametrize("loop", ["127.0.0.1", "::1"])
def test_loopback_is_a_legal_target_because_the_transport_says_so(loop):
    assert snapp.checked_camera_base_url(loop).startswith("http://")


def test_a_host_carrying_a_login_is_refused_and_the_whole_string_is_withheld():
    # The real path cannot hand this a login (the transport's own config check refuses a login in the
    # address, and resolve_target answers with an address, never an authority), so this is a belt for a
    # future edit — and it is the one refusal here that must quote nothing, not even the host.
    with pytest.raises(PrinterError) as caught:
        snapp.checked_camera_base_url("admin:hunter2@192.0.2.10")
    assert caught.value.code == "not_configured"
    text = caught.value.message + json.dumps(caught.value.details, default=str)
    assert "hunter2" not in text and "admin" not in text


async def test_camera_target_pins_the_literal_without_any_lookup(monkeypatch):
    """A literal address is judged as it stands, exactly as the transport judges it: no resolver is
    involved, so there is no answer for a rebinding to have steered."""
    def trip(*a, **k):
        raise AssertionError("a literal address must not reach a resolver")
    monkeypatch.setattr(socket, "getaddrinfo", trip)
    url, host = await snapp.camera_target(Cc2Config(host=HOST))
    assert (url, host) == (EXPECTED_URL, HOST)


async def test_camera_target_refuses_a_public_configured_address_before_anything_is_contacted():
    """A public configured address is refused by the transport's own pin, in the transport's own words, and
    the refusal arrives before any request: the camera fetch adds no policy of its own and loosens none."""
    with pytest.raises(PrinterError) as caught:
        await snapp.camera_target(Cc2Config(host="100.64.0.1"))
    assert caught.value.code == "not_configured"
    assert "public" in caught.value.message


async def test_with_no_configured_address_the_refusal_is_the_transport_s_own(monkeypatch):
    monkeypatch.delenv("ORCA_CC2_URL", raising=False)
    monkeypatch.delenv("ORCA_CC2_ACCESS_CODE", raising=False)
    monkeypatch.delenv("ORCA_CC2_ACCESS_CODE_FILE", raising=False)
    with pytest.raises(PrinterError) as caught:
        await snapp.camera_target()
    assert caught.value.code == "not_configured"


async def test_the_address_follows_the_configuration_and_is_never_hardcoded(monkeypatch):
    """The seam is handed a URL built from the host in the environment, and a different configured host
    produces a different URL: no printer address, and in particular no home address, lives in the
    adapter."""
    monkeypatch.setenv("ORCA_CC2_URL", CONF_HOST)
    fake = FakeStreamHttp(chunks=[GOOD])
    shot = await snapp.capture_snapshot(http_factory=fake.factory())
    assert fake.url == f"http://{CONF_HOST}:8080/?action=stream"
    assert shot.host == CONF_HOST and shot.url == fake.url


# --------------------------------------------------------------------------- the frame hunt


def test_a_whole_frame_is_found_from_the_first_byte_to_the_last():
    frame = snapp.find_jpeg_frame(bytearray(GOOD))
    assert (frame.start, frame.end) == (0, len(GOOD))
    assert bytes(GOOD[frame.start:frame.end]) == GOOD


@pytest.mark.parametrize("lead, expect_start", [
    (PART_HEAD, len(PART_HEAD)),
    (b"\x33\x44\xff\xd9", 4),                 # the tail of a frame already in flight: its EOI, then ours
    (b"\x00" * 300, 300),
])
def test_junk_before_the_frame_is_skipped_and_never_reported_as_a_truncated_picture(lead, expect_start):
    buf = bytearray(lead + GOOD)
    frame = snapp.find_jpeg_frame(buf)
    assert frame.start == expect_start and frame.end == len(buf)


def test_the_first_of_two_frames_wins():
    buf = bytearray(GOOD + PART_HEAD + GOOD)
    frame = snapp.find_jpeg_frame(buf)
    assert (frame.start, frame.end) == (0, len(GOOD))


def test_a_start_of_image_lookalike_inside_data_is_stepped_over():
    bogus = b"\xff\xd8\x00\x01" + b"\xff\xd8\x22" + b"\xff\xd8\xff\xdb\x00\x00"
    buf = bytearray(bogus + GOOD)
    frame = snapp.find_jpeg_frame(buf)
    assert (frame.start, frame.end) == (len(bogus), len(buf))


def test_fill_bytes_between_markers_do_not_move_the_length_field():
    """JPEG allows any number of ``FF`` fill bytes before a marker. A walker that assumes exactly one
    reads the payload as a length, and then reports a frame that never ends: this is the fixture that
    would fail if the walk counted fills as part of the marker."""
    padded = jpeg(fills=3)
    frame = snapp.find_jpeg_frame(bytearray(padded))
    assert (frame.start, frame.end) == (0, len(padded))


def test_an_exif_nested_thumbnail_cannot_cut_the_frame_in_half():
    """The case a first-``FF D9`` search gets wrong, and the reason this walks segments: the nested
    thumbnail inside APP1 ends with an end-of-image marker, and cutting there would hand over the head of
    a real frame as if it were a whole picture."""
    assert b"\xff\xd9" in GOOD[:60]
    frame = snapp.find_jpeg_frame(bytearray(GOOD))
    assert frame.end == len(GOOD) and frame.length == len(GOOD)
    assert snapp.image_kind(bytes(GOOD[frame.start:frame.end])) == "jpeg"


def test_scan_data_holding_an_inner_start_of_image_does_not_start_a_new_frame():
    sneaky = jpeg(body=b"\xff\x00\xff\xd8\xff\x00" * 20)
    frame = snapp.find_jpeg_frame(bytearray(sneaky))
    assert (frame.start, frame.end) == (0, len(sneaky))


# ------------------------------------------------------------------ multi-scan (progressive) frames


@pytest.mark.parametrize("scans, restarts", [(2, False), (3, False), (2, True), (3, True)])
def test_a_multi_scan_frame_is_whole_from_its_first_byte_to_its_own_last(scans, restarts):
    """Every scan of a progressive or restart-marked frame belongs to the frame: the hunt walks past the
    first scan's end and stops at **the frame's** end-of-image, so what is handed over is the whole frame
    and not its head."""
    multi = multi_scan_jpeg(scans=scans, restarts=restarts)
    frame = snapp.find_jpeg_frame(bytearray(multi))
    assert (frame.start, frame.end) == (0, len(multi))
    assert bytes(multi[frame.start:frame.end]) == multi
    assert snapp.image_kind(bytes(multi)) == "jpeg"


def test_an_embedded_end_marker_inside_a_later_segment_does_not_cut_a_multi_scan_frame():
    """The reproduction, with the byte the old hunt stopped at named. ``FF D9`` sits inside the payload of
    the DHT that lives between the two scans, which is a legal place for it: segment payloads are not
    stuffed, so only a walk that honours the declared length can skip it. A hunt that searched for the first
    end-of-image after the first start-of-scan cut the frame there and returned a corrupt picture; the walk
    returns the whole frame instead."""
    multi = multi_scan_jpeg()
    buf = bytearray(multi)
    assert b"\xff\xd9" in multi[:-2]                # there IS an end-of-image lookalike before the real one
    soi = buf.find(b"\xff\xd8")
    sos = buf.find(b"\xff\xda", soi)
    entropy = sos + 2 + int.from_bytes(buf[sos + 2:sos + 4], "big")   # where scan 1's data begins
    naive_end = buf.find(b"\xff\xd9", entropy) + 2
    assert 0 < naive_end < len(multi)               # where the old hunt stopped: mid-frame, mid-segment
    frame = snapp.find_jpeg_frame(buf)
    assert frame.end == len(multi) and frame.length > naive_end - soi
    assert snapp.image_kind(bytes(multi[frame.start:frame.end])) == "jpeg"


def test_a_multi_scan_frame_with_no_embedded_end_marker_is_still_whole():
    """The same shape without the trap payload (multi-scan, restart-marked, nothing embedded), so the pass
    cannot be about the trap fixture alone: multi-scan handling is what carries it."""
    multi = multi_scan_jpeg(scans=3, trap=False, restarts=True)
    frame = snapp.find_jpeg_frame(bytearray(multi))
    assert (frame.start, frame.end) == (0, len(multi))


@pytest.mark.parametrize("cut", [2, 3, 6, 40])
def test_a_multi_scan_frame_that_has_not_reached_its_own_end_is_never_a_frame(cut):
    """A progressive frame that is missing its last scan's bytes, its last scan, or its end marker is not a
    picture yet: the answer is None, so the capture keeps reading or refuses, and a half frame is never
    handed over as a small one."""
    multi = multi_scan_jpeg(scans=3)[:-cut]
    assert snapp.find_jpeg_frame(bytearray(multi)) is None


def test_a_marker_that_cannot_follow_a_scan_is_refused_rather_than_walked_through():
    """A segment that has no business appearing where a scan's data should still be running (here an
    ``APPn``) means this is not a frame the hunt can prove, and it is refused. Frames do arrive back to back
    on a stream, so the next real frame after the damage is still found whole."""
    damaged = multi_scan_jpeg(tail=False) + _segment(0xE0, b"junk\x11\x22") + b"\xff\xd9"
    assert snapp.find_jpeg_frame(bytearray(damaged)) is None
    buf = bytearray(damaged + GOOD)
    frame = snapp.find_jpeg_frame(buf)
    assert (frame.start, frame.end) == (len(damaged), len(buf))


@pytest.mark.parametrize("incomplete", [
    GOOD[:5], GOOD[:-1], GOOD[:-6], b"\xff\xd8", b"\xff\xd8\xff", b"\xff\xd8\xff\xdb",
    b"\xff\xd8\xff\xdb\x00", b"\x00" * 400,
])
def test_nothing_is_ever_reported_as_a_frame_before_it_has_ended(incomplete):
    assert snapp.find_jpeg_frame(bytearray(incomplete)) is None


def test_a_frame_split_across_chunks_is_still_one_frame():
    """The bytes arrive as the device hands them up, so no chunk boundary may matter: the frame is only
    returned once its last byte has arrived, and its bytes are exactly the frame's."""
    fake = FakeStreamHttp(chunks=[GOOD[i:i + 37] for i in range(0, len(GOOD), 37)])
    shot = asyncio.run(capture(fake))
    assert shot.data == GOOD and shot.bytes_skipped == 0


# --------------------------------------------------------------------------- one capture, and what it sends


async def test_a_picture_comes_back_whole_with_the_pinned_url_and_one_request():
    fake = FakeStreamHttp(chunks=[PART_HEAD, GOOD[:40], GOOD[40:], b"\r\n--frame"])
    shot = await capture(fake)
    assert shot.data == GOOD
    assert shot.kind == "jpeg" and shot.http_status == 200
    assert (fake.entered, fake.exited, fake.closed) == (1, 1, 1)   # one request, one release
    assert shot.url == EXPECTED_URL and shot.host == HOST
    assert shot.bytes_consumed >= len(PART_HEAD) + len(GOOD)
    assert shot.bytes_skipped == len(PART_HEAD)


async def test_the_reply_says_what_was_read_and_what_was_never_sent():
    fake = FakeStreamHttp(chunks=[GOOD])
    out = (await capture(fake)).as_dict()
    assert json.loads(json.dumps(out))["image_kind"] == "jpeg"
    assert "verified_on_this_printer" not in out
    assert out["stream_url_source"] == "composed_by_this_server"
    assert out["stream_url"] == EXPECTED_URL
    assert out["stream_port"] == 8080 and out["stream_query"] == "action=stream"
    for field in ("command_sent", "mqtt_session_used", "camera_method_sent", "printer_advertised_url"):
        assert out[field] is False
    assert out["requests_sent"] == 1 and out["frame_complete"] is True
    assert "composed by this server" in " ".join(out["notes"])
    assert "single viewer" in " ".join(out["warnings"])
    assert "camera-address method" in out["unverified"]


async def test_the_window_is_the_thing_that_stops_a_stream_that_never_completes_a_frame():
    fake = FakeStreamHttp(chunks=[b"\x00" * 64], endless=True, trickle=b"\x11")
    with pytest.raises(PrinterError) as caught:
        await capture(fake, deadline_s=0.25)
    assert caught.value.code == "protocol_error"
    assert caught.value.details["reason"] == "no_frame_in_window"
    assert caught.value.details["bytes_consumed"] > 0
    assert caught.value.details["read_window_s"] == 0.25
    assert (fake.entered, fake.exited, fake.closed) == (1, 1, 1)
    # The fixture would have streamed forever: only the window can have ended this read, and the refusal
    # says how many bytes it had managed in that time.
    assert fake.chunks == [b"\x00" * 64]


async def test_a_stream_that_never_answers_is_named_as_unreachable_not_as_a_refusal():
    fake = FakeStreamHttp(hang_on_enter=True)
    with pytest.raises(PrinterError) as caught:
        await capture(fake, deadline_s=0.25)
    assert caught.value.code == "not_reachable"
    assert caught.value.details["reason"] == "camera_timeout"
    assert fake.closed == 1 and fake.exited == 0     # nothing to release: it never became a response
    assert not fake.body_read


async def test_a_client_that_stalls_mid_read_is_reported_once_and_released():
    fake = FakeStreamHttp(chunks=[GOOD[:20]], error=httpx.ReadTimeout("stalled"), error_at=0)
    with pytest.raises(PrinterError) as caught:
        await capture(fake)
    assert caught.value.details["reason"] == "stream_stalled"
    assert fake.closed == 1 and fake.exited == 1
    assert "stalled" not in caught.value.message      # the device's own sentence never rides along


async def test_a_connection_that_never_opens_is_not_reachable_and_is_never_retried():
    fake = FakeStreamHttp(enter_error=httpx.ConnectError("refused"))
    with pytest.raises(PrinterError) as caught:
        await capture(fake)
    assert caught.value.code == "not_reachable"
    assert caught.value.details["reason"] == "camera_unreachable"
    assert (fake.entered, fake.exited, fake.closed) == (1, 0, 1)   # one attempt, nothing to release


async def test_a_mid_stream_failure_names_the_bytes_it_had_and_keeps_none_of_them():
    fake = FakeStreamHttp(chunks=[b"\xff\xd8\xff\xdb", b"\x00\x04\x00"], error=httpx.ReadError("reset"),
                          error_at=1)
    with pytest.raises(PrinterError) as caught:
        await capture(fake)
    assert caught.value.details["reason"] == "stream_failed"
    assert caught.value.details["bytes_consumed"] == 7
    assert fake.closed == 1


async def test_a_stream_that_ends_by_itself_without_a_frame_is_the_device_s_own_answer():
    fake = FakeStreamHttp(chunks=[b"\x00" * 50])
    with pytest.raises(PrinterError) as caught:
        await capture(fake)
    assert caught.value.details["reason"] == "stream_closed_early"
    assert caught.value.details["requests_sent"] == 1
    assert (fake.entered, fake.closed) == (1, 1)


# --------------------------------------------------------------------------- the status, and no redirect


@pytest.mark.parametrize("status, code, reason", [
    (404, "protocol_error", "camera_status"), (500, "protocol_error", "camera_status"),
    (200, None, None),
])
async def test_only_200_is_a_stream_and_every_other_status_keeps_its_number(status, code, reason):
    fake = FakeStreamHttp(status=status, chunks=[GOOD])
    if code is None:
        assert (await capture(fake)).http_status == 200
        return
    with pytest.raises(PrinterError) as caught:
        await capture(fake)
    assert caught.value.code == code and caught.value.details["reason"] == reason
    assert caught.value.details["http_status"] == status
    assert fake.closed == 1 and fake.entered == 1


async def test_a_redirect_is_a_refusal_not_a_hop_and_the_body_is_never_read():
    """A 3xx from the camera names an address this server's LAN policy never judged. Following it would be
    the rebinding door again, with a Location header instead of a name."""
    fake = FakeStreamHttp(status=302, headers={"location": "http://198.51.100.99/pivot"}, chunks=[GOOD])
    with pytest.raises(PrinterError) as caught:
        await capture(fake)
    assert caught.value.details["reason"] == "camera_status"
    assert caught.value.details["http_status"] == 302
    assert "198.51.100.99" not in caught.value.message + json.dumps(caught.value.details, default=str)
    assert fake.entered == 1 and fake.closed == 1


async def test_a_camera_that_asks_for_a_login_gets_no_credential_and_no_retry():
    fake = FakeStreamHttp(status=401, chunks=[GOOD])
    with pytest.raises(PrinterError) as caught:
        await capture(fake)
    assert caught.value.code == "auth_rejected"
    assert caught.value.details["reason"] == "camera_auth"
    assert "no credential" in caught.value.message
    assert fake.entered == 1


async def test_a_non_200_reads_nothing_from_the_reply():
    fake = FakeStreamHttp(status=503, chunks=[GOOD])
    with pytest.raises(PrinterError):
        await capture(fake)
    assert fake.body_read is False


# --------------------------------------------------------------------------- the caps


async def test_a_frame_bigger_than_one_image_reply_may_hold_is_refused_with_its_size_and_never_cut():
    big = jpeg(pad_to=6000)
    fake = FakeStreamHttp(chunks=[big])
    with pytest.raises(PrinterError) as caught:
        await capture(fake, max_frame_bytes=2048)
    assert caught.value.details["reason"] == "snapshot_too_large"
    assert caught.value.details["max_bytes"] == 2048
    assert caught.value.details["bytes"] > 2048
    assert str(len(big)) in caught.value.message       # the number named, not a truncated picture handed back
    assert fake.closed == 1


async def test_a_frame_smaller_than_a_picture_can_be_is_refused_as_a_marker_pair():
    tiny = jpeg(body=b"\x11\x22\x33")                  # a whole, structurally real, useless frame
    assert snapp._MIN_FRAME_BYTES > len(tiny)
    fake = FakeStreamHttp(chunks=[tiny])
    with pytest.raises(PrinterError) as caught:
        await capture(fake)
    assert caught.value.details["reason"] == "frame_too_small"
    assert caught.value.details["minimum_bytes"] == snapp._MIN_FRAME_BYTES


async def test_reading_forever_without_completing_a_frame_hits_the_read_ceiling_and_stops():
    """A device that streams and streams must not be able to make this server hold memory until it is
    killed: the ceiling is a number, it is named, and the read stops at it."""
    fake = FakeStreamHttp(endless=True, trickle=b"\x00" * 4096)
    with pytest.raises(PrinterError) as caught:
        await capture(fake, max_bytes=4096, deadline_s=2.0)
    assert caught.value.details["reason"] == "stream_cap_reached"
    assert caught.value.details["max_bytes"] == 4096
    assert caught.value.details["bytes_consumed"] >= 4096
    assert fake.closed == 1


async def test_a_whole_frame_inside_a_chunk_that_breaches_the_ceiling_is_refused_and_not_kept():
    """The reproduction of the ceiling that used to be checked **after** a frame was accepted. The stream
    delivers one oversized chunk whose first bytes are a complete, keepable frame; the hunt's own ceiling
    sits below the chunk, so the read is over budget the moment that chunk arrives. The frame is in there —
    the parser finds it, which is exactly what the old ordering did, handing it back as a picture from a
    hunt that had already passed the number this module names — and it must now be refused with the ceiling
    named, nothing kept, and the stream released once."""
    chunk = GOOD + b"\x00" * 4096
    assert snapp.find_jpeg_frame(bytearray(chunk)) is not None      # the frame IS inside the breach
    cap = 1500
    assert cap < len(chunk) and cap > snapp._MIN_FRAME_BYTES
    fake = FakeStreamHttp(chunks=[chunk])
    with pytest.raises(PrinterError) as caught:
        await capture(fake, max_bytes=cap)
    details = caught.value.details
    assert details["reason"] == "stream_cap_reached"
    assert details["max_bytes"] == cap
    assert details["bytes_consumed"] > cap                          # the breach is what it says it is
    assert str(cap) in caught.value.message                         # the number, not a shrunk picture
    assert fake.closed == 1 and fake.entered == 1                   # one request, one release, no second try


async def test_the_hunt_stops_at_its_ceiling_rather_than_at_the_next_whole_buffer():
    """Because the ceiling is counted before a chunk is appended, the bytes this server ever holds are the
    bytes it named plus at most one chunk: a stream of small chunks that never completes a frame is stopped
    within one chunk of the ceiling, which is what makes the ceiling a memory bound and not only a
    refusal."""
    chunk = b"\x00" * 512
    cap = 1500
    fake = FakeStreamHttp(chunks=[chunk] * 40)
    with pytest.raises(PrinterError) as caught:
        await capture(fake, max_bytes=cap)
    details = caught.value.details
    assert details["reason"] == "stream_cap_reached"
    assert cap < details["bytes_consumed"] <= cap + len(chunk)
    assert fake.closed == 1


async def test_a_multi_scan_frame_comes_back_whole_however_the_stream_is_split():
    """The multi-scan fix, end to end: a progressive frame arriving in small pieces over a live stream is
    the picture the camera sent, byte for byte, and not the head of it up to the end-of-image lookalike
    carried inside the DHT between its scans."""
    multi = multi_scan_jpeg(scans=3, restarts=True)
    chunks = [PART_HEAD] + [multi[i:i + 29] for i in range(0, len(multi), 29)]
    fake = FakeStreamHttp(chunks=chunks)
    shot = await capture(fake)
    assert shot.data == multi and shot.kind == "jpeg"
    assert shot.bytes_skipped == len(PART_HEAD)
    assert (fake.entered, fake.exited, fake.closed) == (1, 1, 1)


async def test_a_junk_stream_drops_what_cannot_begin_a_frame_instead_of_hoarding_it():
    """The buffer holds at most one byte while nothing in it can start a frame, so the ceiling above is a
    refusal threshold and not a memory plan. Proven behaviourally: the hunt survives a stream that puts a
    start-of-image across a chunk boundary after a megabyte of nothing."""
    split = b"\xff" + b"\xd8"                           # SOI arriving in two bytes
    fake = FakeStreamHttp(endless=False, chunks=[b"\x00" * 65536, split, GOOD[2:], b""])
    shot = await capture(fake, max_bytes=4 * 1024 * 1024)
    assert shot.data == GOOD
    assert shot.bytes_skipped == 65536


async def test_a_stream_that_sends_text_instead_of_bytes_keeps_nothing():
    fake = FakeStreamHttp(chunks=["not bytes"])
    with pytest.raises(PrinterError) as caught:
        await capture(fake)
    assert caught.value.details["reason"] == "stream_shape"
    assert fake.closed == 1


# --------------------------------------------------------------------------- the release, on every path


@pytest.mark.parametrize("build, reason", [
    (lambda: (FakeStreamHttp(chunks=[GOOD]), {}), None),
    (lambda: (FakeStreamHttp(status=404), {}), "camera_status"),
    (lambda: (FakeStreamHttp(chunks=[GOOD[:20]], error=httpx.ReadError("reset"), error_at=0), {}),
     "stream_failed"),
    (lambda: (FakeStreamHttp(chunks=[b"\x00"], endless=True, trickle=b"\x11"), {"deadline_s": 0.2}),
     "no_frame_in_window"),
    (lambda: (FakeStreamHttp(chunks=[b"\xff\xd8\xff\xdb", b"\x00\x04\x00"]), {}), "stream_closed_early"),
    (lambda: (FakeStreamHttp(chunks=["text"]), {}), "stream_shape"),
    (lambda: (FakeStreamHttp(chunks=[jpeg(body=b"\x11\x22\x33")]), {}), "frame_too_small"),
    (lambda: (FakeStreamHttp(hang_on_enter=True), {"deadline_s": 0.2}), "camera_timeout"),
    (lambda: (FakeStreamHttp(enter_error=httpx.ConnectError("refused")), {}), "camera_unreachable"),
    (lambda: (FakeStreamHttp(status=302, headers={"location": "http://198.51.100.99/x"}), {}),
     "camera_status"),
])
async def test_the_stream_is_released_exactly_once_on_every_path(build, reason):
    """One close per call, whether the picture arrived, the device said no, the bytes stopped, the window
    ran out, or the frame was not keepable. A stream left open is a viewer the printer still counts."""
    fake, kwargs = build()
    with pytest.raises(PrinterError) if reason else _nothing():
        await capture(fake, **kwargs)
    assert fake.closed == 1, reason


class _nothing:
    """An `else` branch for the one path above that succeeds instead of refusing."""

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


async def test_a_close_that_fails_does_not_replace_the_refusal_it_follows():
    fake = FakeStreamHttp(status=503, close_error=httpx.ReadError("close failed"))
    with pytest.raises(PrinterError) as caught:
        await capture(fake)
    assert caught.value.details["reason"] == "camera_status"


async def test_a_caller_cancelled_mid_read_still_gives_the_printer_its_camera_back():
    """The requirement an ``async with`` does not cover on its own: the close must happen even when the
    cancellation arrives while the read is parked. A stream that is never released is a viewer slot the
    printer thinks somebody is still watching."""
    fake = FakeStreamHttp(chunks=[GOOD[:20], b"\x00" * 4096], hang_at=1)
    task = asyncio.create_task(capture(fake))
    while fake.entered == 0:
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.02)                  # parked inside the read of chunk index 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.closed == 1 and fake.exited == 1


# --------------------------------------------------------------------------- what this module never speaks


async def test_no_network_is_touched_by_this_module_even_with_the_real_seam_absent(monkeypatch, no_network):
    """With a literal address configured the whole capture is: read the config, judge the literal, hand one
    URL to the seam. Nothing resolves, nothing connects, nothing is looked up — and the picture still
    arrives, which is what makes the zero a measurement."""
    fake = FakeStreamHttp(chunks=[GOOD])
    monkeypatch.setenv("ORCA_CC2_URL", HOST)
    shot = await snapp.capture_snapshot(http_factory=fake.factory())
    assert shot.data == GOOD
    assert no_network["calls"] == []


def test_the_production_session_is_pinned_to_the_literal_and_honours_nothing_else():
    """The switches on the real client are the security policy, so they are pinned where a default could
    silently change under an httpx upgrade: no environment proxy, no redirect, the stated timeouts, and one
    absolute URL that is the string the policy judged (no base_url, so no merging rules to trust)."""
    client = snapp.Cc2StreamHttp(EXPECTED_URL)
    inner = client._http
    assert client.url == EXPECTED_URL
    assert not str(inner.base_url) or str(inner.base_url) == ""
    assert inner.follow_redirects is False
    assert inner._trust_env is False       # internal on purpose: if httpx stops storing it, this fails loudly
    assert inner.timeout.connect == snapp.CONNECT_TIMEOUT_S
    assert inner.timeout.read == snapp.FRAME_DEADLINE_S
    # There is no door for a credential on this path: the seam takes a URL and nothing else.
    assert "headers" not in inspect.signature(snapp.Cc2StreamHttp.stream).parameters
    assert "auth" not in inspect.signature(snapp.Cc2StreamHttp.stream).parameters


def test_the_module_speaks_no_printer_protocol_and_hardcodes_no_address():
    """This adapter's whole safety claim is that it only ever fetches: it speaks no printer protocol, owns
    no MQTT client, never toggles anything, and contains no machine's address. Scanning its own imports and
    its own text is how that stays true after an edit that seemed harmless — the same style of guard the
    camera candidate's tests keep, aimed the other way now that there is a tool.

    The numbers are deliberately absent from this list of prohibitions: the module docstring names the
    printer's camera-address method and the never-called video method precisely so the reader is told what
    is NOT sent, and what is pinned here is that neither can be reached from this code."""
    source = Path(inspect.getfile(snapp)).read_text(encoding="utf-8")
    imports = [line.strip() for line in source.splitlines()
               if line.strip().startswith(("import ", "from "))]
    forbidden = ("socket", "subprocess", "asyncio.tasks", "multiprocessing", "ctypes", "tempfile",
                 "aiomqtt", "paho", "aiohttp", "requests", "urllib.request", "urllib.error",
                 "cc2_camera", "cc2_control", "cc2_state", "cc2_start")
    for line in imports:
        assert not any(f"import {name}" in line or f"from {name}" in line for name in forbidden), line
    # The only way this module reaches the wire is the seam, and the seam takes a URL and nothing else.
    assert "trust_env=False" in source and "follow_redirects=False" in source
    assert "trust_env=True" not in source and "follow_redirects=True" not in source
    # The toggle word and the method numbers may appear in prose that says they are never sent, so this is
    # checked against the code's own shapes, not the text: no keyword, attribute, dict key or string
    # argument is ever spelled `enable`, and no method number of the printer's protocol is a constant here.
    tree = ast.parse(source)
    names: set[str] = set()
    numbers: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Call):
            names.update(a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str))
        elif isinstance(node, ast.Dict):
            names.update(k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str))
        elif isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
            numbers.add(node.value)
    assert "enable" not in names, sorted(n for n in names if "enabl" in n)
    assert not numbers & {1042, 1054, 1001, 1029, 1045}, sorted(numbers & {1042, 1054, 1001, 1029, 1045})
    assert snapp.CAMERA_PORT == 8080        # the port and path are the sources'; the host is the config's


def test_the_addresses_it_names_are_all_documented_test_ranges():
    """Every address this adapter and these fixtures can use as a *target* is IANA documentation space, and
    neither file names an address on a real network — not the lab's, not any RFC1918 machine's. That is what
    makes "no printer, no network was touched" true of this file rather than merely claimed."""
    source = Path(inspect.getfile(snapp)).read_text(encoding="utf-8") + Path(__file__).read_text(encoding="utf-8")
    for const in (HOST, CONF_HOST):
        assert const.startswith(("192.0.2.", "198.51.100.", "203.0.113.")), const
    for found in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", source):
        assert not found.startswith(("10.", "172.16.", "192.168.", "172.17.", "10.16.", "10.88.",
                                     "10.50.")), found
        ip = ipaddress.ip_address(found)      # nothing internet-routable is named anywhere in here: every
        assert not ip.is_global or ip.is_multicast, found   # address is documentation, loopback, multicast
                                                     # (a refusal fixture), or reserved by IANA


# --------------------------------------------------------------------------- the server layer


@pytest.fixture(autouse=True)
def _camera_configured(monkeypatch):
    """The tool-layer tests below run against a configured printer, because "is a printer configured" is
    the one switch that decides whether a CC2 tool can reach anything. The one test of the unconfigured
    posture clears the variable itself. No access code is ever set here, and none is needed: a camera fetch
    sends no credential, which is one of the things these tests are for."""
    monkeypatch.setenv("ORCA_CC2_URL", HOST)
    monkeypatch.delenv("ORCA_CC2_ACCESS_CODE", raising=False)
    monkeypatch.delenv("ORCA_CC2_ACCESS_CODE_FILE", raising=False)


@pytest.fixture
def no_network(monkeypatch):
    """Anything that could open a socket or resolve a name raises for the whole test and counts its own
    attempts, so 'this module never reached the network' is a measured zero even when the seam is scripted.
    Same guard the camera candidate's tests keep, so the two files mean the same thing by it."""
    calls: list[str] = []

    def trip(what):
        def go(*a, **k):
            calls.append(what)
            raise AssertionError(f"the camera snapshot touched the network: {what}")
        return go

    monkeypatch.setattr(socket.socket, "connect", trip("socket.socket.connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", trip("socket.socket.connect_ex"))
    monkeypatch.setattr(socket.socket, "sendto", trip("socket.socket.sendto"))
    monkeypatch.setattr(socket, "getaddrinfo", trip("socket.getaddrinfo"))
    monkeypatch.setattr(socket, "create_connection", trip("socket.create_connection"))
    monkeypatch.setattr(asyncio, "open_connection", trip("asyncio.open_connection"))
    return {"calls": calls}


async def test_the_tool_returns_an_image_block_a_warning_line_and_its_metadata(monkeypatch):
    """The reply a caller can act on: the picture, then a line that carries the single-viewer warning with
    the size/type/origin sentence, then the metadata the adapter built. Building that metadata and dropping
    it on the way out is the defect these two tests exist for."""
    import orcaslicer_mcp.server as srv
    fake = FakeStreamHttp(chunks=[PART_HEAD, GOOD])
    monkeypatch.setattr(srv, "_cc2_snapshot_http_factory", lambda: fake.factory())
    out = await srv.cc2_camera_snapshot()
    assert isinstance(out, list) and len(out) == 3
    image, summary, metadata = out
    assert image.data == GOOD and image._mime_type == "image/jpeg"
    assert "no printer command" in summary.text.lower()
    assert str(len(GOOD)) in summary.text
    assert "warning" in summary.text.lower() and "single viewer" in summary.text.lower()
    assert metadata.text.startswith("cc2_camera_snapshot metadata (JSON):")
    payload = json.loads(metadata.text.split("metadata (JSON):", 1)[1])
    assert payload["bytes"] == len(GOOD) and payload["image_kind"] == "jpeg"
    assert payload["stream_url"] == EXPECTED_URL
    assert payload["stream_url_source"] == "composed_by_this_server"
    assert payload["command_sent"] is False and payload["camera_method_sent"] is False
    assert payload["mqtt_session_used"] is False
    assert payload["frame_complete"] is True and payload["http_status"] == 200
    assert any("single viewer" in w for w in payload["warnings"])
    assert (fake.entered, fake.exited, fake.closed) == (1, 1, 1)


async def test_what_an_mcp_client_is_actually_handed_is_the_image_and_its_metadata(monkeypatch):
    """The MCP-visible result, not the Python value the tool function returns: the tool is run through the
    server's own tool manager with the conversion the server itself performs, so what is asserted here is
    the content list a client receives — one image block whose base64 decodes to the frame's bytes, and the
    metadata beside it as text rather than in a local variable that goes nowhere. A reply that builds its
    metadata and drops it passes the old assertions and fails these."""
    import base64

    from mcp.types import ImageContent, TextContent

    import orcaslicer_mcp.server as srv
    fake = FakeStreamHttp(chunks=[GOOD])
    monkeypatch.setattr(srv, "_cc2_snapshot_http_factory", lambda: fake.factory())
    result = await srv.mcp._tool_manager.call_tool("cc2_camera_snapshot", {}, context=None,
                                                  convert_result=True)
    blocks = list(result.content)
    assert sum(1 for b in blocks if isinstance(b, ImageContent)) == 1
    assert sum(1 for b in blocks if isinstance(b, TextContent)) == 2
    image, texts = blocks[0], [b.text for b in blocks if isinstance(b, TextContent)]
    assert base64.b64decode(image.data) == GOOD      # the real bytes, on the wire, in an image block
    assert image.mime_type == "image/jpeg"
    joined = "\n".join(texts)
    assert "single viewer" in joined.lower()          # the warning reaches the client too
    assert "composed_by_this_server" in joined
    assert f"{len(GOOD)} bytes" in joined
    assert (fake.entered, fake.exited, fake.closed) == (1, 1, 1)


async def test_the_tool_answers_a_refusal_in_the_shape_every_cc2_tool_uses(monkeypatch):
    import orcaslicer_mcp.server as srv
    fake = FakeStreamHttp(status=503)
    monkeypatch.setattr(srv, "_cc2_snapshot_http_factory", lambda: fake.factory())
    out = await srv.cc2_camera_snapshot()
    assert out["error"] == "protocol_error" and out["reason"] == "camera_status"
    assert "503" in out["message"]


async def test_with_no_configured_address_the_tool_refuses_here_and_contacts_nothing(monkeypatch):
    import orcaslicer_mcp.server as srv
    monkeypatch.delenv("ORCA_CC2_URL", raising=False)   # the autouse fixture configures; this test is the posture without it
    called: list = []
    monkeypatch.setattr(srv, "_cc2_snapshot_http_factory",
                        lambda: (lambda base_url: called.append(base_url) or FakeStreamHttp()))
    out = await srv.cc2_camera_snapshot()
    assert out["error"] == "not_configured" and out["printer_contacted"] is False
    assert called == []


async def test_every_key_the_snapshot_answers_with_is_a_name_the_server_declared(monkeypatch):
    """The completeness check that keeps the reply sanitiser from rewriting this tool's own contract: a
    configured access code of "e" is a substring of nearly every key here, so any name missing from
    ``_CC2_REPLY_KEYS`` arrives mangled and fails this instead of reaching a client that way."""
    import orcaslicer_mcp.server as srv
    fake = FakeStreamHttp(chunks=[GOOD])
    shot = await capture(fake)
    out = srv._cc2_out(shot.as_dict(), _code="e")
    keys = set(_walk_keys(out))
    assert all("<redacted>" not in key for key in keys), sorted(keys - srv._CC2_REPLY_KEYS)
    assert {"stream_url", "bytes_consumed", "camera_method_sent", "read_window_s", "elapsed_s",
            "frame_complete", "http_status", "content_type"} <= keys


def _walk_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def test_the_source_writes_no_reply_key_the_exemption_list_does_not_name():
    """Same rule the server's own test enforces for the other adapters, run over this file: every
    identifier-shaped name in a dictionary literal in ``cc2_snapshot.py`` has to be a declared name."""
    import orcaslicer_mcp.server as srv
    tree = ast.parse(Path(inspect.getfile(snapp)).read_text(encoding="utf-8"))
    written: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str) \
                        and re.fullmatch(r"[a-z][a-z0-9_]{1,40}", key.value):
                    written.add(key.value)
    assert written <= srv._CC2_REPLY_KEYS, sorted(written - srv._CC2_REPLY_KEYS)


def test_the_snapshot_is_a_registered_read_only_cc2_tool():
    import orcaslicer_mcp.server as srv
    assert "cc2_camera_snapshot" in srv.CC2_TOOL_NAMES
    assert srv._TOOL_ANNOTATIONS["cc2_camera_snapshot"][1] is True
    assert "cc2_camera_snapshot" in srv._OPEN_WORLD_TOOLS
    tools = srv.mcp._tool_manager._tools
    assert tools["cc2_camera_snapshot"].annotations.read_only_hint is True


@pytest.mark.parametrize("extra", [0, 1])
def test_multiscan_split_segment_length_waits_for_more_bytes(extra):
    frame = multi_scan_jpeg(scans=3)
    marker = frame.index(b"\xff\xdd")
    assert snapp.find_jpeg_frame(frame[:marker + 2 + extra]) is None
    assert snapp.find_jpeg_frame(frame) is not None
