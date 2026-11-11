"""The seventeen direct CC2 tools exactly as the server wires them, against scripted fakes: the
adapters, the reply sanitiser, and what happens before a printer is ever configured. The seventeen are
declared the way every other tool on this server is, so the default posture is proven first: with no
ORCA_CC2_URL set — the fixture clears it, every credential variable and every receipt store for the
whole module — each tool answers not_configured with printer_contacted=false, and NOTHING touches the
network. That is a counted fact, not a hope: the fixture guards socket primitives, name lookups, the
MQTT link factory and httpx for the whole module and counts its own hits, and the tests add a
stronger claim by forbidding the transport's own open, which proves the refusal happens at the config
check, before a session is even attempted. An address carrying a login and a public address are
refused the same way, before any socket, and neither one echoes a credential back.

With a scripted fake session standing in for a
live printer, the tools run end to end against fake transports (the observed 1002 idle / printing, the
observed four-tray 2005 where status 2 is active, the 1044 listing shapes), a fake chunk sender,
and a temp receipt store: parameter forwarding is proven on the recorded wire (the one 1020 with
printer_check explicit both ways, the tool-to-tray map, the chunk headers and X-Token), the
upload then start chain runs through the real adapters and the private receipt, accepted and
observed_running stay separate facts, and a lost 1044 reply comes back as an error, never a partial
list. Replies are asserted secret-free (the simulated token, the simulated access code, the local
path never appear in any output) and sanitised (control characters from a printer reply are
stripped, oversized strings capped, non-finite numbers and unserialisable objects turned to None).
The three job controls are proven in both phases: an unconfirmed call reads the printer once (1002,
read-only) and never sends a control method, and a confirmed call sends its one command exactly once
with accepted, refused and ambiguous kept strictly apart — including the resume whose window
expires, which is reported as the known good outcome of a slow resume and never as a refusal. The
two peripheral controls (1029 the light, 1031 the print speed mode) are proven the same way: a
preview sends no command and shows this call's own exact params, a confirmed call sends its one
command once (the light with both parameter spellings paired, the speed with the bare mode number),
an out-of-range mode or a light value that is not exactly true or false refuses before a session is
opened at all, and the light/mode read-back is a separate fact from acceptance — including the case
where the two disagree, which is said out loud without ever earning a second command, and the case
where the printer reported nothing, which stays unknown instead of becoming off or mode 1. The
temperature setpoint (1028) is proven the same way again, and additionally for the one thing a
heater command needs that a lamp does not: the bound a call may ask for is the printer's own official
control's (0-350 the nozzle, 0-110 the bed), so an extruder number above 350 or a bed number above
110 refuses before a session opens, 320 for the bed refuses while 320 for the nozzle does not, the
chamber refuses as the reading-only thing it is, one command always carries exactly one heater, the
reply carries the ceiling with its source beside it, and a setpoint above the 300 the other Elegoo
source names is sent with the disagreement said out loud instead of resolved on a hunch. The CANVAS
auto-refill switch (2004) is proven the same way a third time, and twice over for what makes it
different: it is the one control whose command has never been answered by a real CC2, so a preview and
a confirmed reply each have to carry `verified_on_this_printer: false` and its sentence whole (not cut
by the reply cap), and its read-back is the 2005 `canvas_info` rather than the 1002 status frame, so
the tests script 2005 and hold that an absent flag stays unknown instead of becoming the status
reader's default of off — while the one field on the wire is Elegoo's own client's `auto_refill` and
the older manual's `enable` is proven never to appear in any recorded request. The CANVAS tray
filament write (2003) is proven the same way a fourth time, with one gate the other controls do not
have: the (canvas_id, tray_id) pair must appear in the printer's own 2005 roster, in a unit reporting
itself connected, on both phases — so these tests script 2005 as well, a preview sends [2005] and
changes nothing, a confirmed call sends [2005, 2003, 2005] exactly once with the nine derived fields,
and an unknown material name refuses before a session is even opened, carrying all 50 vendor catalog
names whole because a name outside the catalog is the type-less, code-less, zero-temperature label
the vendor's own lookup would build.
The three information reads (1001 what the printer is, 1046 one file's detail, 1045 one file's
thumbnail) are proven at the tool layer too, and in the same shapes: the wire carries the parameter
spelling that method actually wants (1046 filename, 1045 file_name — they are not the same word), a
reply the printer left blank is reported as silence rather than as a profile of guesses, a file the
printer has no thumbnail for is said in a sentence of its own, a refusal keeps the printer's code, and
the image arrives whole as one MCP image block with a line of text beside it — an image over what one
reply may carry is refused with its size named and never cut down. A file name carrying a path is
refused before a session is opened at all, and no read path sends a method a control owns. The print
history read (1036) is proven at the tool layer as the candidate it is: the wire carries no parameters
and no paging key, a clean read still says it has never been captured from this printer, a list that
had to be cut for size is reported as cut rather than as the whole history, a reply in Elegoo's cloud
shape refuses instead of being read as a LAN answer, and a time-lapse link carrying a login never
leaves the reply.

Fifteen of the seventeen tools share the process's one registered session (printer/cc2_pool.py), because
the printer's own broker takes about four clients and reaps a departed one slowly: proven here at the
layer the calls arrive at — a run of four ordinary calls registers once, two calls at the same moment
wait for one open instead of each registering, a session the transport reported dead is closed and
replaced by the very next call, moving the configured address between calls opens a fresh session and
gives the abandoned one back, an upload after another call still authenticates with the credential
that session was met with, a cancelled call gives its hold back and leaves the session usable by the
next, and the server's own exit closes it (including the exit where stdio failed). The pool's own
corner cases — the idle window, a configuration change while a call is in flight, a double release —
are tests/test_cc2_pool.py, and the real-client proof that one registration answers two requests is
the loopback test in tests/test_cc2_transport_mqtt.py. The one exception, `cc2_camera_snapshot`, borrows no
session by design: its picture is one bounded HTTP GET of the printer's own camera stream, so it spends
none of the printer's client slots — what it shares with the transport is the configuration and the
address pinning, and its own guards and refusal paths are tests/test_cc2_snapshot.py.
Discovery is proven last, in fresh interpreters whose environment the test builds from PATH, HOME and
the variables it names: all seventeen direct tools are listed with no printer config at all, the list is
identical when a printer address is configured, the server's other tools come back identical in
every one of them, and in a child that raises the moment any socket, name lookup, MQTT link or HTTP
request is attempted, every direct tool still answers not_configured. No live printer, no Home
Assistant, no real network, no real credentials — the addresses are documented TEST-NET and every
credential below is a stand-in string."""


from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import http.client
import httpx
import inspect
import json
import os
import re
import socket
import subprocess
import sys
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mcp.server.mcpserver import Image
from mcp.types import TextContent

import orcaslicer_mcp.server as srv
from orcaslicer_mcp.printer import cc2_receipts
from orcaslicer_mcp.printer.errors import PrinterError

# --------------------------------------------------------------------------- stand-ins

HOST = "192.0.2.10"                  # documented TEST-NET; never a live address
SN = "CC2TESTSN0001"
TOKEN = "SIMULATED-NOT-A-TOKEN"      # the password/access-code stand-in, same role as in the adapter tests
CODE = "SIMULATED-CODE-99"           # a stand-in *configured* access code for the redaction test
DEST = "benchy-petg-2026-11-04.gcode"
SHA = "a" * 64
# Everything that could make a direct tool able to reach something real. There is no read/write
# switch any more — the configuration IS the opt-in — so clearing these is what puts a test in the
# default posture, and the autouse cc2_env fixture below does it for every test in the module.
CC2_CONFIG_VARS = ("ORCA_CC2_URL", "ORCA_CC2_ACCESS_CODE", "ORCA_CC2_ACCESS_CODE_FILE",
                   "ORCA_CC2_ACCESS_CODES_FILE", "ORCA_CC2_ACCESS_CODE_ID",
                   "ORCA_CC2_RECEIPTS_DIR")
CC2_NAMES = frozenset(srv.CC2_TOOL_NAMES)
REPO_ROOT = Path(srv.__file__).resolve().parents[2]   # the checkout the child interpreter imports from

# --------------------------------------------------------------------------- network guards


@contextlib.contextmanager
def _guard(group: dict, name: str, factory):
    original = getattr(group, name)

    def hit(*a, **kw):
        group.setdefault("calls", []).append(name)
        raise AssertionError(f"network bypassed the gate: {name} was called")
    return original, factory, hit


