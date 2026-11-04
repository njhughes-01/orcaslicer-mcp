"""The four direct CC2 tools exactly as the server wires them, against scripted fakes: the
server-side gates, the adapters, and the reply sanitiser. Defaults are proven first — with no
opt-ins set, every tool refuses and NOTHING touches the network: the fixture guards socket
primitives, the MQTT link factory and httpx for the whole module, and the tests additionally
assert the transport seam was never reached. The read gate (ORCA_CC2_READ) and the write gate
(ORCA_CC2_WRITE) are proven independent of each other. With the gates opened by monkeypatched
env, the tools run end to end against fake transports (the observed 1002 idle / printing, the
observed four-tray 2005 where status 2 is active, the 1044 listing shapes), a fake chunk sender,
and a temp receipt store: parameter forwarding is proven on the recorded wire (the one 1020 with
printer_check explicit both ways, the tool-to-tray map, the chunk headers and X-Token), the
upload→start chain runs through the real adapters and the private receipt, accepted and
observed_running stay separate facts, and a lost 1044 reply comes back as an error, never a
partial list. Replies are asserted secret-free (the simulated token, the simulated access code,
the local path never appear in any output) and sanitised (control characters from a printer
reply are stripped, oversized strings capped, non-finite numbers and unserialisable objects
turned to None). No live printer, no Home Assistant, no real network, no real credentials — the
addresses are documented TEST-NET and every credential below is a stand-in string."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import http.client
import httpx
import json
import socket
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import pytest

import orcaslicer_mcp.server as srv

# --------------------------------------------------------------------------- stand-ins

HOST = "192.0.2.10"                  # documented TEST-NET; never a live address
SN = "CC2TESTSN0001"
TOKEN = "SIMULATED-NOT-A-TOKEN"      # the password/access-code stand-in, same role as in the adapter tests
CODE = "SIMULATED-CODE-99"           # a stand-in *configured* access code for the redaction test
DEST = "benchy-petg-2026-11-04.gcode"
SHA = "a" * 64
GATE_VARS = (srv.ENV_CC2_READ, srv.ENV_CC2_WRITE, "ORCA_CC2_URL", "ORCA_CC2_ACCESS_CODE",
             "ORCA_CC2_ACCESS_CODE_FILE", "ORCA_CC2_RECEIPTS_DIR")

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
    """No test in this module may inherit (or leak) a real opt-in, a real printer address or a
    real receipt store."""
    for var in GATE_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Anything that could reach a socket raises for the whole module; the guards count their
    own hits so 'zero network' is a counted fact, not a hope."""
    guards: dict = {"calls": []}

    def trip(name):
        def hit(*a, **kw):
            guards["calls"].append(name)
            raise AssertionError(f"network bypassed the gate: {name} was called")
        return hit

    # The three ways this adapter can reach out: the UDP discovery endpoint, the MQTT link, and
    # httpx (both the modern client and the raw connection it would fall back to). The event
    # loop's own self-pipe sockets stay untouched — nothing in the adapter uses them.
    monkeypatch.setattr(socket.socket, "connect", trip("socket.socket.connect"))
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
    """The transport seam itself: refused unless a test stands a fake in its place, and counted
    either way, so 'refused before any network' includes 'never even opened a session'."""
    opens: list = []

    async def refuse():
        opens.append("open")
        raise AssertionError("_cc2_open_transport must not run when the tool refuses")
    monkeypatch.setattr(srv, "_cc2_open_transport", refuse)
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
    """The observed capped reply — 100 entries of a 112-file listing, paging ignored — which the
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
    reach the seam. `no_open` stays the guard for 'refused before any session'."""
    opens: list = []

    async def fake_open():
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
    (M104) before the M109 S260 wait, an M6211 purge that carries T0 as a parameter and is not a
    tool select, the bed held at 80 by M140 with a later M190 S80 wait, and the end-block
    cooldowns the slicer writes last."""
    return (
        b"M106 S255\n"
        b"M104 S140\n"
        b"M140 S80\n"
        b"G28\n"
        b"M109 S260\n"
        b"M190 S80\n"
        b"T0\n"
        b"M6211 T0 S260 L200\n"
        b"G1 X10 Y5 E5 F1200\n"
        b"G1 Z0.2\n"
        b"M107\n"
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
    return srv._cc2_receipts.Cc2UploadReceipt(**d)


# --------------------------------------------------------------------------- 1. defaults: refused, zero network

ALL_TOOLS = (
    lambda: srv.cc2_status(),
    lambda: srv.cc2_list_files(),
    lambda: srv.cc2_upload_gcode(path="/nowhere/x.gcode", destination="x.gcode"),
    lambda: srv.cc2_start_print(filename=DEST, tool_to_tray={"0": "3"}, bed_clear=True),
)


async def test_default_no_opt_ins_refuses_all_four_with_zero_network(no_network, no_open):
    for call in ALL_TOOLS:
        out = await call()
        assert out["error"] == "disabled", out
        assert out["tool"] in {"cc2_status", "cc2_list_files", "cc2_upload_gcode",
                               "cc2_start_print"}
        assert "annotations are advisory" in out["hint"]
        json.dumps(out)  # the refusal itself must be plain JSON
    assert no_network["calls"] == []          # no socket, no MQTT link, no httpx — counted zero
    assert no_open == []                      # and the session seam was never even reached


@pytest.mark.parametrize("value", ["", "   ", "0", "false", "no", "off", "maybe"])
async def test_a_blank_or_falsy_opt_in_stays_off(value, no_network, no_open, monkeypatch):
    monkeypatch.setenv(srv.ENV_CC2_READ, value)
    monkeypatch.setenv(srv.ENV_CC2_WRITE, value)
    assert (await srv.cc2_status())["error"] == "disabled"
    assert (await srv.cc2_start_print(filename=DEST, tool_to_tray={"0": "3"},
                                      bed_clear=True))["error"] == "disabled"
    assert no_network["calls"] == [] and no_open == []


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on"])
def test_the_opt_in_reads_plain_truthy_words(value, monkeypatch):
    monkeypatch.setenv(srv.ENV_CC2_READ, value)
    assert srv._cc2_enabled(srv.ENV_CC2_READ) is True


# --------------------------------------------------------------------------- 2. gates are independent

async def test_read_opt_in_alone_opens_reads_only(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status())
    monkeypatch.setenv(srv.ENV_CC2_READ, "1")
    out = await srv.cc2_status()
    assert out["machine"]["state_word"] == "idle"
    assert transport.opens == ["open"]

    listed = await srv.cc2_list_files()
    assert listed["complete"] is True
    assert transport.opens == ["open", "open"]

    up = await srv.cc2_upload_gcode(path="x.gcode", destination="x.gcode")
    assert up["error"] == "disabled" and up["tool"] == "cc2_upload_gcode"
    st = await srv.cc2_start_print(filename=DEST, tool_to_tray={"0": "3"}, bed_clear=True)
    assert st["error"] == "disabled" and st["tool"] == "cc2_start_print"
    assert transport.opens == ["open", "open"]  # the writes refused before opening, too


async def test_write_opt_in_alone_refuses_reads_before_any_session(monkeypatch, no_open):
    monkeypatch.setenv(srv.ENV_CC2_WRITE, "1")
    assert (await srv.cc2_status())["error"] == "disabled"
    assert (await srv.cc2_list_files())["error"] == "disabled"
    assert no_open == []  # writes on does not smuggle the reads past their own gate
    # and a write with its gate open does reach the session (refusing there, not at the gate)
    transport = open_fake(monkeypatch, healthy_status())
    out = await srv.cc2_start_print(filename="no-receipt.gcode", tool_to_tray={"0": "3"},
                                    bed_clear=True)
    assert out["error"] == "protocol_error" and out["reason"] == "receipt_missing"
    assert transport.opens == ["open"]


# --------------------------------------------------------------------------- 3. validation before any session

async def test_a_bad_destination_refuses_before_any_session(no_open, monkeypatch):
    monkeypatch.setenv(srv.ENV_CC2_WRITE, "1")
    out = await srv.cc2_upload_gcode(path="x.gcode", destination="../evil.gcode")
    assert out["error"] == "protocol_error" and out["reason"] == "destination_invalid"
    assert no_open == []


@pytest.mark.parametrize("bad", [{"0": "3", "1": "2"}, {"a": "3"}, {"0": "x"}, {}, {"0": None},
                                 {"0": True}, {"-1": "3"}],
                         ids=["two-tools", "text-tool", "text-tray", "empty", "none", "bool",
                              "negative"])
async def test_a_ambiguous_mapping_refuses_before_any_session(bad, no_open, monkeypatch):
    monkeypatch.setenv(srv.ENV_CC2_WRITE, "1")
    out = await srv.cc2_start_print(filename=DEST, tool_to_tray=bad, bed_clear=True)
    assert out["error"] == "protocol_error" and out["reason"] == "tool_map"
    assert "Nothing was sent" in out["message"]
    assert no_open == []


# --------------------------------------------------------------------------- 4. status and listing, end to end

async def test_status_reports_parsed_state_and_never_a_raw_reply(monkeypatch):
    transport = open_fake(monkeypatch, healthy_status())
    monkeypatch.setenv(srv.ENV_CC2_READ, "1")
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
    assert transport.closed is True  # closed on the success path too


async def test_status_reports_uncertainty_without_guessing(monkeypatch):
    printing = copy.deepcopy(PRINTING_1002)
    open_fake(monkeypatch, healthy_status(method_1002=printing))
    monkeypatch.setenv(srv.ENV_CC2_READ, "1")
    out = await srv.cc2_status()
    assert out["machine"] == {"status": 2, "state_word": "printing"}
    assert out["job"]["filename"] == DEST and out["job"]["state"] == "printing"
    assert out["ready"]["ready_for_job"] is False
    assert DEST in out["ready"]["why_not"]
    json.dumps(out)


async def test_list_files_returns_a_proven_complete_listing_or_fails(monkeypatch):
    open_fake(monkeypatch, healthy_status())
    monkeypatch.setenv(srv.ENV_CC2_READ, "1")
    out = await srv.cc2_list_files()
    assert out["complete"] is True and out["total"] == 1 and out["pages_certified"] == 1
    assert out["files"] == [{"name": DEST, "size_bytes": 1234, "layers": 240, "tools": [0]}]
    json.dumps(out)

    # the observed capped reply: an incomplete listing comes back as an error naming the numbers
    open_fake(monkeypatch, healthy_status(method_1044=capped_page))
    bad = await srv.cc2_list_files()
    assert bad["error"] == "protocol_error"
    assert bad["reason"] == "pagination_mismatch"
    assert "files" not in bad  # never a partial list passed off as a listing

    # a listing that does not add up (92 reported, 100 seen) is refused the same way
    over = Listing([[file_entry(name=f"extra-{n}.gcode") for n in range(100)]], total=92)
    open_fake(monkeypatch, healthy_status(method_1044=over))
    bad2 = await srv.cc2_list_files()
    assert bad2["error"] == "protocol_error" and bad2["reason"] == "pagination_mismatch"
    assert "files" not in bad2


async def test_file_listing_reports_when_its_reply_is_truncated(monkeypatch):
    from types import SimpleNamespace
    files = [SimpleNamespace(filename=f"file-{i}.gcode", size=123, layers=None,
                             tools=(), created=None, print_seconds=None, filament_grams=None)
             for i in range(201)]
    async def inventory(_transport):
        return SimpleNamespace(files=files, storage="local", pages=3, total=len(files))
    monkeypatch.setattr(srv._cc2_state, "list_files", inventory)
    result = srv._cc2_out(await srv._cc2_files_reply(None))
    assert len(result["files"]) == 200
    assert result["truncated_in_reply"] is True


def test_valid_long_filename_is_not_truncated():
    name = "x" * 240 + ".gcode"
    assert srv._cc2_out(name) == name


async def test_start_rejects_non_boolean_leveling_before_network(monkeypatch, no_open):
    monkeypatch.setenv(srv.ENV_CC2_WRITE, "1")
    result = await srv.cc2_start_print(filename=DEST, tool_to_tray={"0": "3"},
                                       bed_clear=True, leveling="false")
    assert result["error"]
    assert not no_open


async def test_disabled_tools_do_not_load_printer_configuration(monkeypatch):
    monkeypatch.delenv(srv.ENV_CC2_READ, raising=False)
    monkeypatch.delenv(srv.ENV_CC2_WRITE, raising=False)
    def forbidden():
        raise AssertionError("disabled tools must not load credentials")
    monkeypatch.setattr(srv, "_cc2_configured_code", forbidden)
    for result in (await srv.cc2_status(), await srv.cc2_list_files(),
                   await srv.cc2_upload_gcode("/unused", "part.gcode"),
                   await srv.cc2_start_print("part.gcode", {}, bed_clear=False)):
        assert result["error"] == "disabled"


# --------------------------------------------------------------------------- 5. upload, end to end through the real adapter

async def test_upload_sends_parse_verifies_and_receipts_with_no_secrets(
        monkeypatch, tmp_path, receipts_dir, gcode_file):
    size = gcode_file.stat().st_size
    listing = Listing([[], [file_entry(size=size)]])  # collision check clean, verify agrees
    transport = open_fake(monkeypatch, ScriptedTransport({srv._cc2_state.METHOD_FILES: listing}))
    http = FakeHttp()
    monkeypatch.setattr(srv, "_cc2_upload_http_factory", http.factory)
    monkeypatch.setenv(srv.ENV_CC2_WRITE, "1")

    out = await srv.cc2_upload_gcode(path=str(gcode_file), destination=DEST)
    assert out["uploaded"] == DEST and out["size_bytes"] == size
    assert out["material"] == "PETG" and out["tool_index"] == 0
    assert out["nozzle_temp_c"] == {"first_layer": 260.0, "steady": 260.0}
    assert out["bed_temp_c"] == {"first_layer": 80.0, "steady": 80.0}
    # the 140 °C non-extruding preheat rides as a commanded setpoint, never as an extrusion temp
    assert out["extrusion_nozzle_temps_c"] == [260.0]
    assert out["commanded_nozzle_setpoints_c"] == [140.0, 260.0]
    assert "cc2_start_print" in out["next"]
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
    stored = srv._cc2_receipts.Cc2ReceiptStore(receipts_dir).load(SN, DEST)
    assert stored is not None and stored.material == "PETG"
    assert stored.extrusion_nozzle_temps == (260.0,)
    assert (stored.reported_size_bytes, stored.sent_bytes) == (size, size)

    dumped = json.dumps(out)
    assert TOKEN not in dumped            # the credential never rides out
    assert str(gcode_file) not in dumped  # the local path is not echoed back
    assert "X-Token" not in dumped and "Content-Range" not in dumped


async def test_upload_refuses_a_collision_without_retrying_or_overwriting(
        monkeypatch, tmp_path, receipts_dir, gcode_file):
    size = gcode_file.stat().st_size
    listing = Listing([[file_entry(size=size)]])  # the name already exists, always will
    transport = open_fake(monkeypatch, ScriptedTransport({srv._cc2_state.METHOD_FILES: listing}))
    http = FakeHttp()
    monkeypatch.setattr(srv, "_cc2_upload_http_factory", http.factory)
    monkeypatch.setenv(srv.ENV_CC2_WRITE, "1")
    out = await srv.cc2_upload_gcode(path=str(gcode_file), destination=DEST)
    assert out["error"] == "protocol_error" and out["reason"] == "collision"
    assert http.puts == [] and http.closes == 0   # never a byte, let alone a second try
    assert transport.methods() == [srv._cc2_state.METHOD_FILES]
    assert "Nothing was retried" not in out["message"]  # the refusal itself is clean
    assert srv._cc2_receipts.Cc2ReceiptStore(receipts_dir).load(SN, DEST) is None


async def test_an_unreadable_file_refuses_before_any_byte_and_says_so(
        monkeypatch, tmp_path, receipts_dir):
    transport = open_fake(monkeypatch, healthy_status())
    monkeypatch.setenv(srv.ENV_CC2_WRITE, "1")
    out = await srv.cc2_upload_gcode(path=str(tmp_path / "gone.gcode"), destination=DEST)
    assert out["error"] == "protocol_error" and out["reason"] == "source_unreadable"
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
    srv._cc2_receipts.Cc2ReceiptStore(receipts_dir).save(make_receipt())
    return receipts_dir


async def test_start_maps_levels_and_never_collapses_accepted_with_running(
        monkeypatch, with_receipt):
    transport = open_fake(monkeypatch, start_transport())
    monkeypatch.setenv(srv.ENV_CC2_WRITE, "1")
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
    monkeypatch.setenv(srv.ENV_CC2_WRITE, "1")
    # JSON objects carry string keys and models send string values; the exact integer mapping
    # still goes out, and an explicit leveling request flips printer_check on the wire.
    out = await srv.cc2_start_print(filename=DEST, tool_to_tray={" 0 ": " #3 "}, bed_clear=True,
                                    leveling=True, observe_running=False)
    assert out["started_accepted"] is True
    assert transport.params_for(srv._cc2_start.METHOD_START)["config"] == {
        "slot_map": [{"t": 0, "canvas_id": 0, "tray_id": 3}], "printer_check": True}


async def test_start_observes_running_as_a_separate_fact_and_strips_reply_noise(
        monkeypatch, with_receipt):
    dirty = copy.deepcopy(PRINTING_1002)
    dirty["print_status"]["filename"] = DEST + "\x1bEVIL"  # an escape rode in with the name
    transport = open_fake(monkeypatch, start_transport(
        method_1002=deque([IDLE_1002, dirty])))
    monkeypatch.setenv(srv.ENV_CC2_WRITE, "1")
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
    monkeypatch.setenv(srv.ENV_CC2_WRITE, "1")
    out = await srv.cc2_start_print(filename=DEST, tool_to_tray={"0": "3"}, bed_clear=True)
    assert out["observed_running"] is True and out["observed_state"] == "printing"
    assert any("Observed printing" in n for n in out["notes"])


async def test_start_carries_a_bed_warning_from_the_caller_without_claiming_provenance(
        monkeypatch, with_receipt):
    open_fake(monkeypatch, start_transport())
    monkeypatch.setenv(srv.ENV_CC2_WRITE, "1")
    out = await srv.cc2_start_print(filename=DEST, tool_to_tray={"0": "3"}, bed_clear=True,
                                    bed_warning="First layer may not stick on this plate.")
    assert out["bed_warning"] == "First layer may not stick on this plate."
    assert out["bed_warning_provenance"].startswith("carried_from_slice_unverified")
    assert any("valid slice is not proof" in n for n in out["notes"])
    json.dumps(out)


async def test_bed_clear_false_never_lets_a_start_command_leave(monkeypatch, with_receipt):
    transport = open_fake(monkeypatch, start_transport())
    monkeypatch.setenv(srv.ENV_CC2_WRITE, "1")
    out = await srv.cc2_start_print(filename=DEST, tool_to_tray={"0": "3"}, bed_clear=False)
    assert out["error"] == "protocol_error" and out["reason"] == "bed_not_clear"
    assert srv._cc2_start.METHOD_START not in transport.methods()
    # only reads happened before the refusal, and the session still closed
    assert set(transport.methods()) <= {srv._cc2_state.METHOD_STATUS,
                                        srv._cc2_state.METHOD_CANVAS,
                                        srv._cc2_state.METHOD_FILES}


async def test_a_missing_receipt_refuses_without_any_command(monkeypatch, receipts_dir):
    transport = open_fake(monkeypatch, start_transport())
    monkeypatch.setenv(srv.ENV_CC2_WRITE, "1")
    out = await srv.cc2_start_print(filename=DEST, tool_to_tray={"0": "3"}, bed_clear=True)
    assert out["error"] == "protocol_error" and out["reason"] == "receipt_missing"
    assert srv._cc2_start.METHOD_START not in transport.methods()


async def test_a_lost_start_answer_is_ambiguous_and_never_resent(monkeypatch, with_receipt):
    transport = open_fake(monkeypatch, start_transport(
        method_1020=srv._cc2_transport.Cc2TimeoutError(
            "protocol_error", "simulated: the printer never answered the start",
            reason="timeout", ambiguous=True),
        method_1002=deque([IDLE_1002, IDLE_1002])))
    monkeypatch.setenv(srv.ENV_CC2_WRITE, "1")
    out = await srv.cc2_start_print(filename=DEST, tool_to_tray={"0": "3"}, bed_clear=True)
    assert out["status"] == "ambiguous"
    assert out["started_accepted"] is False and out["observed_running"] is False
    assert transport.methods().count(srv._cc2_start.METHOD_START) == 1  # never resent
    assert any("not be resent" in n or "not been resent" in n or "not resent" in n
               for n in out["notes"])


# --------------------------------------------------------------------------- 7. failures are structured and sanitised

async def test_a_session_failure_is_a_structured_refusal_and_closes_anyway(monkeypatch):
    async def refuse():
        raise srv.PrinterError("not_reachable",
                               "simulated: the printer did not answer discovery")
    monkeypatch.setattr(srv, "_cc2_open_transport", refuse)
    monkeypatch.setenv(srv.ENV_CC2_READ, "1")
    out = await srv.cc2_status()
    assert out == {"error": "not_reachable",
                   "message": "simulated: the printer did not answer discovery"}


async def test_adapter_errors_come_back_sanitised(monkeypatch, tmp_path, receipts_dir):
    open_fake(monkeypatch, healthy_status(
        method_1044=srv._cc2_state.Cc2DataError(
            "protocol_error",
            "simulated bad frame \x1b[31mfrom\x07 the printer", reason="inventory_malformed")))
    monkeypatch.setenv(srv.ENV_CC2_READ, "1")
    out = await srv.cc2_list_files()
    assert out["error"] == "protocol_error" and out["reason"] == "inventory_malformed"
    assert "\x1b" not in out["message"] and "\x07" not in out["message"]
    json.dumps(out)


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
    for name in ("cc2_status", "cc2_list_files"):
        schema = tools[name].input_schema
        assert schema["type"] == "object" and schema["properties"] == {}


def test_annotations_classify_the_four_cc2_tools():
    tools = srv.mcp._tool_manager._tools
    assert set(srv._TOOL_ANNOTATIONS) == set(tools)  # the completeness table still matches
    for name in ("cc2_status", "cc2_list_files"):
        ann = tools[name].annotations
        assert ann.read_only_hint is True and ann.destructive_hint is None
        assert ann.open_world_hint is True and ann.idempotent_hint is None
    for name in ("cc2_upload_gcode", "cc2_start_print"):
        ann = tools[name].annotations
        assert ann.read_only_hint is False and ann.destructive_hint is False
        assert ann.open_world_hint is True
        assert ann.idempotent_hint is None  # writes carry no idempotency hint — they are not