@pytest.fixture(autouse=True)
def cc2_env(monkeypatch):
    """No test in this module may inherit (or leak) a real printer address, a real access code or a
    real receipt store — which is also how the default posture, "no printer configured", is what a
    test starts from rather than something it has to remember to arrange."""
    for var in CC2_CONFIG_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Anything that could reach a socket raises for the whole module; the guards count their
    own hits so 'zero network' is a counted fact, not a hope."""
    guards: dict = {"calls": []}

    def trip(name):
        def hit(*a, **kw):
            guards["calls"].append(name)
            raise AssertionError(f"a tool reached the network: {name} was called")
        return hit

    # The three ways this adapter can reach out: the UDP discovery endpoint, the MQTT link, and
    # httpx (both the modern client and the raw connection it would fall back to). The event
    # loop's own self-pipe sockets stay untouched — nothing in the adapter uses them.
    monkeypatch.setattr(socket.socket, "connect", trip("socket.socket.connect"))
    monkeypatch.setattr(socket, "getaddrinfo", trip("socket.getaddrinfo"))
    monkeypatch.setattr(socket, "create_connection", trip("socket.create_connection"))
    monkeypatch.setattr(socket.socket, "connect_ex", trip("socket.socket.connect_ex"))
    monkeypatch.setattr(socket.socket, "sendto", trip("socket.socket.sendto"))
    monkeypatch.setattr(asyncio.BaseEventLoop, "create_datagram_endpoint",
                        trip("loop.create_datagram_endpoint"), raising=False)
    monkeypatch.setattr(httpx.AsyncClient, "request", trip("httpx.AsyncClient.request"))
    monkeypatch.setattr(httpx.AsyncClient, "send", trip("httpx.AsyncClient.send"))
    monkeypatch.setattr(httpx.AsyncClient, "__init__", trip("httpx.AsyncClient.__init__"))
    monkeypatch.setattr(http.client.HTTPConnection, "request",
                        trip("http.client.HTTPConnection.request"), raising=False)
    monkeypatch.setattr(srv._cc2_transport, "_aiomqtt_link", trip("_aiomqtt_link"))
    return guards


@pytest.fixture(autouse=True)
def no_open(monkeypatch):
    """The transport seam itself, counted on the way through to the real one. Counting (rather than
    tripping) is what lets one fixture prove both halves of the unconfigured story: a parameter
    refusal never reaches the seam at all (`no_open == []`), and since the shared-session pool reads
    the configuration itself a `not_configured` refusal stops *there*, one step before the seam, so
    it too answers `no_open == []` — no session was borrowed and none was opened, which is the
    stronger claim. A test that stands a scripted printer in place replaces this wrapper — see
    `open_fake`."""
    opens: list = []
    real = srv._cc2_open_transport

    async def counted(config):
        opens.append("open")
        return await real(config)

    monkeypatch.setattr(srv, "_cc2_open_transport", counted)
    return opens


# --------------------------------------------------------------------------- observed wire frames

IDLE_1002 = {  # the verbatim observed idle frame: machine 1/0, the printer's idle word ""
    "error_code": 0,
    "machine_status": {"exception_status": [], "progress": 0, "status": 1, "sub_status": 0,
                       "sub_status_reason_code": 0},
    "print_status": {"current_layer": 0, "filename": "", "print_duration": 0,
                     "remaining_time_sec": 0, "state": "", "total_duration": 0, "uuid": ""},
    "extruder": {"filament_detect_enable": 1, "filament_detected": 1, "target": 0,
                 "temperature": 21},
    "heater_bed": {"target": 0, "temperature": 16},
}

PRINTING_1002 = {  # the observed live shape: machine 2, lowercase "printing", filename set
    "error_code": 0,
    "machine_status": {"exception_status": [], "progress": 21, "status": 2, "sub_status": 2075,
                       "sub_status_reason_code": 0},
    "print_status": {"current_layer": 51, "filename": DEST, "print_duration": 573,
                     "remaining_time_sec": 2096, "state": "printing", "total_duration": 2669,
                     "uuid": "80baf1df-e748-4717-adf1-b9487f2c8f32"},
    "extruder": {"filament_detect_enable": 1, "filament_detected": 1, "target": 260,
                 "temperature": 259.6},
    "heater_bed": {"target": 70, "temperature": 69.9},
}

# The observed idle frame plus the two peripheral blocks spelled exactly as the protocol doc's
# full-status sample spells them — led.status and gcode_move_inf.speed_mode. Those two blocks are
# doc-derived, not observed by this test lane, which is why they live in their own frame: everything
# a test asserts about the light and the speed mode is a claim about the documented shape.
IDLE_1002_PERIPHERALS = {
    **IDLE_1002,
    "led": {"status": 0},
    "gcode_move_inf": {"x": 0.0, "y": 0.0, "z": 0.0, "e": 0.0, "speed": 0, "speed_mode": 1},
}


# The three information frames, taken from the protocol doc: its 1001 attributes sample, its measured
# 1046 file-detail reply, and the 1045 shape whose thumbnail field carries base64 PNG. The image body
# below is a PNG signature plus filler, which is all the tool layer needs: the adapter's own tests
# (tests/test_cc2_info.py) carry real, openable PNG/JPEG/GIF/WebP bytes, and what is proven here is
# plumbing — that the bytes arrive whole and the picture is never cut to fit a reply.
ATTRS_1001 = {
    "error_code": 0, "hostname": "Carbono", "machine_model": "Centauri Carbon 2", "sn": SN,
    "ip": HOST, "mac": "AA:BB:CC:DD:EE:FF", "protocol_version": "1.0.0", "hardware_version": "1.0",
    "software_version": {"ota_version": "02.01.01.07", "mcu_version": "00.00.00.00",
                         "soc_version": ""},
    "resolution": "1920x1080", "xyz_size": "220x220x250", "network_type": "wifi",
    "usb_connected": False, "camera_connected": True, "remaining_memory": 1073741824,
    "max_video_connections": 1, "video_connections": 0,
}

DETAIL_1046 = {
    "error_code": 0, "filename": DEST, "size": 5712759, "layer": 722, "print_time": 4690,
    "total_filament_used": 24.8, "color_map": [{"color": "0B6283", "name": "PLA", "t": 3}],
    "create_time": 1772820155, "last_print_time": 0, "total_print_times": 0,
}

THUMB_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00\xb0\xf0" * 12

# The 1036 history reply, in the shape Elegoo's own LAN web client reads it (error_code and
# history_task_list, each entry carrying its nine names). No reply has ever been captured from this
# printer, which is the whole reason the tool labels itself a candidate and these tests are scripted.
HISTORY_1036 = {
    "error_code": 0, "total": 2, "history_task_list": [
        {"task_id": "b52af24c-764e-4092-8a50-00e5f8f02b46", "task_name": DEST,
         "begin_time": 1772820155, "end_time": 1772824845, "task_status": 1,
         "time_lapse_video_status": 1, "time_lapse_video_url": f"http://{HOST}/tl.mp4",
         "time_lapse_video_size": 4096, "time_lapse_video_duration": 120},
        {"task_id": "c62be35d-1111-4092-8a50-00e5f8f02b46", "task_name": "cat-plate.gcode",
         "begin_time": 1772740000, "end_time": 1772740600, "task_status": 2,
         "time_lapse_video_status": 0, "time_lapse_video_url": "", "time_lapse_video_size": 0,
         "time_lapse_video_duration": 0}],
}


def thumb_1045(data: bytes = THUMB_BYTES, **over) -> dict:
    return {"error_code": 0, "thumbnail": base64.b64encode(data).decode(), **over}


def _tray(tray_id, status, *, ftype="PETG", brand="ELEGOO", mn=230, mx=260) -> dict:
    return {"tray_id": tray_id, "status": status, "filament_type": ftype, "brand": brand,
            "filament_name": f"{brand} {ftype}".strip(), "filament_color": "00B0F0",
            "min_nozzle_temp": mn, "max_nozzle_temp": mx}


CANVAS = {"error_code": 0, "canvas_info": {  # the observed roster: A4 active (status 2),
    "active_canvas_id": 0, "active_tray_id": 3, "auto_refill": False,
    "canvas_list": [{"canvas_id": 0, "connected": 1, "tray_list": [
        _tray(0, 0, ftype="", brand="", mn=0, mx=0),
        _tray(1, 0, ftype="", brand="", mn=0, mx=0),
        _tray(2, 1, brand="Generic"),      # present (status 1)
        _tray(3, 2, brand="ELEGOO"),
    ]}]}}


def payload(entries, total: int | None = None) -> dict:
    return {"error_code": 0, "total": len(entries) if total is None else total,
            "file_list": [dict(e, type="file") for e in entries]}


def file_entry(name=DEST, size=1234) -> dict:
    return {"filename": name, "size": size, "layer": 240,
            "color_map": [{"t": 0, "color": "#00B0F0", "name": "PETG"}]}


class ScriptedTransport:
    """The registered session as the adapter tests script it: every request is recorded and
    answered from a method -> script map. A callable answers per call, a deque shifts (last
    sticks), exceptions raise as given, and an unscripted method answers {}."""

    def __init__(self, replies=None, *, host=HOST, sn=SN, display="CC2-test"):
        self.host, self.sn, self.display = host, sn, display
        self.replies = dict(replies or {})
        self.requests: list[tuple[int, dict]] = []
        self.closed = False

    async def request(self, method: int, params: dict | None = None, *, timeout_s=None) -> dict:
        self.requests.append((method, copy.deepcopy(params or {})))
        reply = self.replies.get(method, {})
        if callable(reply):
            reply = reply(params or {})
        if inspect.isawaitable(reply):
            # A script may be a coroutine function as well as a plain one: that is how a test holds a
            # reply open (a request still in flight when a caller is cancelled, or an open that two
            # calls arrive at together). Unawaited it is a coroutine, and a coroutine is not an answer.
            reply = await reply
        if isinstance(reply, deque):
            reply = reply.popleft() if len(reply) > 1 else reply[0]
        if isinstance(reply, BaseException):
            raise reply
        return copy.deepcopy(reply)

    async def close(self) -> None:
        self.closed = True

    def methods(self) -> list[int]:
        return [m for m, _ in self.requests]

    def params_for(self, method: int, n: int = 0) -> dict:
        return [p for m, p in self.requests if m == method][n]


class Listing:
    """A per-call 1044 source: hands back snapshots in order (last sticks) and counts calls."""

    def __init__(self, snapshots: list[list[dict]], total: int | None = None):
        self.snapshots = snapshots
        self.total = total       # None: the reply's total matches its own page, entry for entry
        self.calls = 0

    def __call__(self, params: dict) -> dict:
        snap = self.snapshots[min(self.calls, len(self.snapshots) - 1)]
        self.calls += 1
        return payload(snap, total=self.total)


def capped_page(params: dict) -> dict:
    """A simulated capped reply — 100 entries of a 112-file listing, paging ignored — which the
    fail-closed walk must refuse as incomplete after two pages, not pass off as a listing."""
    page = [{"filename": f"filler-{i:03d}.gcode", "size": 10 + i, "type": "file"}
            for i in range(100)]
    return {"error_code": 0, "total": 112, "file_list": page}


class FakeHttp:
    """A chunk sender answering the observed success frame; records every PUT and close."""

    def __init__(self):
        self.puts: list[dict] = []
        self.closes = 0
        self.base_urls: list[str] = []

    def factory(self):
        def make(base_url: str) -> FakeHttp:
            self.base_urls.append(base_url)
            return self
        return make

    async def put_chunk(self, *, body: bytes, headers: dict[str, str]):
        self.puts.append({"body": body, "headers": dict(headers)})
        return srv._cc2_upload.Cc2ChunkResponse(status_code=200, body=b'{"error_code": 0}')

    async def aclose(self) -> None:
        self.closes += 1


# --------------------------------------------------------------------------- fake session seam


def open_fake(monkeypatch, transport, *, password: str = TOKEN):
    """Stand a scripted fake session in place of the live one, counting how often the tools
    reach the seam. `no_open` stays the guard for 'refused before any session'.

    The pool is emptied first, because it is the process's one session: a test that hands the seam a
    second scripted printer means *that* printer to be the one the next call talks to, and a session
    borrowed from an earlier test would silently be what it got instead. `opens` counts registrations
    on the seam, which is how the reuse tests below say 'one session, four calls'."""
    opens: list = []
    srv._CC2_POOL.forget()
    # A scripted session stands in for a *configured* printer, and the pool reads the configuration
    # itself now, one step before the seam: without an address the tools would stop at their own
    # config check and never reach this fake at all. The address is TEST-NET, and the module's
    # `no_network` guards stay counting zero because the seam is patched before anything could use it.
    monkeypatch.setenv(srv._cc2_transport.ENV_URL, HOST)

    async def fake_open(config):
        opens.append("open")
        return transport, password
    monkeypatch.setattr(srv, "_cc2_open_transport", fake_open)
    transport.opens = opens
    return transport


def healthy_status(**overrides) -> ScriptedTransport:
    """Every read answers the way the observed printer answers a startable machine; overrides
    arrive as method_<n> keywords (dict keys are ints on the wire) and replace one script."""
    replies = {srv._cc2_state.METHOD_STATUS: IDLE_1002,
               srv._cc2_state.METHOD_CANVAS: CANVAS,
               srv._cc2_state.METHOD_FILES: Listing([[file_entry()]])}
    for key, value in overrides.items():
        replies[int(key.split("_")[-1])] = value
    return ScriptedTransport(replies)


def gcode_bytes() -> bytes:
    """A single-tool PETG file in the mount slice's real shape: the 140 °C non-extruding preheat
    (M104) before the M109 S260 wait, then the flush purge exactly as all four real CC2 slices write
    it (`M6211 A1 L200 T0 Q260 R260 S260` — and per the published firmware that line's T is a real
    filament switch and its Q, R and S are the three setpoints the purge heats to, all three of which
    the parser must be able to account for), the bed held at 80 by M140 with a later M190 S80 wait,
    and the end-block cooldowns the slicer writes last. Every command is one the upload parser's
    command list carries — the fan stops the way the slice writes it (`M106 S0`, not the `M107` this
    slicer never emits), because a file off that list is refused before a byte is sent."""
    return (
        b"M106 S255\n"
        b"M104 S140\n"
        b"M140 S80\n"
        b"G28\n"
        b"M109 S260\n"
        b"M190 S80\n"
        b"T0\n"
        b"M6211 A1 L200 T0 Q260 R260 S260\n"
        b"G1 X10 Y5 E5 F1200\n"
        b"G1 Z0.2\n"
        b"M106 S0\n"
        b"M104 S0\n"
        b"M140 S0\n"
        b"; CONFIG_BLOCK_START\n"
        b"; filament_type = PETG\n"
        b"; curr_bed_type = High temp plate\n"
        b"; nozzle_temperature = 260\n"
        b"; nozzle_temperature_initial_layer = 260\n"
        b"; hot_plate_temp = 80\n"
        b"; hot_plate_temp_initial_layer = 80\n"
        b"; CONFIG_BLOCK_END\n"
    )


@pytest.fixture
def gcode_file(tmp_path) -> Path:
    path = tmp_path / "benchy-petg.gcode"
    path.write_bytes(gcode_bytes())
    return path


@pytest.fixture
def receipts_dir(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "cc2-receipts"
    monkeypatch.setenv("ORCA_CC2_RECEIPTS_DIR", str(root))
    return root


def make_receipt(**over):
    d = {"printer_sn": SN, "remote_filename": DEST, "reported_size_bytes": 1234,
         "content_sha256": SHA, "sent_bytes": 1234, "material": "PETG",
         "nozzle_first_layer_temp": 260.0, "nozzle_temp": 260.0,
         "bed_first_layer_temp": 80.0, "bed_temp": 80.0,
         "commanded_nozzle_setpoints": (140.0, 260.0), "commanded_bed_setpoints": (80.0,),
         "extrusion_nozzle_temps": (260.0,), "tool_index": 0,
         "uploaded_at": datetime(2026, 11, 4, 1, 47, tzinfo=UTC)}
    d.update(over)
    return cc2_receipts.Cc2UploadReceipt(**d)


# ----------------------------------------------- 1. default posture: honest refusal, zero network

ALL_TOOLS = (
    lambda: srv.cc2_status(),
    lambda: srv.cc2_list_files(),
    lambda: srv.cc2_printer_info(),
    lambda: srv.cc2_file_detail(filename=DEST),
    lambda: srv.cc2_file_thumbnail(filename=DEST),
    lambda: srv.cc2_print_history(),
    lambda: srv.cc2_camera_snapshot(),
    lambda: srv.cc2_upload_gcode(path="/nowhere/x.gcode", destination="x.gcode"),
    lambda: srv.cc2_start_print(filename=DEST, tool_to_tray={"0": "3"}, bed_clear=True),
    lambda: srv.cc2_pause_print(),
    lambda: srv.cc2_pause_print(confirm=True),
    lambda: srv.cc2_resume_print(confirm=True),
    lambda: srv.cc2_stop_print(confirm=True),
    lambda: srv.cc2_set_light(on=True),
    lambda: srv.cc2_set_light(on=False, confirm=True),
    lambda: srv.cc2_set_print_speed(mode=2),
    lambda: srv.cc2_set_print_speed(mode=0, confirm=True),
    lambda: srv.cc2_set_temperature(heater="extruder", target=220),
    lambda: srv.cc2_set_temperature(heater="bed", target=0, confirm=True),
    lambda: srv.cc2_set_auto_refill(auto_refill=True),
    lambda: srv.cc2_set_auto_refill(auto_refill=False, confirm=True),
    lambda: srv.cc2_set_tray_filament(tray_id=3, filament_name="PLA", colour="#00B0F0"),
    lambda: srv.cc2_set_tray_filament(tray_id=3, filament_name="PLA", colour="#00B0F0",
                                      confirm=True),
)


async def test_with_no_printer_address_every_tool_refuses_with_zero_network(no_network, no_open):
    # the whole default posture in one place: seventeen tools a client can see and call, no printer
    # configured, and an honest refusal whose every field is plain JSON and whose every promise holds
    for call in ALL_TOOLS:
        out = await call()
        assert out["error"] == "not_configured", out
        assert out["printer_contacted"] is False, out
        assert "ORCA_CC2_URL" in out["message"], out       # it names what to configure
        assert "ORCA_CC2_URL" in out["hint"], out          # and where to configure it
        assert "ACCESS_CODE" not in json.dumps(out), out   # a missing address never implies a code
        # the reply is short by construction, so no client has to truncate the facts a model needs
        assert len(out["message"]) <= srv._CC2_STR_MAX and len(out["hint"]) <= srv._CC2_STR_MAX, out
        json.dumps(out)
    assert no_network["calls"] == []                 # no socket, no lookup, no MQTT link: zero hits
    assert no_open == []          # the pool's own config check refuses before the seam is reached


async def test_an_unconfigured_tool_refuses_at_the_config_check_before_opening(monkeypatch,
                                                                              no_network, no_open):
    # stronger than 'no socket': the transport's own open is forbidden, so a tool that got as far as
    # trying to open a session — even one that would then have failed at discovery — fails this test.
    # The shared-session pool reads the configuration itself, one step before the seam, so the seam
    # is not reached at all: nothing was borrowed and nothing was opened.
    async def forbidden(*args, **kwargs):
        raise AssertionError("an unconfigured tool must not try to open a transport")
    monkeypatch.setattr(srv._cc2_transport, "open_transport", forbidden)
    for call in ALL_TOOLS:
        assert (await call())["error"] == "not_configured"
    assert no_network["calls"] == [] and no_open == []


@pytest.mark.parametrize("url, why", [
    ("http://someone:SECRETCODE@192.0.2.10", "an address carrying a login"),
    ("http://8.8.8.8", "a public address"),
    ("http://192.0.2.10:8883", "a port the CC2 does not speak"),
    ("   ", "an address that is only whitespace"),
], ids=["login", "public", "port", "blank"])
async def test_an_address_that_cannot_be_the_printer_refuses_before_any_socket(url, why, no_network,
                                                                              no_open, monkeypatch):
    # ordinary address handling, unchanged: what is new is that these are now the tools' own first
    # answer instead of a hidden tool's. Each one is a config error, never a network attempt, and the
    # login form never echoes its userinfo back.
    monkeypatch.setenv("ORCA_CC2_URL", url)
    out = await srv.cc2_status()
    assert out["error"] == "not_configured", (why, out)
    assert out["printer_contacted"] is False, (why, out)
    assert "SECRETCODE" not in json.dumps(out), out
    # Where each refusal happens is worth naming rather than averaged out: a login, a port or a blank
    # address is a configuration fault, so the pool's own config check stops it one step before the
    # seam (`no_open == []`). A public address is not a config fault — it is this adapter's address
    # policy, which lives inside the open, so the seam is reached and refused there. Either way the
    # socket guards above stay at zero: a literal address needs no lookup and no packet.
    reached_the_seam = why == "a public address"
    assert no_network["calls"] == [] and (no_open == ["open"]) is reached_the_seam, why


async def test_a_configured_code_stays_out_of_an_unconfigured_refusal(monkeypatch, tmp_path,
                                                                     no_network, no_open):
    # the redaction belt runs on the failure path too: a code that is configured is never echoed by
    # a refusal that was only ever about a missing address
    code_file = tmp_path / "code"
    code_file.write_text(CODE)
    code_file.chmod(0o600)
    monkeypatch.setenv("ORCA_CC2_ACCESS_CODE_FILE", str(code_file))
    for call in ALL_TOOLS:
        out = await call()
        assert out["error"] == "not_configured"
        assert CODE not in json.dumps(out)
    assert no_network["calls"] == []


# --------------------------------------------------------------------------- 2. gates are independent

async def test_reads_and_writes_are_one_configured_printer_without_a_switch(monkeypatch):
    # what removing the gates did not change: with one printer configured the reads answer, and a
    # write that should not happen is stopped by its own honest check — inside the session, for a
    # documented reason, never by a policy switch. A preview reads and sends nothing, as before.
    transport = open_fake(monkeypatch, healthy_status())
    assert (await srv.cc2_status())["machine"]["state_word"] == "idle"
    assert (await srv.cc2_list_files())["complete"] is True
    assert transport.opens == ["open"]   # two reads, one registration: the session is shared

    started = await srv.cc2_start_print(filename="never-uploaded.gcode", tool_to_tray={"0": "3"},
                                        bed_clear=True)
    assert started["error"] == "protocol_error" and started["reason"] == "receipt_missing"
    assert (await srv.cc2_stop_print())["confirm_required"] is True
    # four configured calls, still one session and one registration. This is the shape that used to
    # cost a slot apiece, which on a broker that takes about four clients is how an idle, healthy
    # printer came to refuse the fourth ordinary call with 'too many clients'.
    assert len(transport.opens) == 1
    assert srv._CC2_POOL.holds == 0
    assert transport.methods().count(srv._cc2_control.METHOD_STOP) == 0


async def test_an_unconfigured_write_never_enters_its_own_adapter(no_open, monkeypatch):
    # the writes are listed now, so what stops them has to be their own checks rather than a switch:
    # with no printer configured the pool's own config check refuses before the seam, so the adapter —
    # and with it the receipt lookup and the credential it would have carried — is never entered at all
    def forbidden(*args, **kwargs):
        raise AssertionError("an unconfigured write must not reach its adapter")
    monkeypatch.setattr(srv._cc2_start, "start_print", forbidden)
    monkeypatch.setattr(srv._cc2_upload, "upload_gcode", forbidden)
    assert (await srv.cc2_upload_gcode(path="/nowhere/x.gcode",
                                       destination=DEST))["error"] == "not_configured"
    assert (await srv.cc2_start_print(filename=DEST, tool_to_tray={"0": "3"},
                                      bed_clear=True))["error"] == "not_configured"
    assert no_open == []


# --------------------------------------------------------------------------- 3. validation before any session

async def test_a_bad_destination_refuses_before_any_session(no_open, monkeypatch):
    out = await srv.cc2_upload_gcode(path="x.gcode", destination="../evil.gcode")
    assert out["error"] == "protocol_error" and out["reason"] == "destination_invalid"
    assert no_open == []


@pytest.mark.parametrize("bad", [{"0": "1", "1": "2", "2": "3", "3": "4", "4": "5"}, {"a": "3"},
                                 {"0": "x"}, {}, {"0": None},
                                 {"0": True}, {"-1": "3"}, "0:3", ["0=3"], None],
                         ids=["five-tools", "text-tool", "text-tray", "empty", "none",
                              "bool", "negative", "text", "list", "missing"])
async def test_a_ambiguous_mapping_refuses_before_any_session(bad, no_open, monkeypatch):
    out = await srv.cc2_start_print(filename=DEST, tool_to_tray=bad, bed_clear=True)
    assert out["error"] == "protocol_error" and out["reason"] == "tool_map"
    assert "Nothing was sent" in out["message"]
    # the refusal teaches the shape it wants, example and all — a caller who guessed at one
    # entry per file reads the two-entry example and sees exactly what a map is
    assert '{"0": "1", "1": "2"}' in out["message"]
    assert no_open == []


def test_cc2_tool_map_reads_one_entry_per_tool_up_to_four():
    # whole numbers and digit strings alike read, spaces and a '#' are tolerated, and one to
    # four entries are a mapping — what the start adapter expects, as ints
    assert srv._cc2_tool_map({"0": "3"}) == {0: 3}
    assert srv._cc2_tool_map({"0": "1", "1": "2"}) == {0: 1, 1: 2}
    assert srv._cc2_tool_map({0: 1, " 1 ": " #2 "}) == {0: 1, 1: 2}
    assert srv._cc2_tool_map({"0": "0", "1": "1", "2": "2", "3": "3"}) == {0: 0, 1: 1, 2: 2, 3: 3}
    for bad, why in (
            ({"0": "1", "1": "2", "2": "3", "3": "4", "4": "5"}, "five entries"),
            ({"a": "3"}, "a text tool index"),
            ({"0": "x"}, "a text tray id"),
            ({}, "no entry at all"),
            ({"0": None}, "a null tray"),
            ({"0": True}, "a boolean"),
            ({"-1": "3"}, "a negative tool"),
            ({"0": "1", " 0 ": "2"}, "two spellings of one tool index"),
            ("0:3", "text, not an object"),
            (["0=3"], "a list"),
            (None, "nothing at all")):
        with pytest.raises(PrinterError) as raised:
            srv._cc2_tool_map(bad)
        assert raised.value.details.get("reason") == "tool_map", why
        assert "Nothing was sent" in raised.value.message, why
        assert '{"0": "1", "1": "2"}' in raised.value.message, why


# --------------------------------------------------------------------------- 4. status and listing, end to end

async def test_status_reports_parsed_state_and_never_a_raw_reply(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status())
    out = await srv.cc2_status()
    assert out["printer"] == {"serial": SN, "display": "CC2-test"}
    assert out["machine"] == {"status": 1, "state_word": "idle"}
    assert out["job"]["state"] == "idle" and out["job"]["state_known"] is True
    assert out["error"] is None
    assert out["ready"] == {"ready_for_job": True}
    assert out["canvas"]["connected"] is True and out["canvas"]["active_tray_id"] == 3
    trays = {t["tray_id"]: t for t in out["canvas"]["trays"]}
    assert trays[3]["state"] == "active" and trays[3]["raw_state"] == 2  # active, never empty
    assert trays[3]["may_print_from_it"] is True and trays[3]["active"] is True
    assert trays[2]["state"] == "present" and trays[2]["may_print_from_it"] is True
    assert trays[0]["state"] == "empty" and trays[0]["may_print_from_it"] is False
    assert trays[3]["material"] == "PETG" and trays[3]["max_nozzle_temp_c"] == 260
    assert trays[0]["color"] == "#00B0F0"  # normalised by the adapter, still one plain word
    json.dumps(out)
    dumped = json.dumps(out)
    assert "sub_status_reason_code" not in dumped  # raw envelope noise never rides along
    assert "bed_mesh" not in dumped
    assert transport.methods() == [srv._cc2_state.METHOD_STATUS, srv._cc2_state.METHOD_CANVAS]
    # what a tool call owes the shared session now that it does not own it: the hold came back, and
    # nothing is left counting it. It is *not* closed — closing per call is the behaviour that ate
    # the printer's four MQTT slots — and when it does close is what tests/test_cc2_pool.py proves.
    assert srv._CC2_POOL.holds == 0


async def test_status_reports_uncertainty_without_guessing(monkeypatch):
    printing = copy.deepcopy(PRINTING_1002)
    open_fake(monkeypatch, healthy_status(method_1002=printing))
    out = await srv.cc2_status()
    assert out["machine"] == {"status": 2, "state_word": "printing"}
    assert out["job"]["filename"] == DEST and out["job"]["state"] == "printing"
    assert out["ready"]["ready_for_job"] is False
    assert DEST in out["ready"]["why_not"]
    json.dumps(out)


async def test_list_files_returns_a_proven_complete_listing_or_fails(monkeypatch):
    open_fake(monkeypatch, healthy_status())
    out = await srv.cc2_list_files()
    assert out["complete"] is True and out["total"] == 1 and out["pages_certified"] == 1
    assert out["files"] == [{"name": DEST, "size_bytes": 1234, "layers": 240, "tools": [0]}]
    json.dumps(out)

    # the modelled capped reply (100 of 112, paging ignored): an incomplete listing comes back as
    # an error naming the numbers, and the overlap of its repeated page is what proves it
    open_fake(monkeypatch, healthy_status(method_1044=capped_page))
    bad = await srv.cc2_list_files()
    assert bad["error"] == "protocol_error"
    assert bad["reason"] == "pagination_overlap"
    assert "files" not in bad  # never a partial list passed off as a listing

    # a listing that does not add up (92 reported, 100 seen) is refused the same way
    over = Listing([[file_entry(name=f"extra-{n}.gcode") for n in range(100)]], total=92)
    open_fake(monkeypatch, healthy_status(method_1044=over))
    bad2 = await srv.cc2_list_files()
    assert bad2["error"] == "protocol_error" and bad2["reason"] == "pagination_mismatch"
    assert "files" not in bad2


def _shelf_names(count: int) -> list[str]:
    return [f"file-{i:03d}.gcode" for i in range(count)]


def _stub_certified_shelf(monkeypatch, count: int, *, pages: int = 3):
    """Stand a certified inventory of `count` plain names behind the transport seam, so a page
    boundary is the only variable a test is looking at. `total` is the printer's own count and the
    entries are exactly that many, which is what a certified inventory means (see cc2_state)."""
    from types import SimpleNamespace
    files = [SimpleNamespace(filename=n, size=123, layers=None, tools=(), created=None,
                             print_seconds=None, filament_grams=None)
             for n in _shelf_names(count)]

    async def inventory(_transport):
        return SimpleNamespace(files=files, storage="local", pages=pages, total=count,
                               count=len(files))
    monkeypatch.setattr(srv._cc2_state, "list_files", inventory)


async def test_file_listing_reports_when_its_reply_is_truncated(monkeypatch):
    _stub_certified_shelf(monkeypatch, 201)
    result = srv._cc2_out(await srv._cc2_files_reply(None))
    assert len(result["files"]) == 200
    assert result["truncated_in_reply"] is True
    # the review's point, pinned: a withheld tail may never be reported as a complete listing, and
    # the reply has to carry the means to fetch what it left out
    assert result["complete"] is False and result["inventory_complete"] is True
    assert result["has_more"] is True and result["next_offset"] == 200
    assert result["total"] == 201 and result["returned"] == 200
    assert "201 certified files are not in this reply" in result["note"]
    assert "offset=200" in result["note"]


async def test_201_certified_names_are_all_retrievable_and_none_of_them_is_hidden(monkeypatch):
    # 201 names against a 200-file reply cap. The first page used to say complete:true and name no
    # way to reach the 201st file; now it says how many it left out and the exact offset that
    # fetches them, and the two pages between them hold every name exactly once.
    _stub_certified_shelf(monkeypatch, 201)
    open_fake(monkeypatch, healthy_status())   # a session exists; the shelf is the stub
    first = srv._cc2_out(await srv.cc2_list_files())
    assert len(first["files"]) == 200, "_cc2_out must never cut a page of its own"
    assert first["complete"] is False and first["inventory_complete"] is True
    assert first["has_more"] is True and first["next_offset"] == 200
    second = srv._cc2_out(await srv.cc2_list_files(offset=first["next_offset"]))
    assert second["returned"] == 1 and second["has_more"] is False
    assert second["next_offset"] is None and "note" not in second
    seen = [f["name"] for f in first["files"]] + [f["name"] for f in second["files"]]
    assert seen == _shelf_names(201) and len(set(seen)) == 201
    json.dumps(first) and json.dumps(second)


async def test_offset_and_limit_walk_a_bigger_shelf_with_no_gap_and_no_repeat(monkeypatch):
    # 460 names at 50 a page: following next_offset reaches every name once and stops by itself
    _stub_certified_shelf(monkeypatch, 460)
    open_fake(monkeypatch, healthy_status())   # a session exists; the shelf is the stub
    seen: list[str] = []
    out = srv._cc2_out(await srv.cc2_list_files(offset=0, limit=50))
    for _ in range(20):
        assert out["limit"] == 50 and out["total"] == 460
        assert len(out["files"]) == out["returned"]
        seen += [f["name"] for f in out["files"]]
        if not out["has_more"]:
            break
        out = srv._cc2_out(await srv.cc2_list_files(offset=out["next_offset"], limit=50))
    else:
        raise AssertionError("the walk never finished")
    assert seen == _shelf_names(460) and len(set(seen)) == 460
    assert out["next_offset"] is None and out["returned"] == 10


async def test_a_small_listing_still_answers_as_one_complete_page(monkeypatch):
    # compatibility for the default caller: no arguments, everything fits, complete is true, and
    # the new keys say so plainly rather than only appearing when something went wrong
    _stub_certified_shelf(monkeypatch, 3)
    open_fake(monkeypatch, healthy_status())   # a session exists; the shelf is the stub
    out = srv._cc2_out(await srv.cc2_list_files())
    assert out["complete"] is True and out["returned"] == 3 and out["total"] == 3
    assert out["has_more"] is False and out["next_offset"] is None
    assert out["truncated_in_reply"] is False and "note" not in out
    assert out["offset"] == 0 and out["limit"] == srv._CC2_FILES_MAX
    assert out["inventory_complete"] is True and out["pages_certified"] == 3


async def test_an_offset_past_the_last_file_answers_empty_and_done(monkeypatch):
    _stub_certified_shelf(monkeypatch, 5)
    open_fake(monkeypatch, healthy_status())   # a session exists; the shelf is the stub
    out = srv._cc2_out(await srv.cc2_list_files(offset=99))
    assert out["files"] == [] and out["returned"] == 0
    assert out["has_more"] is False and out["next_offset"] is None
    assert out["complete"] is False and out["inventory_complete"] is True
    assert out["total"] == 5


async def test_the_walk_the_tool_documents_ends_at_has_more_because_no_page_is_ever_complete(
        monkeypatch):
    """A caller runs the loop this tool's own text describes, so that text has to name a stop that
    exists. `complete` is the claim of the one reply that held the whole listing, so it is false on
    the last page of a paged shelf exactly as on the middle ones, and a loop written against it
    reads the shelf and then asks for the same last page for ever. `has_more` is the stop, in the
    tool's text, in the hint an over-large limit answers with, and in the README."""
    _stub_certified_shelf(monkeypatch, 460)
    open_fake(monkeypatch, healthy_status())   # a session exists; the shelf is the stub
    names, offset, pages = [], 0, 0
    while pages < 10:
        page = srv._cc2_out(await srv.cc2_list_files(offset=offset, limit=200))
        names += [f["name"] for f in page["files"]]
        pages += 1
        if not page["has_more"]:
            break
        offset = page["next_offset"]
    assert names == _shelf_names(460) and pages == 3, "the documented walk must end at has_more"
    last = srv._cc2_out(await srv.cc2_list_files(offset=400, limit=200))
    assert last["returned"] == 60 and last["has_more"] is False and last["next_offset"] is None
    assert last["complete"] is False and last["inventory_complete"] is True

    doc = srv.cc2_list_files.__doc__
    assert "until ``has_more`` is false" in doc and "until ``complete``" not in doc
    over = await srv.cc2_list_files(limit=srv._CC2_FILES_MAX + 1)
    assert over["error"] == "protocol_error"
    assert "until has_more is false" in over["hint"] and "until complete is" not in over["hint"]
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "`has_more` is the paging stop, never `complete`" in readme


def test_the_page_cap_is_the_reply_cap_so_no_page_is_ever_cut_twice():
    # the tool may never offer a page bigger than _cc2_out can carry: 200 is that number, and the
    # equality is the reason a page of 200 arrives as 200 names rather than as 200 minus whatever
    # the sanitiser decided to drop
    assert srv._CC2_LIST_MAX == 200
    assert srv._CC2_FILES_MAX == srv._CC2_LIST_MAX


@pytest.mark.parametrize("kwargs", [
    {"limit": 0}, {"limit": -5}, {"offset": -1}, {"offset": "0"}, {"limit": 1.5},
    {"offset": True}, {"limit": False}, {"limit": srv._CC2_FILES_MAX + 1}, {"offset": 1.0},
], ids=["limit-0", "limit-negative", "offset-negative", "offset-text", "limit-float",
        "offset-bool", "limit-bool", "limit-over-cap", "offset-float"])
async def test_a_bad_page_number_refuses_before_any_session(kwargs, no_open):
    # a wrong page argument is a caller mistake, so it is answered where every caller mistake is
    # answered — in-process, with no address resolved and no socket opened
    out = await srv.cc2_list_files(**kwargs)
    assert out["error"] == "protocol_error" and out["reason"] == "page_bounds"
    assert out["hint"] and no_open == []


async def test_an_over_large_limit_refuses_with_the_cap_and_the_way_to_page(no_open):
    out = await srv.cc2_list_files(limit=500)
    assert out["error"] == "protocol_error" and out["reason"] == "page_bounds"
    assert str(srv._CC2_FILES_MAX) in out["message"]
    assert f"offset={srv._CC2_FILES_MAX}" in out["hint"]   # the hint is the pagination how-to
    assert no_open == []
    # and the same limit as a string is the same refusal, not a coercion
    assert (await srv.cc2_list_files(limit="500"))["reason"] == "page_bounds"


def test_valid_long_filename_is_not_truncated():
    name = "x" * 240 + ".gcode"
    assert srv._cc2_out(name) == name


# ------------------------- the information reads (1001 attributes, 1046 detail, 1045 thumbnail)

async def test_printer_info_reports_the_answer_and_names_the_silence(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status(method_1001=ATTRS_1001))
    out = await srv.cc2_printer_info()
    assert out["printer"] == {"model": "Centauri Carbon 2", "name": "Carbono", "serial": SN}
    assert out["firmware"]["ota_version"] == "02.01.01.07"     # the firmware, where it is readable
    assert out["firmware"]["soc_version"] is None              # the measured empty string: no version
    assert out["not_reported"] == ["software_version.soc_version"]
    assert out["identity"] == {"session_serial": SN, "serial_agree": True}
    assert out["attachments"]["camera_connected"] is True
    assert transport.methods() == [1001]
    assert transport.params_for(1001) == {}                    # the paramless request, as documented
    assert srv._CC2_POOL.holds == 0                            # the hold came back
    dumped = json.dumps(out)
    assert HOST not in dumped and ATTRS_1001["mac"] not in dumped   # no address, no MAC carried on
    assert "raw" not in dumped                                   # and never the reply as it arrived


async def test_printer_info_refuses_a_reply_that_answered_nothing(monkeypatch):
    # a stub reply must not come back as a profile of nulls dressed as a result
    open_fake(monkeypatch, healthy_status(method_1001={"error_code": 0}))
    out = await srv.cc2_printer_info()
    assert out["error"] == "protocol_error" and out["reason"] == "reply_empty"
    assert "printer" not in out and "firmware" not in out


async def test_printer_info_says_so_when_the_reply_names_another_machine(monkeypatch):
    other = dict(ATTRS_1001, sn="CC2OTHERSN00001")
    open_fake(monkeypatch, healthy_status(method_1001=other))
    out = await srv.cc2_printer_info()
    assert out["identity"]["serial_agree"] is False
    assert "may not describe" in out["identity"]["warning"]


async def test_file_detail_sends_the_spelling_1046_wants(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status(method_1046=DETAIL_1046))
    out = await srv.cc2_file_detail(filename=DEST)
    assert transport.methods() == [1046]
    assert transport.params_for(1046) == {"storage_media": "local", "filename": DEST}
    assert out["name"] == DEST and out["name_matches_request"] is True
    assert out["size_bytes"] == 5712759
    assert out["layers"] == 722 and out["layers_from_field"] == "layer"
    assert out["est_print_s"] == 4690 and out["est_filament_g"] == pytest.approx(24.8)
    assert out["never_printed"] is True
    assert out["created"].startswith("2026-")
    json.dumps(out)


async def test_file_detail_says_so_when_the_answer_is_about_another_file(monkeypatch):
    # the printer answered with a filename; if it is not the one asked for, that is said, not smoothed
    other = dict(DETAIL_1046, filename="someone-elses-print.gcode")
    open_fake(monkeypatch, healthy_status(method_1046=other))
    out = await srv.cc2_file_detail(filename=DEST)
    assert out["name"] == "someone-elses-print.gcode" and out["requested"] == DEST
    assert out["name_matches_request"] is False
    assert any("different file" in w for w in out["warnings"])


@pytest.mark.parametrize("body, reason, code", [
    ({"error_code": 0}, "no_thumbnail", None),
    ({"error_code": 1003}, "declined", 1003),
    ({"error_code": 0, "thumbnail": ""}, "no_thumbnail", None),
    ({"error_code": 0, "thumbnail": "not base64 !!!"}, "thumbnail_malformed", None),
], ids=["no-field", "declined-1003", "empty", "not-base64"])
async def test_a_missing_thumbnail_a_refusal_and_a_damaged_reply_are_three_sentences(body, reason,
                                                                                    code, monkeypatch):
    open_fake(monkeypatch, healthy_status(method_1045=body))
    out = await srv.cc2_file_thumbnail(filename=DEST)
    assert out["error"] == "protocol_error" and out["reason"] == reason
    assert (out.get("error_code") == code) and "image" not in out
    if reason == "no_thumbnail":
        assert "cc2_file_detail" in out["hint"]      # it says where the metadata still lives
    if code is not None:
        assert "no thumbnail" not in out["message"]  # a refusal is not relabelled "the file has none"


async def test_file_thumbnail_sends_file_name_and_returns_the_picture_whole(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status(method_1045=thumb_1045()))
    out = await srv.cc2_file_thumbnail(filename=DEST)
    # the wire carries the underscored spelling this method alone wants — 1046 above wants the other
    assert transport.params_for(1045) == {"storage_media": "local", "file_name": DEST}
    assert transport.methods() == [1045]
    picture, caption = out
    assert isinstance(picture, Image)
    content = picture.to_image_content()
    assert content.type == "image" and content.mime_type == "image/png"
    assert base64.b64decode(content.data) == THUMB_BYTES        # whole, byte for byte, nothing cut
    assert isinstance(caption, TextContent) and caption.type == "text"
    assert DEST in caption.text and "png" in caption.text and str(len(THUMB_BYTES)) in caption.text
    assert srv._CC2_POOL.holds == 0                            # the hold came back


async def test_an_image_bigger_than_one_reply_may_carry_is_refused_with_its_size_named(monkeypatch):
    big = "A" * (srv._cc2_info._MAX_THUMBNAIL_CHARS + 1)
    open_fake(monkeypatch, healthy_status(method_1045={"error_code": 0, "thumbnail": big}))
    out = await srv.cc2_file_thumbnail(filename=DEST)
    assert out["error"] == "protocol_error" and out["reason"] == "thumbnail_too_large"
    assert out["max_bytes"] == srv._cc2_info.MAX_THUMBNAIL_BYTES
    assert "truncated" in out["message"]           # refused at full size: a half an image is a corrupt one


@pytest.mark.parametrize("bad_name", ["../secrets.gcode", "dir/" + DEST, " ", "", DEST + "\n",
                                      "x" * 256, "/etc/passwd"],
                         ids=["dotdot", "dir", "blank", "empty", "newline", "long", "absolute"])
async def test_a_file_name_that_is_not_a_plain_name_refuses_before_any_session(bad_name, no_open,
                                                                              monkeypatch):
    # the argument bar holds even with a printer configured — it is an argument bar, not a gate — and
    # what it refuses, it refuses without opening anything, so no path ever reaches a printer
    monkeypatch.setenv("ORCA_CC2_URL", HOST)
    for call in (lambda: srv.cc2_file_detail(filename=bad_name),
                 lambda: srv.cc2_file_thumbnail(filename=bad_name)):
        out = await call()
        assert out["error"] == "protocol_error" and out["reason"] == "name_rejected", bad_name
        assert "cc2_list_files" in out["hint"] and "Nothing was sent" in out["message"]
    assert no_open == []


async def test_a_storage_other_than_local_refuses_without_a_request(monkeypatch):
    # the tools hard-code it today; the adapter refuses the two spellings the sources offer for the
    # volumes nobody has asked this printer about, so the refusal is on the record either way
    with pytest.raises(PrinterError) as exc:
        await srv._cc2_info.fetch_file_thumbnail(ScriptedTransport({}), DEST, storage="u-disk")
    assert exc.value.details["reason"] == "storage_unsupported"


@pytest.mark.parametrize("method", [1001, 1036, 1045, 1046],
                         ids=["attributes", "history", "thumbnail", "detail"])
async def test_an_information_reply_that_never_arrives_is_reported_once_and_never_resent(method,
                                                                                          monkeypatch):
    # a read that went unanswered is a read that went unanswered: the transport's timeout is handed
    # over untouched, the reply is a structured refusal, the session still closes, and the method is
    # on the wire exactly once because asking twice would only hide the fact
    timeout = srv._cc2_transport.Cc2TimeoutError(
        "protocol_error", f"The printer did not answer method {method} within 10s.",
        reason="timeout", ambiguous=True)
    transport = open_fake(monkeypatch, healthy_status(**{f"method_{method}": timeout}))
    call = {1001: srv.cc2_printer_info, 1036: srv.cc2_print_history,
            1045: lambda **kw: srv.cc2_file_thumbnail(**kw),
            1046: lambda **kw: srv.cc2_file_detail(**kw)}[method]
    out = await (call() if method in (1001, 1036) else call(filename=DEST))
    assert out["error"] == "protocol_error" and out["reason"] == "timeout", out
    assert out["ambiguous"] is True                      # the transport's own word, kept
    assert transport.methods().count(method) == 1
    assert srv._CC2_POOL.holds == 0                            # the hold came back, even on a timeout


async def test_the_information_reads_never_send_a_method_a_control_owns(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status(method_1001=ATTRS_1001,
                                                      method_1045=thumb_1045(),
                                                      method_1046=DETAIL_1046,
                                                      method_1036=HISTORY_1036))
    assert "error" not in await srv.cc2_printer_info()
    assert "error" not in await srv.cc2_file_detail(filename=DEST)
    assert "error" not in await srv.cc2_print_history()
    assert isinstance((await srv.cc2_file_thumbnail(filename=DEST))[0], Image)
    assert set(transport.methods()) == {1001, 1036, 1045, 1046}
    # no status re-read, no command, no delete, no history detail (1037, unimplemented) and no history
    # deletion (1038, unimplemented): four reads, one request each, exactly once
    assert not set(transport.methods()) & {1002, 1020, 1021, 1022, 1023, 1029, 1031, 1037, 1038,
                                           1044, 1047, 2005}


# -------------------------------------------- the print history read (1036) — a labelled candidate

async def test_print_history_is_asked_with_no_parameters_at_all(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status(method_1036=HISTORY_1036))
    out = await srv.cc2_print_history()
    assert transport.methods() == [1036]
    assert transport.params_for(1036) == {}      # the parameterless request, as the app sends it
    assert srv._CC2_POOL.holds == 0              # the hold came back
    assert [e["task_name"] for e in out["entries"]] == [DEST, "cat-plate.gcode"]
    assert out["entries"][0]["task_status"] == 1 and out["entries"][0]["task_status_label"] == "complete"
    assert out["entries"][1]["task_status"] == 2 and out["entries"][1]["task_status_label"] == "cancel"
    assert out["entries"][0]["duration_seconds"] == 4690
    assert out["complete"] is True and out["truncated_in_reply"] is False
    json.dumps(out)


async def test_print_history_carries_its_candidate_label_in_every_clean_reply(monkeypatch):
    # the point of this tool is that it has never met this printer's 1036 answer, so every reply it
    # likes about itself has to say so where a model will read it, not only in its docstring
    open_fake(monkeypatch, healthy_status(method_1036=HISTORY_1036))
    out = await srv.cc2_print_history()
    assert out["verified_on_this_printer"] is False
    assert "ever been captured" in out["unverified"]
    assert "Elegoo's own LAN web client" in out["status_labels"]


@pytest.mark.parametrize("body, reason", [
    ({"error_code": 1003}, "declined"),
    ({"error_code": 0}, "no_history_field"),
    ({"error_code": 0, "totalTasks": 4, "taskList": [{"taskId": "x"}]}, "cloud_shape"),
    ({"error_code": 0, "history_task_list": "nope"}, "history_malformed"),
], ids=["declined", "no-field", "cloud-shape", "not-a-list"])
async def test_a_history_reply_this_tool_cannot_read_is_a_refusal_never_an_empty_history(
        body, reason, monkeypatch):
    transport = open_fake(monkeypatch, healthy_status(method_1036=body))
    out = await srv.cc2_print_history()
    assert out["error"] == "protocol_error" and out["reason"] == reason
    assert "entries" not in out          # a refusal is not reported as a list of nothing
    assert transport.methods() == [1036]  # and nothing was asked again to see what else it might say


async def test_a_history_list_cut_for_size_says_so_instead_of_passing_as_the_whole_of_it(monkeypatch):
    many = [dict(HISTORY_1036["history_task_list"][0], task_name=f"print-{i}.gcode")
            for i in range(srv._cc2_history.MAX_HISTORY_ENTRIES + 40)]
    open_fake(monkeypatch, healthy_status(method_1036={"error_code": 0, "total": len(many),
                                                       "history_task_list": many}))
    out = await srv.cc2_print_history()
    assert out["entries_kept"] == srv._cc2_history.MAX_HISTORY_ENTRIES
    assert out["entries_in_reply"] == len(many)
    assert out["truncated_in_reply"] is True and out["complete"] is False
    assert any("not the whole history" in w for w in out["warnings"])
    # the module's own sentence wins because its cap sits under the sanitiser's list cap: no silent
    # drop at 200 rows behind the tool's back
    assert srv._cc2_history.MAX_HISTORY_ENTRIES < srv._CC2_LIST_MAX
    assert len(out["entries"]) == out["entries_kept"]


async def test_a_time_lapse_link_carrying_a_login_is_never_handed_on(monkeypatch):
    entry = dict(HISTORY_1036["history_task_list"][0],
                 time_lapse_video_url=f"http://nathan:{TOKEN}@{HOST}/timelapse.mp4")
    open_fake(monkeypatch, healthy_status(method_1036={"error_code": 0, "total": 1,
                                                       "history_task_list": [entry]}))
    out = await srv.cc2_print_history()
    assert out["entries"][0]["time_lapse_video_url"] is None
    assert "user name" in out["entries"][0]["time_lapse_video_url_withheld"]
    assert TOKEN not in json.dumps(out) and "nathan:" not in json.dumps(out)


@pytest.mark.parametrize("key", ["task_name", "task_id"])
async def test_a_long_history_field_survives_the_sanitiser_or_is_withheld_not_chopped(monkeypatch, key):
    whole = "n" * 255
    entry = dict(HISTORY_1036["history_task_list"][0], **{key: whole})
    open_fake(monkeypatch, healthy_status(method_1036={"error_code": 0, "total": 1,
                                                       "history_task_list": [entry]}))
    out = await srv.cc2_print_history()
    assert out["entries"][0][key] == whole       # the sanitiser caps at 255 too: neither may halve it
    long_entry = dict(entry, **{key: "y" * 900})
    open_fake(monkeypatch, healthy_status(method_1036={"error_code": 0, "total": 1,
                                                       "history_task_list": [long_entry]}))
    cut = await srv.cc2_print_history()
    assert cut["entries"][0][key] is None         # withheld, and the entry beside it still read
    assert any("900 characters" in n for n in cut["entries"][0]["notes"])
    # the 900 characters never reach the reply at all — and crucially not as the sanitiser's own
    # 255-character cut with an ellipsis, which would be a plausible-looking fake name
    assert "\u2026" not in json.dumps(cut)


# ------------------------------------------------------------------ job controls, both phases


async def test_a_job_control_preview_reads_state_once_and_never_sends_a_command(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status())   # 1021/1022/1023 unscripted on purpose:
    out = await srv.cc2_pause_print()
    assert out["confirm_required"] is True and out["action"] == "pause"
    assert out["preview"]["will_send"] == {"method": srv._cc2_control.METHOD_PAUSE, "params": {}}
    assert out["preview"]["waits_up_to_s"] == 60.0
    assert out["preview"]["job_state"] == "idle" and out["preview"]["job_state_known"] is True
    assert transport.methods() == [srv._cc2_state.METHOD_STATUS]
    assert "Nothing was sent to the printer" in out["how"] and "confirm=true" in out["how"]
    json.dumps(out)


async def test_a_truthy_but_non_true_confirm_stays_a_preview(monkeypatch):
    # the confirmation is exact-or-preview, the way bed_clear is exact-or-refusal: text that
    # merely means yes never moves the machine
    transport = open_fake(monkeypatch, healthy_status())
    out = await srv.cc2_stop_print(confirm="true")
    assert out["confirm_required"] is True and out["action"] == "stop"
    assert transport.methods() == [srv._cc2_state.METHOD_STATUS]


async def test_a_confirmed_pause_sends_one_1021_and_keeps_acceptance_apart_from_state(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status(method_1021={"error_code": 0}))
    out = await srv.cc2_pause_print(confirm=True)
    assert out["status"] == "accepted" and out["accepted"] is True
    assert out["method"] == srv._cc2_control.METHOD_PAUSE and out["waited_s"] == 60.0
    assert out["error_code"] is None
    # one command, then exactly the one read-only look the accepted path allows
    assert transport.methods() == [srv._cc2_control.METHOD_PAUSE, srv._cc2_state.METHOD_STATUS]
    assert out["observed_state"] == "idle"   # the fake still answers idle: reported, never spun
    assert "did not refuse" in " ".join(out["notes"])
    json.dumps(out)


async def test_a_lost_resume_is_ambiguous_at_the_tool_layer_and_never_resent(monkeypatch):
    lost = srv._cc2_transport.Cc2TimeoutError("protocol_error", "simulated: never answered",
                                              reason="timeout", ambiguous=True)
    transport = open_fake(monkeypatch, healthy_status(method_1023=lost))
    out = await srv.cc2_resume_print(confirm=True)
    assert out["status"] == "ambiguous" and out["accepted"] is False
    assert transport.methods().count(srv._cc2_control.METHOD_RESUME) == 1
    assert transport.methods().count(srv._cc2_state.METHOD_STATUS) == 1   # the one look only
    joined = " ".join(out["notes"])
    assert "neither a refusal nor a failure" in joined and "122" in joined
    json.dumps(out)


async def test_a_refused_stop_keeps_its_code_and_reads_nothing_more(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status(method_1022={"error_code": 1010}))
    out = await srv.cc2_stop_print(confirm=True)
    assert out["status"] == "refused" and out["error_code"] == 1010 and out["accepted"] is False
    assert transport.methods() == [srv._cc2_control.METHOD_STOP]   # an on-record refusal is final
    assert "no active print" in " ".join(out["notes"])
    json.dumps(out)


async def test_start_rejects_non_boolean_leveling_before_network(monkeypatch, no_open):
    result = await srv.cc2_start_print(filename=DEST, tool_to_tray={"0": "3"},
                                       bed_clear=True, leveling="false")
    assert result["error"]
    assert not no_open


# ----------------------------------------------------- peripheral controls (1029 light, 1031 speed)
#
# The same two phases as the job controls, proven on the same recorded wire. The frames these tests
# script are the observed idle 1002 with the protocol doc's own led.status and
# gcode_move_inf.speed_mode added (IDLE_1002_PERIPHERALS), so what a confirmed call is allowed to
# claim about the light and the mode is a claim about the documented shape, and an absent field is
# tested as the unknown it must stay.

async def test_a_peripheral_preview_reads_state_once_and_never_sends_a_command(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status())    # 1029/1031 unscripted on purpose:
    out = await srv.cc2_set_light(on=True)                  # reaching one would still be recorded
    assert out["confirm_required"] is True and out["action"] == "light"
    p = out["preview"]
    assert p["will_send"] == {"method": 1029, "params": {"power": 1, "brightness": 255}}
    assert p["waits_up_to_s"] == 10.0
    assert transport.methods() == [srv._cc2_state.METHOD_STATUS]
    assert "Nothing was sent to the printer" in out["how"] and "confirm=true" in out["how"]
    json.dumps(out)


async def test_the_speed_preview_shows_the_mode_and_what_the_printer_reports_now(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status(method_1002=IDLE_1002_PERIPHERALS))
    out = await srv.cc2_set_print_speed(mode=3)
    p = out["preview"]
    assert p["will_send"] == {"method": 1031, "params": {"mode": 3}}
    assert p["mode_now"] == 1 and p["mode_name_now"] == "Balanced" and p["mode_is_known"] is True
    assert transport.methods() == [srv._cc2_state.METHOD_STATUS]


async def test_a_truthy_but_non_true_confirm_leaves_a_peripheral_a_preview(monkeypatch):
    # exact-or-preview, the rule the job controls and bed_clear already hold to
    transport = open_fake(monkeypatch, healthy_status(method_1002=IDLE_1002_PERIPHERALS))
    out = await srv.cc2_set_light(on=True, confirm="true")
    assert out["confirm_required"] is True
    assert transport.methods() == [srv._cc2_state.METHOD_STATUS]


async def test_a_confirmed_light_on_sends_one_1029_with_both_spellings_paired(monkeypatch):
    # the CC2's own documentation and its own web client disagree about this parameter, so the
    # command carries both, paired: firmware reading either one hears the same thing
    transport = open_fake(monkeypatch, healthy_status(method_1002=IDLE_1002_PERIPHERALS,
                                                      method_1029={"error_code": 0}))
    out = await srv.cc2_set_light(on=True, confirm=True)
    assert out["status"] == "accepted" and out["accepted"] is True
    assert out["method"] == 1029 and out["sent"] == {"power": 1, "brightness": 255}
    assert out["waited_s"] == 10.0 and out["error_code"] is None
    assert transport.methods() == [1029, srv._cc2_state.METHOD_STATUS]   # one command, one look
    json.dumps(out)


async def test_a_confirmed_light_off_sends_zero_and_zero(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status(method_1002=IDLE_1002_PERIPHERALS,
                                                      method_1029={"error_code": 0}))
    out = await srv.cc2_set_light(on=False, confirm=True)
    assert out["accepted"] is True and out["sent"] == {"power": 0, "brightness": 0}
    assert out["observed_light_on"] is False     # the frame says off: it agrees, nothing to flag
    assert not any("disagreement" in n for n in out["notes"])
    assert transport.methods().count(1029) == 1


async def test_an_accepted_light_the_status_disagrees_with_is_said_at_the_tool_layer(monkeypatch):
    lit = copy.deepcopy(IDLE_1002_PERIPHERALS)
    lit["led"] = {"status": 1}
    # asked off, and the printer's own status afterwards says the light is on
    transport = open_fake(monkeypatch, healthy_status(method_1002=lit,
                                                      method_1029={"error_code": 0}))
    out = await srv.cc2_set_light(on=False, confirm=True)
    assert out["accepted"] is True and out["observed_light_on"] is True
    contra = [n for n in out["notes"] if "disagreement" in n]
    assert len(contra) == 1 and "reports the light on" in contra[0]
    assert transport.methods().count(1029) == 1  # and the disagreement earns no second command


async def test_a_confirmed_speed_sends_the_mode_number_and_waits_ten_seconds(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status(method_1002=IDLE_1002_PERIPHERALS,
                                                      method_1031={"error_code": 0}))
    out = await srv.cc2_set_print_speed(mode=2, confirm=True)
    assert out["status"] == "accepted" and out["sent"] == {"mode": 2}
    assert out["waited_s"] == 10.0
    assert transport.methods().count(1031) == 1 and transport.params_for(1031) == {"mode": 2}
    assert out["observed_speed_mode"] == 1 and out["observed_speed_name"] == "Balanced"
    json.dumps(out)


async def test_a_lost_peripheral_answer_is_ambiguous_at_the_tool_layer_and_never_resent(monkeypatch):
    lost = srv._cc2_transport.Cc2TimeoutError("protocol_error", "simulated: never answered",
                                              reason="timeout", ambiguous=True)
    transport = open_fake(monkeypatch, healthy_status(method_1002=IDLE_1002_PERIPHERALS,
                                                      method_1031=lost))
    out = await srv.cc2_set_print_speed(mode=3, confirm=True)
    assert out["status"] == "ambiguous" and out["accepted"] is False
    assert transport.methods().count(1031) == 1
    assert transport.methods().count(srv._cc2_state.METHOD_STATUS) == 1   # the one look only
    joined = " ".join(out["notes"])
    assert "neither a refusal nor a failure" in joined and "nothing was retried" in joined
    json.dumps(out)


async def test_a_refused_peripheral_keeps_its_code_and_reads_nothing_more(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status(method_1002=IDLE_1002_PERIPHERALS,
                                                      method_1029={"error_code": 1012}))
    out = await srv.cc2_set_light(on=True, confirm=True)
    assert out["status"] == "refused" and out["error_code"] == 1012 and out["accepted"] is False
    assert transport.methods() == [1029]          # an on-record refusal is final
    assert "1012" in " ".join(out["notes"])
    json.dumps(out)


@pytest.mark.parametrize("bad", [4, -1, 45, "fast", 2.5, None, ""],
                         ids=["four", "minus", "percent", "name", "float", "missing", "empty"])
async def test_a_speed_that_is_not_one_of_the_four_modes_refuses_before_any_session(bad, no_open,
                                                                                     monkeypatch):
    # nothing about a refused argument may reach the printer: no session, no method, and the four
    # modes named so the caller can fix the call instead of guessing at it
    out = await srv.cc2_set_print_speed(mode=bad)
    assert out["error"] == "protocol_error" and out["reason"] == "mode_range"
    assert "0 Silent" in out["hint"] and "3 Ludicrous" in out["hint"]
    assert not no_open


@pytest.mark.parametrize("bad", ["on", "off", 1, 0, 255, None],
                         ids=["on", "off", "one", "zero", "brightness", "missing"])
async def test_a_light_value_that_is_not_exactly_true_or_false_refuses_before_any_session(bad,
                                                                                           no_open):
    out = await srv.cc2_set_light(on=bad)
    assert out["error"] == "protocol_error" and out["reason"] == "light_argument"
    assert not no_open


async def test_status_reports_the_light_and_the_speed_mode_the_printer_states(monkeypatch):
    open_fake(monkeypatch, healthy_status(method_1002=IDLE_1002_PERIPHERALS))
    out = await srv.cc2_status()
    assert out["peripherals"] == {"light_on": False, "led_status": 0, "speed_mode": 1,
                                  "speed_name": "Balanced"}
    json.dumps(out)


async def test_status_leaves_unreported_peripherals_null_instead_of_picking_a_value(monkeypatch):
    # the observed idle frame carries no led block and no speed_mode at all: the read-back stays
    # unknown rather than becoming the friendly default (off, or the doc's 'normal' mode 1)
    open_fake(monkeypatch, healthy_status())
    out = await srv.cc2_status()
    assert out["peripherals"] == {"light_on": None, "led_status": None, "speed_mode": None,
                                  "speed_name": None}
    json.dumps(out)


# ------------------------------------------------ temperature setpoints (1028 extruder / heater_bed)
#
# The same two phases again, plus the one thing a heater command needs that a lamp does not: a
# ceiling. The bounds these tests hold the tool to are the printer's own official control's (nozzle
# 0-350, bed 0-110, from Elegoo's LAN web client release 20260625), so what is proven here is that
# the tool enforces a sourced bound, says where it came from, refuses above it before a session is
# opened, and never lets a caller raise it. The frames scripted are the observed idle 1002 with its
# own extruder / heater_bed blocks (a temperature plus a target), with the target set to whatever a
# test needs.

WARM_1002 = {**IDLE_1002, "extruder": {**IDLE_1002["extruder"], "target": 220,
                                        "temperature": 212.4}}


async def test_a_temperature_preview_reads_state_once_and_never_sends_a_command(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status())     # 1028 unscripted on purpose
    out = await srv.cc2_set_temperature(heater="extruder", target=220)
    assert out["confirm_required"] is True and out["action"] == "extruder_temperature"
    p = out["preview"]
    assert p["will_send"] == {"method": 1028, "params": {"extruder": 220}}
    assert p["waits_up_to_s"] == 10.0 and p["ceiling_c"] == 350.0
    assert p["actual_now_c"] == 21 and p["target_now_c"] == 0 and p["heater_is_known"] is True
    assert transport.methods() == [srv._cc2_state.METHOD_STATUS]
    assert "Nothing was sent to the printer" in out["how"] and "confirm=true" in out["how"]
    json.dumps(out)


async def test_the_temperature_preview_names_the_ceiling_and_its_source(monkeypatch):
    # a person deciding whether to confirm needs to know whose number the ceiling is
    open_fake(monkeypatch, healthy_status())
    out = await srv.cc2_set_temperature(heater="heater_bed", target=100)
    joined = " ".join(out["notes"])
    assert "110°C for the bed" in joined
    assert "LAN web client release 20260625" in joined and "a hardware rating" in joined
    assert out["preview"]["limit_basis"]
    json.dumps(out)


async def test_a_truthy_but_non_true_confirm_leaves_a_setpoint_a_preview(monkeypatch):
    # exact-or-preview, the rule the job controls, the peripherals and bed_clear already hold to
    transport = open_fake(monkeypatch, healthy_status(method_1002=WARM_1002))
    out = await srv.cc2_set_temperature(heater="extruder", target=220, confirm="true")
    assert out["confirm_required"] is True
    assert transport.methods() == [srv._cc2_state.METHOD_STATUS]


async def test_a_confirmed_extruder_setpoint_sends_one_1028_with_that_heater_alone(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status(method_1002=WARM_1002,
                                                      method_1028={"error_code": 0}))
    out = await srv.cc2_set_temperature(heater="extruder", target=220, confirm=True)
    assert out["status"] == "accepted" and out["accepted"] is True
    assert out["heater"] == "extruder" and out["method"] == 1028
    assert out["sent"] == {"extruder": 220}      # whole numbers go out whole
    assert out["waited_s"] == 10.0 and out["error_code"] is None
    assert out["ceiling_c"] == 350.0 and "hardware rating" in out["limit_basis"]
    assert out["observed_target_c"] == 220 and out["observed_actual_c"] == 212.4
    assert transport.methods() == [1028, srv._cc2_state.METHOD_STATUS]   # one command, one look
    assert transport.params_for(1028) == {"extruder": 220}
    json.dumps(out)


async def test_the_bed_label_reaches_the_wire_as_the_protocol_spells_it(monkeypatch):
    # 'bed' is the word a person reads and the word the maintainer's companion uses; the wire is
    # heater_bed, and the bed's own ceiling (110) is what the reply carries
    transport = open_fake(monkeypatch, healthy_status(method_1028={"error_code": 0}))
    out = await srv.cc2_set_temperature(heater="bed", target=60, confirm=True)
    assert out["accepted"] is True and out["sent"] == {"heater_bed": 60}
    assert out["heater"] == "heater_bed" and out["ceiling_c"] == 110.0
    assert transport.methods().count(1028) == 1
    json.dumps(out)


async def test_a_zero_setpoint_says_the_heater_turns_off(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status(method_1028={"error_code": 0}))
    out = await srv.cc2_set_temperature(heater="extruder", target=0, confirm=True)
    assert out["accepted"] is True and out["sent"] == {"extruder": 0}
    assert any("heater is off" in n for n in out["notes"])
    json.dumps(out)


async def test_an_accepted_setpoint_the_reported_target_disagrees_with_is_said_at_the_tool_layer(
        monkeypatch):
    # the idle frame reports target 0 after the printer accepted 220: both facts, said plainly, and
    # the disagreement earns no second command
    transport = open_fake(monkeypatch, healthy_status(method_1028={"error_code": 0}))
    out = await srv.cc2_set_temperature(heater="extruder", target=220, confirm=True)
    assert out["accepted"] is True and out["observed_target_c"] == 0
    contra = [n for n in out["notes"] if "disagreement" in n]
    assert len(contra) == 1 and "accepted 220°C" in contra[0] and "reports the target as 0°C" in contra[0]
    assert transport.methods().count(1028) == 1
    json.dumps(out)


async def test_a_setpoint_above_the_other_sources_300_is_sent_with_the_disagreement_said(
        monkeypatch):
    # the printer's own control reaches this high, so the call goes; the disagreement between the two
    # Elegoo sources is reported rather than decided on a hunch
    warm = {**IDLE_1002, "extruder": {**IDLE_1002["extruder"], "target": 320,
                                       "temperature": 300.0}}
    transport = open_fake(monkeypatch, healthy_status(method_1002=warm,
                                                      method_1028={"error_code": 0}))
    out = await srv.cc2_set_temperature(heater="extruder", target=320, confirm=True)
    assert out["accepted"] is True and out["sent"] == {"extruder": 320}
    assert any("two Elegoo sources disagree" in n for n in out["notes"])
    assert transport.methods().count(1028) == 1
    json.dumps(out)


async def test_a_lost_temperature_answer_is_ambiguous_at_the_tool_layer_and_never_resent(monkeypatch):
    lost = srv._cc2_transport.Cc2TimeoutError("protocol_error", "simulated: never answered",
                                              reason="timeout", ambiguous=True)
    transport = open_fake(monkeypatch, healthy_status(method_1002=WARM_1002, method_1028=lost))
    out = await srv.cc2_set_temperature(heater="extruder", target=220, confirm=True)
    assert out["status"] == "ambiguous" and out["accepted"] is False
    assert transport.methods().count(1028) == 1
    assert transport.methods().count(srv._cc2_state.METHOD_STATUS) == 1   # the one look only
    joined = " ".join(out["notes"])
    assert "neither a refusal nor a failure" in joined and "nothing was retried" in joined
    json.dumps(out)


async def test_a_refused_setpoint_keeps_its_code_and_reads_nothing_more(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status(method_1028={"error_code": 1012}))
    out = await srv.cc2_set_temperature(heater="heater_bed", target=60, confirm=True)
    assert out["status"] == "refused" and out["error_code"] == 1012 and out["accepted"] is False
    assert transport.methods() == [1028]          # an on-record refusal is final
    assert "1012" in " ".join(out["notes"])
    json.dumps(out)


@pytest.mark.parametrize("heater, target, reason", [
    ("chamber", 60, "heater_name"),
    ("fan", 60, "heater_name"),
    ("", 60, "heater_name"),
    ("extruder", 351, "temp_range"),
    ("extruder", -1, "temp_range"),
    ("bed", 320, "temp_range"),        # legal for the nozzle, illegal for the bed: two ceilings
    ("bed", 110.5, "temp_range"),
    ("extruder", "hot", "temp_value"),
    ("extruder", True, "temp_value"),
], ids=["chamber", "fan", "no-name", "nozzle-351", "minus", "bed-320", "bed-110.5", "word",
        "bool"])
async def test_a_heater_or_setpoint_that_is_not_accepted_refuses_before_any_session(heater, target,
                                                                                    reason, no_open):
    # nothing about a refused argument may reach the printer: no session, no method — and a refusal
    # that names the bound and where it came from, so the caller can fix the call instead of guessing
    out = await srv.cc2_set_temperature(heater=heater, target=target)
    assert out["error"] == "protocol_error" and out["reason"] == reason
    assert not no_open
    json.dumps(out)


async def test_a_refused_ceiling_says_it_cannot_be_raised_from_here(no_open):
    out = await srv.cc2_set_temperature(heater="extruder", target=400)
    assert out["reason"] == "temp_range"
    assert "cannot be raised from here" in out["hint"]
    assert "LAN web client release 20260625" in out["message"]
    assert not no_open





# ------------------------------------------------- the CANVAS auto-refill switch (2004) — a labelled
# ------------------------------------------------- candidate, proven at the tool layer
#
# The same two phases a third time, and two things the other controls do not have to prove. This
# command has never been answered by a real CC2, so both a preview and a confirmed reply must carry
# `verified_on_this_printer: false` and its sentence *whole* — the sanitiser caps strings at the same
# length it caps notes at, so a caveat that overruns is a caveat cut in half. And its read-back is the
# 2005 `canvas_info`, not the 1002 status frame, which is why the frames below script 2005 (the
# observed four-tray CANVAS roster, whose `auto_refill` is false) and why an absent flag must stay
# unknown rather than become the status reader's default of off.

def canvas_refill(on) -> dict:
    """The observed 2005 reply with the auto-refill flag set to whatever a test needs."""
    frame = copy.deepcopy(CANVAS)
    if on is None:
        frame["canvas_info"].pop("auto_refill")
    else:
        frame["canvas_info"]["auto_refill"] = on
    return frame


async def test_the_auto_refill_preview_reads_the_canvas_once_and_never_sends_the_command(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status())     # 2004 unscripted on purpose:
    out = await srv.cc2_set_auto_refill(auto_refill=True)    # reaching one would still be recorded
    assert out["confirm_required"] is True and out["action"] == "auto_refill"
    p = out["preview"]
    assert p["will_send"] == {"method": 2004, "params": {"auto_refill": True}}
    assert p["waits_up_to_s"] == 10.0
    assert p["auto_refill_now"] is False and p["auto_refill_is_known"] is True
    assert transport.methods() == [srv._cc2_state.METHOD_CANVAS]
    assert out["verified_on_this_printer"] is False
    assert out["unverified"] == srv._cc2_canvas.UNVERIFIED   # the cap leaves it whole
    json.dumps(out)


async def test_a_truthy_but_non_true_confirm_leaves_the_switch_a_preview(monkeypatch):
    # exact-or-preview, the rule the job and peripheral controls already hold to
    transport = open_fake(monkeypatch, healthy_status())
    out = await srv.cc2_set_auto_refill(auto_refill=True, confirm="true")
    assert out["confirm_required"] is True
    assert transport.methods() == [srv._cc2_state.METHOD_CANVAS]


async def test_a_confirmed_switch_sends_one_2004_with_the_one_field_and_enable_never(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status(method_2005=canvas_refill(True),
                                                     method_2004={"error_code": 0}))
    out = await srv.cc2_set_auto_refill(auto_refill=True, confirm=True)
    assert out["status"] == "accepted" and out["accepted"] is True
    assert out["method"] == 2004 and out["sent"] == {"auto_refill": True}
    assert out["waited_s"] == 10.0 and out["error_code"] is None
    assert out["observed_auto_refill"] is True                # the canvas agrees, nothing to flag
    assert out["verified_on_this_printer"] is False           # agreement is not the flag's behaviour
    assert transport.methods() == [2004, srv._cc2_state.METHOD_CANVAS]   # one command, one look
    assert transport.params_for(2004) == {"auto_refill": True}
    # the older manual's spelling is the thing this must never send, in any direction, ever
    assert "enable" not in json.dumps(transport.requests)
    json.dumps(out)


async def test_an_accepted_switch_the_canvas_still_reads_off_is_said_at_the_tool_layer(monkeypatch):
    # the observed roster answers off; the printer accepts the command anyway: two facts, both said,
    # and the disagreement earns no second command
    transport = open_fake(monkeypatch, healthy_status(method_2004={"error_code": 0}))
    out = await srv.cc2_set_auto_refill(auto_refill=True, confirm=True)
    assert out["accepted"] is True and out["observed_auto_refill"] is False
    contra = [n for n in out["notes"] if "disagreement" in n]
    assert len(contra) == 1 and "reports it off" in contra[0]
    assert transport.methods().count(2004) == 1


async def test_an_absent_auto_refill_flag_stays_unknown_at_the_tool_layer(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status(method_2005=canvas_refill(None),
                                                     method_2004={"error_code": 0}))
    out = await srv.cc2_set_auto_refill(auto_refill=True, confirm=True)
    assert out["accepted"] is True
    assert out["observed_auto_refill"] is None      # not the status reader's default of false
    assert out["observed_auto_refill_raw"] is None
    assert any("stays unknown" in n for n in out["notes"])
    assert not any("disagreement" in n for n in out["notes"])


async def test_a_lost_auto_refill_answer_is_ambiguous_at_the_tool_layer_and_never_resent(monkeypatch):
    lost = srv._cc2_transport.Cc2TimeoutError("protocol_error", "simulated: never answered",
                                              reason="timeout", ambiguous=True)
    transport = open_fake(monkeypatch, healthy_status(method_2004=lost))
    out = await srv.cc2_set_auto_refill(auto_refill=False, confirm=True)
    assert out["status"] == "ambiguous" and out["accepted"] is False
    assert transport.methods().count(2004) == 1
    assert transport.methods().count(srv._cc2_state.METHOD_CANVAS) == 1   # the one look only
    assert "neither a refusal nor a failure" in " ".join(out["notes"])
    json.dumps(out)


async def test_a_refused_auto_refill_keeps_its_code_and_reads_nothing_more(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status(method_2004={"error_code": 1012}))
    out = await srv.cc2_set_auto_refill(auto_refill=True, confirm=True)
    assert out["status"] == "refused" and out["error_code"] == 1012 and out["accepted"] is False
    assert transport.methods() == [2004]            # an on-record refusal is final
    assert "1012" in " ".join(out["notes"])
    assert out["verified_on_this_printer"] is False  # even a refusal keeps its candidate label


@pytest.mark.parametrize("bad", ["true", 1, 0, None, 1.0],
                         ids=["text", "one", "zero", "missing", "float"])
async def test_an_auto_refill_value_that_is_not_a_boolean_refuses_before_any_session(bad, no_open):
    out = await srv.cc2_set_auto_refill(auto_refill=bad)
    assert out["error"] == "protocol_error" and out["reason"] == "refill_argument"
    assert "Nothing was sent" in out["message"]
    assert not no_open                              # no session, so nothing could have been sent


# ------------------------------------------------- the CANVAS tray filament write (2003) — proven at
# ------------------------------------------------- the tool layer, roster gate and all
#
# The two phases a fourth time, with one gate the other controls do not have: the (canvas_id,
# tray_id) pair has to appear in the printer's own 2005 roster, in a unit reporting itself connected,
# on BOTH phases — so the frames below script 2005 too (the observed four-tray roster: A3 active,
# ELEGOO PETG). The gate's own refusals (unknown tray, disconnected unit, unreadable roster) and the
# contradiction note are tests/test_cc2_tray.py; what is pinned here is the tool layer's wire: a
# preview is [2005] with the exact nine-field frame it offers to send, a confirmed call is
# [2005, 2003, 2005] with that same frame sent once, and an unknown material name refuses before a
# session opens at all — the vendor lookup is the gate before the gate, and its 50 names ride out
# whole in the refusal because a name outside it is the type-less, code-less, 0-0 label the vendor's
# own lookup would build for the tray.

def canvas_tray3(name: str, brand: str, colour: str, mn: int, mx: int) -> dict:
    """The observed 2005 roster with tray A3 restated as if the printer already showed this write."""
    frame = copy.deepcopy(CANVAS)
    entry = frame["canvas_info"]["canvas_list"][0]["tray_list"][3]
    entry.update({"filament_type": name, "filament_name": name, "brand": brand,
                  "filament_color": colour, "min_nozzle_temp": mn, "max_nozzle_temp": mx})
    return frame


TRAY_PLA_PARAMS = {"canvas_id": 0, "tray_id": 3, "brand": "ELEGOO", "filament_type": "PLA",
                   "filament_name": "PLA", "filament_code": "0x0000", "filament_color": "#00B0F0",
                   "filament_min_temp": 190, "filament_max_temp": 230}


async def test_the_tray_filament_preview_reads_state_and_roster_and_never_sends_the_command(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status())     # 2003 unscripted on purpose:
    out = await srv.cc2_set_tray_filament(tray_id=3, filament_name="PLA", colour="#00b0f0",
                                          brand="ELEGOO")    # reaching one would still be recorded
    assert out["confirm_required"] is True and out["action"] == "set_tray_filament"
    p = out["preview"]
    # four values from the call, five derived by the vendor catalog; the colour normalised upper-case
    assert p["will_send"] == {"method": 2003, "params": TRAY_PLA_PARAMS}
    assert p["waits_up_to_s"] == 10.0
    assert p["tray_now"]["tray_id"] == 3 and p["tray_now"]["material"] == "PETG"
    assert p["tray_now"]["name"] == "ELEGOO PETG"        # the printer's words for the tray, kept
    # the state the idle gate stood on, in its own field: the printer's number and the doc's word
    assert p["machine_now"] == {"machine_status": 1, "state": "idle"}
    # the idle gate's state read, then the roster read — two reads, no command
    assert transport.methods() == [srv._cc2_state.METHOD_STATUS, srv._cc2_state.METHOD_CANVAS]
    assert out["verified_on_this_printer"] is False
    assert out["unverified"] == srv._cc2_tray.UNVERIFIED   # the cap leaves it whole
    json.dumps(out)


async def test_a_confirmed_tray_write_sends_one_2003_with_the_nine_derived_fields(monkeypatch):
    # the roster is scripted to read back exactly what was sent, so this test pins the wire and the
    # reply; the contradiction note the disagreeing read earns is tests/test_cc2_tray.py's
    transport = open_fake(monkeypatch, healthy_status(method_2005=canvas_tray3("PLA", "ELEGOO",
                                                                               "00B0F0", 190, 230),
                                                      method_2003={"error_code": 0}))
    out = await srv.cc2_set_tray_filament(tray_id=3, filament_name="PLA", colour="#00B0F0",
                                          brand="ELEGOO", confirm=True)
    assert out["status"] == "accepted" and out["accepted"] is True
    assert out["method"] == 2003 and out["sent"] == TRAY_PLA_PARAMS
    assert out["waited_s"] == 10.0 and out["error_code"] is None
    assert out["observed"]["filament_name"] == "PLA"        # the quiet look agrees, nothing to flag
    assert out["verified_on_this_printer"] is False         # agreement is not the command's behaviour
    # the idle gate's state read, the roster read, one command, one look — in that order, and the
    # 2003 carries the nine fields whole
    assert transport.methods() == [srv._cc2_state.METHOD_STATUS, srv._cc2_state.METHOD_CANVAS,
                                   2003, srv._cc2_state.METHOD_CANVAS]
    assert transport.params_for(2003) == TRAY_PLA_PARAMS
    assert transport.methods().count(2003) == 1
    json.dumps(out)


async def test_a_printing_machine_refuses_the_tray_write_before_any_2003(monkeypatch):
    # the 2003 is scripted on purpose: the idle gate runs before the roster read and before the
    # command, so a machine that calls itself printing refuses having asked nothing at all —
    # not even the roster — and a command that did leave would show up on the recorded wire
    transport = open_fake(monkeypatch, healthy_status(method_1002=copy.deepcopy(PRINTING_1002),
                                                      method_2003={"error_code": 0}))
    out = await srv.cc2_set_tray_filament(tray_id=3, filament_name="PLA", colour="#00B0F0",
                                          brand="ELEGOO", confirm=True)
    assert out["error"] == "protocol_error" and out["reason"] == "machine_not_idle"
    assert "Nothing was sent" in out["message"] + str(out.get("hint", ""))
    assert out["printer_says"]                       # the printer's own sentence, in its own field
    assert transport.methods() == [srv._cc2_state.METHOD_STATUS]   # one read, nothing after it
    json.dumps(out)


async def test_an_unknown_filament_name_refuses_the_tray_write_before_any_session_opens(monkeypatch):
    # the catalog refusal is the one refusal that never reaches the seam: parse first, session after,
    # so even a standing scripted printer is never borrowed and no method — not even the roster read
    # — goes out, and the full 50-name list arrives as its own field rather than a capped sentence
    transport = open_fake(monkeypatch, healthy_status())
    out = await srv.cc2_set_tray_filament(tray_id=3, filament_name="Unobtainium", colour="#00B0F0")
    assert out["error"] == "protocol_error" and out["reason"] == "filament_name"
    assert "Nothing was sent" in out["message"]
    assert out["catalog_names"] == srv._cc2_tray.catalog_names()   # whole, in the catalog's order
    assert len(out["catalog_names"]) == 50
    assert transport.opens == [] and transport.methods() == []
    json.dumps(out)


async def test_every_tray_write_refusal_carries_the_two_unverified_labels(monkeypatch, no_network,
                                                                           no_open):
    # the spec's "every reply" includes the refusals: the catalog refusal (before any session), the
    # no-address refusal (the seam's own error) and the gate refusal (a PrinterError from the module)
    # all carry the same two labels the preview and the accepted write carry
    monkeypatch.delenv("ORCA_CC2_URL", raising=False)
    no_config = await srv.cc2_set_tray_filament(tray_id=3, filament_name="PLA", colour="#00B0F0")
    assert no_config["error"] == "not_configured"
    unknown = await srv.cc2_set_tray_filament(tray_id=3, filament_name="Unobtainium",
                                              colour="#00B0F0")
    assert unknown["reason"] == "filament_name"
    transport = open_fake(monkeypatch, healthy_status(method_1002=copy.deepcopy(PRINTING_1002)))
    busy = await srv.cc2_set_tray_filament(tray_id=3, filament_name="PLA", colour="#00B0F0",
                                           confirm=True)
    assert busy["reason"] == "machine_not_idle"
    for out in (no_config, unknown, busy):
        assert out["verified_on_this_printer"] is False, out
        assert out["unverified"] == srv._cc2_tray.UNVERIFIED, out
        json.dumps(out)
    assert transport.methods() == [srv._cc2_state.METHOD_STATUS]


# --------------------------------------------------------------------------- 5. upload, end to end through the real adapter

async def test_upload_sends_parse_verifies_and_receipts_with_no_secrets(
        monkeypatch, tmp_path, receipts_dir, gcode_file):
    size = gcode_file.stat().st_size
    listing = Listing([[], [file_entry(size=size)]])  # collision check clean, verify agrees
    transport = open_fake(monkeypatch, ScriptedTransport({srv._cc2_state.METHOD_FILES: listing}))
    http = FakeHttp()
    monkeypatch.setattr(srv, "_cc2_upload_http_factory", http.factory)

    out = await srv.cc2_upload_gcode(path=str(gcode_file), destination=DEST)
    assert out["uploaded"] == DEST and out["size_bytes"] == size
    assert out["material"] == "PETG" and out["tool_index"] == 0
    # the per-tool view rides beside the flat first-tool fields: one entry per tool the file
    # selects — this slice selects one, and its entry is the flat material and nozzle pair
    assert out["tools"] == [{"tool_index": 0, "material": "PETG",
                             "nozzle_temp_c": {"first_layer": 260.0, "steady": 260.0}}]
    assert out["nozzle_temp_c"] == {"first_layer": 260.0, "steady": 260.0}
    assert out["bed_temp_c"] == {"first_layer": 80.0, "steady": 80.0}
    # the 140 °C non-extruding preheat rides as a commanded setpoint, never as an extrusion temp
    assert out["extrusion_nozzle_temps_c"] == [260.0]
    assert out["commanded_nozzle_setpoints_c"] == [140.0, 260.0]
    assert "cc2_start_print" in out["next"]
    # the stock-profile fields ride the reply whether or not the parse carries them yet
    assert "printer_model" in out and "vendor_templates" in out and "templates_compared" in out
    json.dumps(out)

    assert transport.methods() == [srv._cc2_state.METHOD_FILES] * 2
    assert http.closes == 1 and http.base_urls == [f"http://{HOST}:80"]
    assert len(http.puts) == 1
    headers = http.puts[0]["headers"]
    assert headers["X-File-Name"] == DEST
    assert headers["Content-Range"] == f"bytes 0-{size - 1}/{size}"
    assert headers["X-Token"] == TOKEN  # the session's own password, into a header only
    assert http.puts[0]["body"] == gcode_file.read_bytes()

    # the private receipt is in the store, and it binds the very bytes that were sent
    stored = cc2_receipts.Cc2ReceiptStore(receipts_dir).load(SN, DEST)
    assert stored is not None and stored.material == "PETG"
    assert stored.extrusion_nozzle_temps == (260.0,)
    assert (stored.reported_size_bytes, stored.sent_bytes) == (size, size)

    dumped = json.dumps(out)
    assert TOKEN not in dumped            # the credential never rides out
    assert str(gcode_file) not in dumped  # the local path is not echoed back
    assert "X-Token" not in dumped and "Content-Range" not in dumped


async def test_upload_reply_carries_printer_model_and_vendor_templates_unredacted(
        monkeypatch, tmp_path, receipts_dir, gcode_file):
    size = gcode_file.stat().st_size
    listing = Listing([[], [file_entry(size=size)]])
    open_fake(monkeypatch, ScriptedTransport({srv._cc2_state.METHOD_FILES: listing}))
    monkeypatch.setattr(srv, "_cc2_upload_http_factory", FakeHttp().factory)
    real_upload = srv._cc2_upload.upload_gcode

    async def upload_with_stock_fields(*args, **kwargs):
        receipt = await real_upload(*args, **kwargs)
        # what the upload path hands back once the parser carries these (tasks 1.1 and 1.5)
        object.__setattr__(receipt, "printer_model", "Elegoo Centauri Carbon 2")
        object.__setattr__(receipt, "vendor_templates", "modified: machine_end_gcode")
        object.__setattr__(receipt, "templates_compared", 5)
        return receipt

    monkeypatch.setattr(srv._cc2_upload, "upload_gcode", upload_with_stock_fields)
    out = await srv.cc2_upload_gcode(path=str(gcode_file), destination=DEST)
    assert out["uploaded"] == DEST, out
    assert out["printer_model"] == "Elegoo Centauri Carbon 2"
    assert out["vendor_templates"] == "modified: machine_end_gcode"
    assert out["templates_compared"] == 5
    json.dumps(out)


async def test_upload_refuses_a_collision_without_retrying_or_overwriting(
        monkeypatch, tmp_path, receipts_dir, gcode_file):
    size = gcode_file.stat().st_size
    listing = Listing([[file_entry(size=size)]])  # the name already exists, always will
    transport = open_fake(monkeypatch, ScriptedTransport({srv._cc2_state.METHOD_FILES: listing}))
    http = FakeHttp()
    monkeypatch.setattr(srv, "_cc2_upload_http_factory", http.factory)
    out = await srv.cc2_upload_gcode(path=str(gcode_file), destination=DEST)
    assert out["error"] == "protocol_error" and out["reason"] == "collision"
    assert http.puts == [] and http.closes == 0   # never a byte, let alone a second try
    assert transport.methods() == [srv._cc2_state.METHOD_FILES]
    assert "Nothing was retried" not in out["message"]  # the refusal itself is clean
    assert cc2_receipts.Cc2ReceiptStore(receipts_dir).load(SN, DEST) is None


async def test_an_unreadable_file_refuses_before_any_byte_and_says_so(
        monkeypatch, tmp_path, receipts_dir):
    transport = open_fake(monkeypatch, healthy_status())
    out = await srv.cc2_upload_gcode(path=str(tmp_path / "gone.gcode"), destination=DEST)
    assert out["error"] == "protocol_error" and out["reason"] == "source_unreadable"
    assert transport.methods() == []


async def test_a_command_off_the_list_refuses_through_the_tool_before_any_request(
        monkeypatch, tmp_path, receipts_dir):
    # the parser's command list guards the tool's own path too, not only the adapter: a slice carrying
    # one macro this adapter cannot justify comes back naming it, with the transport never opened and
    # (under this module's network guards) no client ever constructed
    transport = open_fake(monkeypatch, healthy_status())
    src = tmp_path / "with-a-macro.gcode"
    src.write_bytes(gcode_bytes().replace(b"T0\n", b"T0\nG_CODE_MACRO\n", 1))
    out = await srv.cc2_upload_gcode(path=str(src), destination=DEST)
    assert out["error"] == "protocol_error" and out["reason"] == "gcode_unreadable"
    assert "G_CODE_MACRO" in out["message"] and "Nothing was sent" in out["message"]
    # the reply sanitizer caps every string, so a refusal long enough to be cut would lose the one
    # sentence the model most needs — this one arrives whole
    assert not out["message"].endswith("…"), out["message"]
    assert len(out["message"]) <= srv._CC2_STR_MAX
    assert transport.methods() == []


# --------------------------------------------------------------------------- 6. start, end to end through the real adapter

def start_transport(**overrides) -> ScriptedTransport:
    replies = {srv._cc2_state.METHOD_STATUS: IDLE_1002,
               srv._cc2_state.METHOD_CANVAS: CANVAS,
               srv._cc2_state.METHOD_FILES: Listing([[file_entry()]]),
               srv._cc2_start.METHOD_START: {"error_code": 0}}
    for key, value in overrides.items():
        replies[int(key.split("_")[-1])] = value
    return ScriptedTransport(replies)


@pytest.fixture
def with_receipt(receipts_dir):
    cc2_receipts.Cc2ReceiptStore(receipts_dir).save(make_receipt())
    return receipts_dir


async def test_start_maps_levels_and_never_collapses_accepted_with_running(
        monkeypatch, with_receipt):
    transport = open_fake(monkeypatch, start_transport())
    out = await srv.cc2_start_print(filename=DEST, tool_to_tray={"0": "3"}, bed_clear=True)
    assert out["started_accepted"] is True and out["observed_running"] is False
    assert out["status"] == "accepted" and out["error_code"] is None  # accepted never overclaims
    assert out["receipt_sha256"] == SHA
    # leveling defaults to an explicit printer_check false — the firmware remembers the last value
    assert transport.params_for(srv._cc2_start.METHOD_START) == {
        "storage_media": "local", "filename": DEST,
        "config": {"slot_map": [{"t": 0, "canvas_id": 0, "tray_id": 3}], "printer_check": False}}
    # accepted alone carries the honesty notes and never claims a running job
    assert any("not refusing" in n for n in out["notes"])
    assert any("cannot read file content back" in n for n in out["notes"])  # provenance limit
    assert out["observed_state"] == "idle" and out["observed_running"] is False  # the separate
    assert out["observed_job"] is None  # what it saw, reported honestly — idle, not printing
    # bed warnings are never invented: without a carried one the reply says unavailable
    assert out["bed_warning"] is None
    assert out["bed_warning_provenance"].startswith("unavailable")
    json.dumps(out)


async def test_start_levelling_true_and_string_tray_reach_the_wire(
        monkeypatch, with_receipt):
    transport = open_fake(monkeypatch, start_transport())
    # JSON objects carry string keys and models send string values; the exact integer mapping
    # still goes out, and an explicit leveling request flips printer_check on the wire.
    out = await srv.cc2_start_print(filename=DEST, tool_to_tray={" 0 ": " #3 "}, bed_clear=True,
                                    leveling=True, observe_running=False)
    assert out["started_accepted"] is True
    assert transport.params_for(srv._cc2_start.METHOD_START)["config"] == {
        "slot_map": [{"t": 0, "canvas_id": 0, "tray_id": 3}], "printer_check": True}


async def test_a_two_tool_start_maps_every_tool_and_sends_the_two_entry_slot_map(
        monkeypatch, receipts_dir):
    # the receipt this workflow would hold for a two-tool file: tools 0 and 1, both PETG at 260,
    # the flat fields still the first tool's values (the store refuses it otherwise). The
    # observed roster answers: tray A3 active (ELEGOO PETG, 230-260) and tray A2 present
    # (Generic PETG, 230-260), so each tool has a spool of its own material rated for its heat.
    cc2_receipts.Cc2ReceiptStore(receipts_dir).save(make_receipt(tools=(
        cc2_receipts.ToolReceipt(tool_index=0, material="PETG", nozzle_first_layer_temp=260.0,
                                 nozzle_temp=260.0, extrusion_nozzle_temps=(260.0,)),
        cc2_receipts.ToolReceipt(tool_index=1, material="PETG", nozzle_first_layer_temp=260.0,
                                 nozzle_temp=260.0, extrusion_nozzle_temps=(260.0,)))))
    transport = open_fake(monkeypatch, start_transport())
    out = await srv.cc2_start_print(filename=DEST, tool_to_tray={"0": "3", "1": "2"},
                                    bed_clear=True)
    assert out["started_accepted"] is True and out["observed_running"] is False
    # one complete slot_map entry per tool on the single 1020, ordered by t, built from the
    # trays that answered — not from numbers the caller retyped
    assert transport.params_for(srv._cc2_start.METHOD_START)["config"]["slot_map"] == [
        {"t": 0, "canvas_id": 0, "tray_id": 3}, {"t": 1, "canvas_id": 0, "tray_id": 2}]
    assert transport.methods().count(srv._cc2_start.METHOD_START) == 1
    # the adapter's own honesty note travels: a multi-entry slot_map has never been sent here
    assert any("Multi-tool" in n and "never" in n for n in out["notes"]), out["notes"]
    json.dumps(out)


async def test_start_observes_running_as_a_separate_fact_and_strips_reply_noise(
        monkeypatch, with_receipt):
    dirty = copy.deepcopy(PRINTING_1002)
    dirty["print_status"]["filename"] = DEST + "\x1bEVIL"  # an escape rode in with the name
    transport = open_fake(monkeypatch, start_transport(
        method_1002=deque([IDLE_1002, dirty])))
    out = await srv.cc2_start_print(filename=DEST, tool_to_tray={"0": "3"}, bed_clear=True)
    # the printer names a *different* (control-laced) file, so running must stay false —
    # and whatever it names, no control character may ride out in the reply.
    assert out["observed_running"] is False and out["started_accepted"] is True
    assert "\x1b" not in json.dumps(out)
    assert "\x1b" not in (out["observed_job"] or "")
    assert transport.methods().count(srv._cc2_start.METHOD_START) == 1


async def test_start_observed_running_true_only_on_the_observed_pair(monkeypatch, with_receipt):
    exact = copy.deepcopy(PRINTING_1002)
    open_fake(monkeypatch, start_transport(method_1002=deque([IDLE_1002, exact])))
    out = await srv.cc2_start_print(filename=DEST, tool_to_tray={"0": "3"}, bed_clear=True)
    assert out["observed_running"] is True and out["observed_state"] == "printing"
    assert any("Observed printing" in n for n in out["notes"])


async def test_start_carries_a_bed_warning_from_the_caller_without_claiming_provenance(
        monkeypatch, with_receipt):
    open_fake(monkeypatch, start_transport())
    out = await srv.cc2_start_print(filename=DEST, tool_to_tray={"0": "3"}, bed_clear=True,
                                    bed_warning="First layer may not stick on this plate.")
    assert out["bed_warning"] == "First layer may not stick on this plate."
    assert out["bed_warning_provenance"].startswith("carried_from_slice_unverified")
    assert any("valid slice is not proof" in n for n in out["notes"])
    json.dumps(out)


async def test_bed_clear_false_never_lets_a_start_command_leave(monkeypatch, with_receipt):
    transport = open_fake(monkeypatch, start_transport())
    out = await srv.cc2_start_print(filename=DEST, tool_to_tray={"0": "3"}, bed_clear=False)
    assert out["error"] == "protocol_error" and out["reason"] == "bed_not_clear"
    assert srv._cc2_start.METHOD_START not in transport.methods()
    # only reads happened before the refusal, and the hold came back all the same
    assert srv._CC2_POOL.holds == 0
    assert set(transport.methods()) <= {srv._cc2_state.METHOD_STATUS,
                                        srv._cc2_state.METHOD_CANVAS,
                                        srv._cc2_state.METHOD_FILES}


async def test_a_missing_receipt_refuses_without_any_command(monkeypatch, receipts_dir):
    transport = open_fake(monkeypatch, start_transport())
    out = await srv.cc2_start_print(filename=DEST, tool_to_tray={"0": "3"}, bed_clear=True)
    assert out["error"] == "protocol_error" and out["reason"] == "receipt_missing"
    assert srv._cc2_start.METHOD_START not in transport.methods()


async def test_a_lost_start_answer_is_ambiguous_and_never_resent(monkeypatch, with_receipt):
    transport = open_fake(monkeypatch, start_transport(
        method_1020=srv._cc2_transport.Cc2TimeoutError(
            "protocol_error", "simulated: the printer never answered the start",
            reason="timeout", ambiguous=True),
        method_1002=deque([IDLE_1002, IDLE_1002])))
    out = await srv.cc2_start_print(filename=DEST, tool_to_tray={"0": "3"}, bed_clear=True)
    assert out["status"] == "ambiguous"
    assert out["started_accepted"] is False and out["observed_running"] is False
    assert transport.methods().count(srv._cc2_start.METHOD_START) == 1  # never resent
    assert any("not be resent" in n or "not been resent" in n or "not resent" in n
               for n in out["notes"])


# --------------------------------------------------------------------------- 7. failures are structured and sanitised

async def test_a_session_failure_is_a_structured_refusal_and_leaves_nothing_pooled(monkeypatch):
    # an open that fails is reported as itself, and the pool keeps nothing: what never opened cannot
    # be borrowed by the next call, and the next call pays its own honest open.
    monkeypatch.setenv("ORCA_CC2_URL", HOST)

    async def refuse(*args, **kwargs):
        raise srv.PrinterError("not_reachable",
                               "simulated: the printer did not answer discovery")
    monkeypatch.setattr(srv, "_cc2_open_transport", refuse)
    out = await srv.cc2_status()
    assert out == {"error": "not_reachable",
                   "message": "simulated: the printer did not answer discovery"}
    assert srv._CC2_POOL.open is False and srv._CC2_POOL.holds == 0


async def test_adapter_errors_come_back_sanitised(monkeypatch, tmp_path, receipts_dir):
    open_fake(monkeypatch, healthy_status(
        method_1044=srv._cc2_state.Cc2DataError(
            "protocol_error",
            "simulated bad frame \x1b[31mfrom\x07 the printer", reason="inventory_malformed")))
    out = await srv.cc2_list_files()
    assert out["error"] == "protocol_error" and out["reason"] == "inventory_malformed"
    assert "\x1b" not in out["message"] and "\x07" not in out["message"]
    json.dumps(out)


def test_redaction_precedes_truncation_and_handles_short_codes():
    assert "abcde" not in srv._cc2_out("x" * 250 + "abcdefgh", _code="abcdefgh")
    assert "xyz" not in srv._cc2_out("prefix xyz suffix", _code="xyz")


def test_sanitized_key_collisions_withhold_the_conflicting_mapping():
    assert srv._cc2_out({"state": "idle", " state ": "printing"}, _code="abcdef") == {
        "error": "sanitization_key_collision"
    }
    assert srv._cc2_out({"abc_first": 1, "<redacted>_first": 2}, _code="abc") == {
        "error": "sanitization_key_collision"
    }
    assert "collision" not in str(srv._cc2_out({"state": "idle", " state ": "printing"},
                                                _code="collision"))


def test_the_sanitizer_bounds_shapes_and_redacts_a_configured_code(monkeypatch, tmp_path):
    code_file = tmp_path / "code"
    code_file.write_text(CODE)
    code_file.chmod(0o600)
    monkeypatch.setenv("ORCA_CC2_URL", HOST)
    monkeypatch.setenv("ORCA_CC2_ACCESS_CODE_FILE", str(code_file))
    messy = {"message": "  he\x00llo\x07world\x1b  ", "long": "x" * 500,
             "nested": {"deep": [1, float("nan"), float("inf"), (2, 3), {"x" * 300: None}]},
             "obj": object(), "big": 2 ** 70}
    out = srv._cc2_out(messy)
    assert out["message"] == "he llo world"  # controls become spaces, the shells get stripped
    assert out["long"].endswith("…") and len(out["long"]) <= srv._CC2_STR_MAX + 1
    assert out["nested"]["deep"][1] is None and out["nested"]["deep"][2] is None
    assert out["nested"]["deep"][3] == [2, 3]
    assert out["obj"] is None and out["big"] is None
    json.dumps(out)
    leak = srv._cc2_err(srv._cc2_transport.PrinterError(
        "auth_rejected", f"simulated refusal with the code {CODE} inside"))
    assert CODE not in json.dumps(leak) and "<redacted>" in leak["message"]


def _walk_keys(value):
    """Every dictionary key of a reply, however deep it sits in it."""
    if isinstance(value, dict):
        for key, sub in value.items():
            yield key
            yield from _walk_keys(sub)
    elif isinstance(value, list):
        for sub in value:
            yield from _walk_keys(sub)


def test_a_short_access_code_leaves_the_key_names_the_server_wrote_alone():
    """The review's case exactly. The configured access code is whatever the printer's LAN happens
    to use, and a short one used to be rewritten into the reply's OWN key names: with the code "a"
    "filename" arrived as "filen<redacted>me" and "state" as "st<redacted>te", so a trusted reply
    carried keys no client was told to expect. Contract keys go out whole; the text under them is
    still passed, however short the code is."""
    reply = {"status": "printing", "filename": DEST, "state_word": "idle", "complete": True,
             "job": {"current_layer": 51, "task_name": DEST, "state": "printing"},
             "temps": {"nozzle_c": 259.6, "nozzle_target_c": 260},
             "files": [{"name": DEST, "size_bytes": 5712759, "tools": ["PETG"]}]}
    out = srv._cc2_out(reply, _code="a")
    assert list(out) == list(reply)
    assert list(out["job"]) == ["current_layer", "task_name", "state"]
    assert list(out["files"][0]) == ["name", "size_bytes", "tools"]
    assert out["filename"] == DEST.replace("a", "<redacted>")   # the value is still passed
    assert "a" not in json.dumps(out["job"]["state"])
    assert all("<redacted>" not in k for k in _walk_keys(out))
    json.dumps(out)


def test_keys_the_server_did_not_write_are_redacted_however_short_the_code_is():
    """The exemption is exact names, not substrings: a key carrying the code is text from wherever
    it came from, and it is treated as such — control characters stripped, capped, and the code
    replaced — even when the redacted result would have looked like a contract key."""
    out = srv._cc2_out({"status" + CODE: 1, f"filen{CODE}ame": 2, "in\rvalid key": 3,
                        "u" * 400: 4, "filenames": CODE}, _code=CODE)
    keys = list(out)
    assert keys[0] == "status<redacted>"
    assert keys[1] == "filen<redacted>ame"          # NOT "filename": the pass is not a substring test
    assert keys[2] == "in valid key" and len(keys[3]) <= srv._CC2_STR_MAX + 1
    assert out["filenames"] == "<redacted>"        # and a short key is no safer than a long one
    assert CODE not in json.dumps(out)


def test_a_code_straddling_a_value_cap_never_survives_the_cut():
    """Redaction runs before the cap, so a code the 255-character cut would have split in half is
    still gone, at any nesting depth and whichever side of the boundary it sits on."""
    for offset in (249, 250, 251, 255):
        long_value = "x" * offset + "abcdefgh" + "y" * 40
        assert "abcdefgh" not in srv._cc2_out(long_value, _code="abcdefgh")
        deep = srv._cc2_out({"job": {"note": long_value, "task_name": [long_value]}},
                            _code="abcdefgh")
        assert "abcdefgh" not in json.dumps(deep)


async def test_under_a_one_letter_code_every_key_a_tool_answers_is_a_name_the_server_wrote(
        monkeypatch):
    """The completeness check behind the exemption list: configure a code that is a substring of
    nearly every key this server emits, answer with the scripted printer, and require every key of
    every reply to be a declared name. A reply field added without its name in _CC2_REPLY_KEYS
    fails here with the name in the diff, instead of reaching a client mangled."""
    monkeypatch.setattr(srv, "_cc2_configured_code", lambda: "e")
    _stub_certified_shelf(monkeypatch, 3)
    open_fake(monkeypatch, healthy_status(method_1001=ATTRS_1001, method_1046=DETAIL_1046,
                                          method_1036=HISTORY_1036, method_2003={"error_code": 0}))
    replies = [await srv.cc2_status(), await srv.cc2_list_files(), await srv.cc2_printer_info(),
               await srv.cc2_file_detail(filename=DEST), await srv.cc2_print_history(),
               await srv.cc2_pause_print(), await srv.cc2_set_light(on=True)]
    # the tray write's three answers join the walk: the preview's names, the confirmed reply's names,
    # and the unknown-name refusal, whose catalog_names rides out as a top-level key of the refusal
    replies += [await srv.cc2_set_tray_filament(tray_id=3, filament_name="PLA", colour="#00b0f0"),
                await srv.cc2_set_tray_filament(tray_id=3, filament_name="PLA", colour="#00B0F0",
                                                brand="ELEGOO", confirm=True),
                await srv.cc2_set_tray_filament(tray_id=3, filament_name="Unobtainium",
                                                colour="#00B0F0")]
    replies.append(srv._cc2_err(srv.PrinterError("protocol_error", "simulated refusal",
                                                 reason="gate")))
    seen: set[str] = set()
    for reply in replies:
        keys = set(_walk_keys(reply))
        assert all("<redacted>" not in k for k in keys), sorted(keys - srv._CC2_REPLY_KEYS)
        seen |= keys
    assert len(seen) > 60, "this check is only worth anything if it walks a real reply"
    assert seen <= srv._CC2_REPLY_KEYS
    assert {"error", "message", "reason", "status", "files", "name", "filename",
            "entries"} <= seen


def test_the_source_writes_no_reply_key_the_exemption_list_does_not_name():
    """The exemption list only protects the contract if it stays complete, so it is checked against
    the source that writes it: every identifier-shaped name the cc2 adapters put in a dictionary
    literal has to be declared in _CC2_REPLY_KEYS. A wire field name or a command parameter belongs
    there whether or not it is ever read back, because a name this source contains is a name this
    server wrote, and that is the whole test the exemption applies. A name that is not
    identifier-shaped (the header "X-File-Name", a G-code word, another tool's name) can never be a
    key a client reads, and the list's own shape rule keeps them out; keys a reply carries only as
    **kwargs (a PrinterError's reason=, method=) are not dictionary literals, and the runtime check
    above is what pins those."""
    import ast
    import glob

    root = Path(srv.__file__).resolve().parent / "printer"
    written: set[str] = set()
    for path in sorted(glob.glob(str(root / "cc2_*.py"))) + [str(root / "errors.py")]:
        for node in ast.walk(ast.parse(Path(path).read_text(encoding="utf-8"))):
            if isinstance(node, ast.Dict):
                written |= {k.value for k in node.keys
                            if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    named = {n for n in written if re.fullmatch(r"[a-z][a-z0-9_]+", n)}
    assert named <= srv._CC2_REPLY_KEYS, sorted(named - srv._CC2_REPLY_KEYS)
    assert len(named) > 150, "the scan read too little of the source to be a check"
    assert all(re.fullmatch(r"_?[a-z][a-z0-9_]+", k) for k in srv._CC2_REPLY_KEYS)


# ------------------------------------ one shared session across calls (the printer's four MQTT slots)

async def test_a_sequence_of_tool_calls_shares_one_registered_session(monkeypatch):
    """The regression, at the layer Nathan's calls arrive at. Four ordinary calls used to mean four
    registrations, and a broker that takes about four clients refused the fourth — `too_many_clients`
    from a printer that was idle and healthy, which then refused for a while. One session now answers
    all four, each call gives its hold back, and the session is left open for the next."""
    transport = open_fake(monkeypatch, healthy_status())
    assert (await srv.cc2_status())["machine"]["state_word"] == "idle"
    assert (await srv.cc2_list_files())["complete"] is True
    assert (await srv.cc2_set_light(on=True))["confirm_required"] is True
    assert (await srv.cc2_stop_print())["confirm_required"] is True

    assert transport.opens == ["open"]                     # four calls, one registration
    assert srv._CC2_POOL.holds == 0                        # and every call gave its hold back
    assert transport.closed is False                       # still the printer's client, ready for more
    assert transport.methods().count(srv._cc2_state.METHOD_FILES) == 1   # nothing was asked twice


async def test_two_calls_at_the_same_moment_wait_for_one_open_rather_than_registering_twice(monkeypatch):
    """Two calls arriving together must not each register: the roster is the scarce thing, and one
    process being two clients is exactly what filled it. The second caller waits for the open that is
    already in flight and is answered by the session it opens."""
    transport = open_fake(monkeypatch, healthy_status())
    gate = asyncio.Event()
    in_flight: list = []

    async def slow_open(config):
        in_flight.append("open")
        await gate.wait()                 # the open stands where a real discovery + register would
        return transport, TOKEN
    monkeypatch.setattr(srv, "_cc2_open_transport", slow_open)

    first = asyncio.create_task(srv.cc2_status())
    second = asyncio.create_task(srv.cc2_list_files())
    await asyncio.sleep(0.02)
    assert in_flight == ["open"]          # one open, and the other caller is waiting rather than registering
    gate.set()
    a, b = await asyncio.gather(first, second)
    assert a["machine"]["state_word"] == "idle" and b["complete"] is True
    assert in_flight == ["open"] and srv._CC2_POOL.holds == 0


async def test_a_session_that_died_between_calls_is_dropped_and_the_next_call_gets_a_fresh_one(monkeypatch):
    """A session the transport reported dead cannot serve the next read, and the caller is not made to
    pay for it or to notice: the dead one is closed and this same call is answered by a new session.
    Nothing was resent on the way — the command that was in flight when it died was reported by the
    reply that reported the death, and this call is a command of its own."""
    dead = open_fake(monkeypatch, healthy_status())
    assert (await srv.cc2_status())["machine"]["state_word"] == "idle"
    dead.last_error = srv.PrinterError("not_reachable",
                                       "simulated: the MQTT session with the printer was dropped",
                                       reason="lost", ambiguous=True)

    fresh = healthy_status()
    fresh_opens: list = []

    async def open_fresh(config):
        fresh_opens.append("open")
        return fresh, TOKEN
    monkeypatch.setattr(srv, "_cc2_open_transport", open_fresh)

    out = await srv.cc2_status()
    assert out["machine"]["state_word"] == "idle"
    assert fresh_opens == ["open"]        # the fresh session was opened by this call, once
    assert fresh.methods().count(srv._cc2_state.METHOD_STATUS) >= 1
    assert dead.closed is True            # and the dead one left the printer's roster
    assert srv._CC2_POOL.holds == 0


async def test_moving_the_configured_address_between_calls_opens_a_fresh_session(monkeypatch):
    """The configuration is re-read on every borrow, so re-pointing the server cannot leave a tool
    talking to the printer it was configured for before."""
    transport = open_fake(monkeypatch, healthy_status())
    assert (await srv.cc2_status())["machine"]["state_word"] == "idle"
    monkeypatch.setenv(srv._cc2_transport.ENV_URL, "192.0.2.99")   # a different printer, same policy
    assert (await srv.cc2_status())["machine"]["state_word"] == "idle"
    assert transport.opens == ["open", "open"]
    assert transport.closed is True        # the abandoned session was given back, not kept


async def test_a_later_upload_still_carries_the_credential_the_session_was_met_with(
        monkeypatch, tmp_path, receipts_dir, gcode_file):
    """Pooling must not break the credential handoff the upload depends on: the upload is the second
    call on a session another call opened, and it still authenticates with the password *that* session
    was met with — into a header only, and never out in the reply."""
    size = gcode_file.stat().st_size
    listing = Listing([[], [file_entry(size=size)]])   # collision check clean, verify agrees
    transport = open_fake(monkeypatch, healthy_status(method_1044=listing))
    http = FakeHttp()
    monkeypatch.setattr(srv, "_cc2_upload_http_factory", http.factory)

    await srv.cc2_status()                              # a different call opens the session first
    out = await srv.cc2_upload_gcode(path=str(gcode_file), destination=DEST)

    assert out["uploaded"] == DEST
    assert transport.opens == ["open"]                  # one session for both calls
    assert http.puts[0]["headers"]["X-Token"] == TOKEN   # the session's own password, into a header only
    assert TOKEN not in json.dumps(out)


async def test_a_cancelled_tool_call_gives_the_session_back_and_leaves_it_usable(monkeypatch):
    """A call cancelled by its client is an ordinary ending, not a leak: the hold comes back, the
    healthy session is not closed by a caller that left, and the next call is answered by it without a
    new registration."""
    gate = asyncio.Event()

    async def never_answers(params):      # a request still in flight when the client goes away
        await gate.wait()
        return IDLE_1002
    transport = open_fake(monkeypatch, healthy_status(method_1002=never_answers))
    task = asyncio.create_task(srv.cc2_status())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert srv._CC2_POOL.holds == 0 and transport.closed is False

    gate.set()
    transport.replies[srv._cc2_state.METHOD_STATUS] = IDLE_1002
    assert (await srv.cc2_status())["machine"]["state_word"] == "idle"
    assert transport.opens == ["open"]     # the next call shared the session the cancelled one left


async def test_the_server_stopping_gives_the_printer_its_slot_back(monkeypatch):
    """The CC2 session lives on the server's own loop, so the server's own exit is where it closes: a
    client left behind across the exit is one the printer has to time out on its own clock."""
    transport = open_fake(monkeypatch, healthy_status())
    assert (await srv.cc2_status())["machine"]["state_word"] == "idle"
    served: list = []

    async def fake_run_stdio(lowlevel_server):
        served.append(lowlevel_server)
    monkeypatch.setattr(srv, "run_stdio", fake_run_stdio)

    await srv._serve_stdio_then_close()
    assert served == [srv.mcp._lowlevel_server]
    assert transport.closed is True and srv._CC2_POOL.open is False and srv._CC2_POOL.holds == 0


async def test_a_session_closes_when_the_server_stops_because_stdio_failed(monkeypatch):
    """The close is a finally, so a server that stopped because its transport died still hands the
    slot back — and the failure stays the story, which is what the caller reads."""
    transport = open_fake(monkeypatch, healthy_status())
    assert (await srv.cc2_status())["machine"]["state_word"] == "idle"

    async def failing_run_stdio(lowlevel_server):
        raise RuntimeError("simulated: stdio closed")
    monkeypatch.setattr(srv, "run_stdio", failing_run_stdio)

    with pytest.raises(RuntimeError):
        await srv._serve_stdio_then_close()
    assert transport.closed is True


# --------------------------------------------------------------------------- 8. schema and annotation completeness

def test_json_schema_is_mcp_compatible_and_documented():
    import anyio
    tools = {t.name: t for t in anyio.run(srv.mcp.list_tools)}
    start = tools["cc2_start_print"].input_schema
    assert start["type"] == "object"
    assert set(start["required"]) == {"filename", "tool_to_tray", "bed_clear"}
    props = start["properties"]
    assert props["filename"]["type"] == "string"
    assert props["tool_to_tray"]["type"] == "object"          # JSON keys are strings — see below
    assert props["tool_to_tray"]["additionalProperties"]["type"] == "string"
    assert props["bed_clear"]["type"] == "boolean"
    assert "default" not in props["bed_clear"]                # no silent default for the assertion
    assert props["leveling"]["default"] is False              # leveling off unless asked
    assert props["observe_running"]["default"] is True
    assert props["bed_warning"].get("anyOf")[0]["type"] == "string"
    assert all(p.get("description") for p in props.values())

    up = tools["cc2_upload_gcode"].input_schema
    assert set(up["required"]) == {"path", "destination"}
    assert all(p.get("description") for p in up["properties"].values())
    for name in ("cc2_status",):
        schema = tools[name].input_schema
        assert schema["type"] == "object" and schema["properties"] == {}
    # the file listing now takes the two page arguments, both optional and both documented: a caller
    # reading only the schema can see the default page and that the limit has a maximum
    listing = tools["cc2_list_files"].input_schema
    assert listing["type"] == "object" and listing.get("required", []) == []
    assert listing["properties"]["offset"]["type"] == "integer"
    assert listing["properties"]["offset"]["default"] == 0
    assert listing["properties"]["limit"]["type"] == "integer"
    assert listing["properties"]["limit"]["default"] == srv._CC2_FILES_MAX == 200
    assert all(p.get("description") for p in listing["properties"].values())
    # the peripheral controls are plainly typed: the light is a boolean, the speed is the printer's
    # integer mode, and both carry the optional confirm. Neither has a schema-level range: an
    # out-of-range mode is refused with the four modes named, which a schema cannot say.
    light = tools["cc2_set_light"].input_schema
    assert set(light["required"]) == {"on"}
    assert light["properties"]["on"]["type"] == "boolean"
    assert light["properties"]["confirm"]["default"] is False
    speed = tools["cc2_set_print_speed"].input_schema
    assert set(speed["required"]) == {"mode"}
    assert speed["properties"]["mode"]["type"] == "integer"
    assert speed["properties"]["confirm"]["default"] is False
    assert all(p.get("description") for p in light["properties"].values())
    assert all(p.get("description") for p in speed["properties"].values())
    assert "not a percentage" in speed["properties"]["mode"]["description"]
    # the temperature control is typed the same plain way: a heater name, a number of degrees, and
    # the optional confirm. No schema-level range again — an out-of-range setpoint refuses with the
    # ceiling and where it comes from, which a schema cannot say, and a schema range would invite a
    # reader to believe the number is a hardware rating when it is the official control's bound.
    heat = tools["cc2_set_temperature"].input_schema
    assert set(heat["required"]) == {"heater", "target"}
    assert heat["properties"]["heater"]["type"] == "string"
    assert heat["properties"]["target"]["type"] == "number"
    assert heat["properties"]["confirm"]["default"] is False
    assert all(p.get("description") for p in heat["properties"].values())
    assert "0 to 350 for the extruder, 0 to 110" in heat["properties"]["target"]["description"]
    assert "not a hardware rating" in heat["properties"]["target"]["description"]
    # the CANVAS switch is typed the same plain way: one boolean and the optional confirm. Being a
    # candidate is said in its replies, not hidden in its schema, so there is nothing exotic here —
    # and no second field, because the one field it sends is the safety property of this command.
    refill = tools["cc2_set_auto_refill"].input_schema
    assert set(refill["required"]) == {"auto_refill"}
    assert refill["properties"]["auto_refill"]["type"] == "boolean"
    assert refill["properties"]["confirm"]["default"] is False
    assert all(p.get("description") for p in refill["properties"].values())
    assert "plain boolean" in refill["properties"]["auto_refill"]["description"]
    assert "Second-call only" in refill["properties"]["confirm"]["description"]
    # the tray write is typed the same plain way: the printer's two ids, the vendor catalog's own
    # material name and a #RRGGBB colour, with brand, canvas_id and confirm defaulted. No type, code
    # or temperature is an argument and no schema range appears — whether a (canvas_id, tray_id) pair
    # exists is the printer's roster's answer, which a schema cannot say.
    tray = tools["cc2_set_tray_filament"].input_schema
    assert set(tray["required"]) == {"tray_id", "filament_name", "colour"}
    assert tray["properties"]["tray_id"]["type"] == "integer"
    assert tray["properties"]["filament_name"]["type"] == "string"
    assert tray["properties"]["colour"]["type"] == "string"
    assert tray["properties"]["brand"]["default"] == "Generic"
    assert tray["properties"]["canvas_id"]["default"] == 0
    assert tray["properties"]["confirm"]["default"] is False
    assert not ({"filament_type", "filament_code", "filament_min_temp", "filament_max_temp"}
                & set(tray["properties"]))      # derived by the catalog, never arguments
    assert all(p.get("description") for p in tray["properties"].values())


def test_annotations_classify_the_seventeen_cc2_tools():
    tools = srv.mcp._tool_manager._tools
    # the whole tool list is the annotation table: seventeen direct tools and everything that predates
    # them, each with its title and hints, nothing registered that the table does not speak for
    assert set(srv._TOOL_ANNOTATIONS) == set(tools)
    for name in ("cc2_status", "cc2_list_files", "cc2_printer_info", "cc2_file_detail",
                 "cc2_file_thumbnail", "cc2_print_history", "cc2_camera_snapshot"):
        ann = tools[name].annotations
        assert ann.read_only_hint is True and ann.destructive_hint is None
        assert ann.open_world_hint is True and ann.idempotent_hint is None
    for name in ("cc2_upload_gcode", "cc2_start_print", "cc2_pause_print", "cc2_resume_print",
                 "cc2_set_light", "cc2_set_print_speed", "cc2_set_temperature",
                 "cc2_set_auto_refill", "cc2_set_tray_filament"):
        ann = tools[name].annotations
        assert ann.read_only_hint is False and ann.destructive_hint is False
        assert ann.open_world_hint is True
        assert ann.idempotent_hint is None  # writes carry no idempotency hint — they are not
    stop = tools["cc2_stop_print"].annotations   # the irreversible one says so
    assert stop.read_only_hint is False and stop.destructive_hint is True


# ------------------------------------------------------------------ 9. discovery, in a fresh process
#
# Discovery is a decision the server makes once, at import, so the only honest witness is a fresh
# interpreter. Each child gets a hand-built environment — PATH, HOME and exactly the variables the
# test names — so it is proven by construction that no credential, printer address or receipt store
# was present for it to read, let alone echo. What the new posture has to buy is exactly this: the
# seventeen direct tools are listed by default, a printer address changes nothing about that list, and
# the server's other tools come back identical in every case.

BASELINE_TOOLS = frozenset(srv.mcp._tool_manager._tools) - CC2_NAMES

LIST_TOOLS = ("import asyncio, json, orcaslicer_mcp.server as srv; "
              "print(json.dumps(sorted(t.name for t in asyncio.run(srv.mcp.list_tools()))))")

# The unconfigured calls: label, tool function, arguments. `confirm` variants included, because a
# preview path and a confirmed path both have to refuse before any socket when there is no printer.
PROBE_CALLS = [
    ("cc2_status", "cc2_status", {}),
    ("cc2_list_files", "cc2_list_files", {}),
    ("cc2_printer_info", "cc2_printer_info", {}),
    ("cc2_file_detail", "cc2_file_detail", {"filename": DEST}),
    ("cc2_file_thumbnail", "cc2_file_thumbnail", {"filename": DEST}),
    ("cc2_print_history", "cc2_print_history", {}),
    ("cc2_camera_snapshot", "cc2_camera_snapshot", {}),
    ("cc2_upload_gcode", "cc2_upload_gcode", {"path": "/nowhere/x.gcode", "destination": DEST}),
    ("cc2_start_print", "cc2_start_print", {"filename": DEST, "tool_to_tray": {"0": "3"},
                                            "bed_clear": True}),
    ("cc2_pause_preview", "cc2_pause_print", {}),
    ("cc2_pause_confirmed", "cc2_pause_print", {"confirm": True}),
    ("cc2_resume_confirmed", "cc2_resume_print", {"confirm": True}),
    ("cc2_stop_confirmed", "cc2_stop_print", {"confirm": True}),
    ("cc2_light_preview", "cc2_set_light", {"on": True}),
    ("cc2_light_confirmed", "cc2_set_light", {"on": False, "confirm": True}),
    ("cc2_speed_preview", "cc2_set_print_speed", {"mode": 2}),
    ("cc2_speed_confirmed", "cc2_set_print_speed", {"mode": 0, "confirm": True}),
    ("cc2_temperature_preview", "cc2_set_temperature", {"heater": "extruder", "target": 220}),
    ("cc2_temperature_confirmed", "cc2_set_temperature",
     {"heater": "heater_bed", "target": 60, "confirm": True}),
    ("cc2_auto_refill_preview", "cc2_set_auto_refill", {"auto_refill": True}),
    ("cc2_auto_refill_confirmed", "cc2_set_auto_refill",
     {"auto_refill": False, "confirm": True}),
    ("cc2_tray_filament_preview", "cc2_set_tray_filament",
     {"tray_id": 3, "filament_name": "PLA", "colour": "#00B0F0"}),
    ("cc2_tray_filament_confirmed", "cc2_set_tray_filament",
     {"tray_id": 3, "filament_name": "PLA", "colour": "#00B0F0", "confirm": True}),
]

# Every guard in this child is installed before the server is imported, so a name lookup, a socket,
# an MQTT link or an HTTP request made during import, during tools/list or during any call aborts
# the child instead of being reported by it. A clean run is therefore the proof, not a hope.
PROBE_UNCONFIGURED = """
import asyncio, json, socket
import http.client
import httpx


def trip(name):
    def hit(*args, **kwargs):
        raise AssertionError("network while unconfigured: " + name)
    return hit


socket.socket.connect = trip("socket.connect")
socket.socket.connect_ex = trip("socket.connect_ex")
socket.socket.sendto = trip("socket.sendto")
socket.socket.sendall = trip("socket.sendall")
socket.getaddrinfo = trip("socket.getaddrinfo")
socket.create_connection = trip("socket.create_connection")
asyncio.BaseEventLoop.create_datagram_endpoint = trip("create_datagram_endpoint")
httpx.AsyncClient.request = trip("httpx.request")
httpx.AsyncClient.send = trip("httpx.send")
httpx.AsyncClient.__init__ = trip("httpx.init")
http.client.HTTPConnection.request = trip("http.request")

import orcaslicer_mcp.server as srv

CALLS = __CALLS__


async def probe():
    names = sorted(t.name for t in await srv.mcp.list_tools())
    replies = {}
    for label, tool, kwargs in CALLS:
        replies[label] = await getattr(srv, tool)(**kwargs)
    return {"tools": names, "replies": replies}


print(json.dumps(asyncio.run(probe())))
""".replace("__CALLS__", repr(PROBE_CALLS))


def child(argv: str, env_overrides: dict[str, str], *, home: str | None = None,
          check: bool = True) -> str:
    """Run a snippet in a fresh interpreter whose environment is PATH, HOME (optionally a caller's
    empty directory, to prove nothing about discovery depends on user config) and exactly the
    variables named here — no printer address inherited, no credential inherited, nothing CC2."""
    child_env = {"PATH": os.environ.get("PATH", ""), "HOME": home or os.environ.get("HOME", ""),
                 "PYTHONIOENCODING": "utf-8"}
    child_env.update(env_overrides)
    assert not [key for key in child_env
                if key.startswith("ORCA_CC2_") and key not in env_overrides], child_env
    done = subprocess.run([sys.executable, "-c", argv], env=child_env, cwd=str(REPO_ROOT),
                          capture_output=True, text=True, timeout=180, check=False)
    if check:
        assert done.returncode == 0, done.stderr[-2000:]
        return done.stdout
    assert done.returncode != 0, f"expected the child to fail, got: {done.stdout[:500]}"
    return done.stderr


def test_all_seventeen_direct_tools_are_listed_with_no_printer_config(tmp_path):
    # the posture Nathan asked for: no read/write switch, no hidden tools. A client pointed at a
    # server with no printer configured is still shown all seventeen — in an empty HOME, with no
    # credential anywhere in the child's environment — and every other tool is untouched by it
    names = set(json.loads(child(LIST_TOOLS, {}, home=str(tmp_path))))
    assert CC2_NAMES == frozenset(srv.CC2_TOOL_NAMES)
    assert names == BASELINE_TOOLS | CC2_NAMES


def test_a_configured_address_changes_nothing_about_what_is_listed(tmp_path):
    # configuring the printer is the only switch there is, and it is a switch about the network, not
    # about discovery: the same tools, in the same list, before and after
    plain = set(json.loads(child(LIST_TOOLS, {}, home=str(tmp_path))))
    configured = set(json.loads(child(LIST_TOOLS, {"ORCA_CC2_URL": HOST}, home=str(tmp_path))))
    assert configured == plain == BASELINE_TOOLS | CC2_NAMES


def test_a_child_that_forbids_the_network_lists_all_seventeen_and_refuses_all_seventeen(tmp_path):
    # discovery needs nothing, and a call with nothing configured answers honestly instead of
    # reaching out: the guards in the child raise on the first socket, name lookup, MQTT link or
    # HTTP request, so this test passing IS the zero-network proof
    stdout = child(PROBE_UNCONFIGURED, {}, home=str(tmp_path))
    out = json.loads(stdout)
    assert BASELINE_TOOLS | CC2_NAMES <= set(out["tools"])
    assert set(out["replies"]) == {label for label, _, _ in PROBE_CALLS}
    for label, reply in out["replies"].items():
        assert reply["error"] == "not_configured", (label, reply)
        assert reply["printer_contacted"] is False, (label, reply)
        assert "ORCA_CC2_URL" in reply["message"] or "ORCA_CC2_URL" in reply["hint"], (label, reply)
        # nothing the child was given, and nothing it could have inferred, rides along in a refusal
        assert str(tmp_path) not in json.dumps(reply), (label, reply)
    # and the one read whose own reply admits it has never been seen on a printer still refuses here
    # without ever getting that far: no session, so no entries and no candidate claim on the wire
    assert "entries" not in json.dumps(out), stdout
    assert TOKEN not in stdout and CODE not in stdout


def test_the_probe_child_is_armed_and_fails_when_a_tool_reaches_the_network(tmp_path):
    # the negative control on the test above: the same child, with a printer address it may use, must
    # die on the first discovery packet instead of reporting replies. Without this, the clean run
    # would prove nothing except that the guards were never armed.
    complaint = child(PROBE_UNCONFIGURED, {"ORCA_CC2_URL": HOST}, home=str(tmp_path), check=False)
    assert "network while unconfigured" in complaint, complaint[-800:]


def test_this_module_leaves_the_direct_tools_listed_as_it_found_them():
    # a hygiene check on the module itself, last by file order: the tests above patch, fake and
    # restore freely, and this is where a leak would surface. The seventeen are declared at import, so
    # what a later module must see is exactly those seventeen plus everything that predates them
    assert frozenset(srv.mcp._tool_manager._tools) == BASELINE_TOOLS | CC2_NAMES
