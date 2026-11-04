"""The CC2 camera candidate (method 1042) against scripted replies, in the house style of the history
and information tests: no socket is opened, the transport is a fake that records every method, the exact
params it was given and the timeout each request asked for, and the guards counting their own hits prove
the module never reached for the network it was handed an address for.

**The wire.** One 1042, params exactly the empty object (Elegoo's own LAN client sends ``{method: 1042}``
with no params key at all), waited for 10 s (that client's class default for a caller that passes no
override, against this server's 10 s transport default — the longer of the two, as cc2_canvas records),
asked once and never in another spelling. ``enable`` is the trap this pins hardest: the community manual
and the Home Assistant integration treat 1042 as a camera *toggle*, so every test asserts the word
appears nowhere on the wire, alongside ``video_url``, ``page`` and the never-called 1054.

**The vetting.** Every address the printer sends is checked before it is repeated, and each refusal is
tested twice: that it refuses, and that the printer's own string appears nowhere in the message, the
hint, the details or a rendered reply. That is the whole point of the module — a login in an authority,
a token-shaped query parameter, an unparseable or over-long string and a disallowed scheme all get
reported as facts without the credential inside them riding out. Nothing is ever composed either: an
empty or null address is reported as the printer's own silence, never as the
``http://<ip>:8080/?action=stream`` the third-party integration builds for itself.

**The second request.** One read-only 1001 for the printer's own ``camera_connected``/``video_connections``
/``max_video_connections``, asked after an answer and after a refusal alike, reported beside the address
rather than as a condition, never asked twice, and never asked after a timeout or a dead session. A 1001
that refuses leaves the 1042 answer standing with a note.

**And the posture.** The last section fails the moment this candidate is exposed before a real 1042 has
ever been captured: no camera tool is registered, no camera name is in the CC2 roster, and neither
``server.py``, ``README.md`` nor the manifests mention it. Every reply says ``verified_on_this_printer:
false``, because no CC2 has answered 1042 for this project, and because on this firmware a bare 1042 has
never been proven free of side effects on the stream."""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import socket
from pathlib import Path

import pytest

from orcaslicer_mcp.printer import cc2_camera as cam
from orcaslicer_mcp.printer import cc2_state as st
from orcaslicer_mcp.printer.cc2_transport import Cc2TimeoutError, PrinterError

ROOT = Path(__file__).resolve().parents[1]

HOST = "192.0.2.10"                  # documented TEST-NET, like the whole lane; never a live address
SN = "CC2TESTSN0001"
FAKE_CODE = "SIMULATED-NOT-A-CODE"   # a stand-in credential, proven never to ride any reply
GOOD = f"http://{HOST}:8080/?action=stream"

ATTRS_OK = {"error_code": 0, "camera_connected": True, "video_connections": 0,
            "max_video_connections": 1, "sn": SN, "machine_model": "Centauri Carbon 2"}


class FakeTransport:
    """A session that records and answers: one entry per method, a callable answering per call, an
    exception raising as given, and an unscripted method answering ``{}`` — which is the no-answer trap
    these tests watch for."""

    def __init__(self, replies=None, *, host=HOST, sn=SN):
        self.host, self.sn = host, sn
        self.replies = dict(replies or {})
        self.requests: list[tuple[int, dict, float | None]] = []
        self.closed = False

    async def request(self, method: int, params: dict | None = None, *, timeout_s=None) -> dict:
        self.requests.append((method, copy.deepcopy(params or {}), timeout_s))
        reply = self.replies.get(method, {})
        if callable(reply):
            reply = reply(params or {})
        if isinstance(reply, BaseException):
            raise reply
        return copy.deepcopy(reply)

    def methods(self) -> list[int]:
        return [m for m, _, _ in self.requests]

    def one(self, method: int) -> tuple[dict, float | None]:
        """The single call this method got — params and the timeout it asked for. Asserting through this
        is what makes "asked exactly once" part of every wire-shape test."""
        calls = [(p, t) for m, p, t in self.requests if m == method]
        assert len(calls) == 1, f"expected exactly one {method}, got {self.methods()}"
        return calls[0]

    def wire_text(self) -> str:
        """Everything that went out, as text — so a test can say a word like ``enable`` was never sent."""
        return json.dumps([{"m": m, "p": p, "t": t} for m, p, t in self.requests])


def transport(camera=None, attrs=ATTRS_OK, **extra) -> FakeTransport:
    """A session with the 1042 answer scripted (and the 1001 answer beside it). ``camera=None`` means
    the 1042 answer carries no fields at all, which is a trap of its own, so pass ``camera=...``."""
    replies = {}
    if camera is not None:
        replies[cam.METHOD_LIVING_VIDEO_URL] = camera
    if attrs is not None:
        replies[1001] = attrs
    replies.update(extra)
    return FakeTransport(replies)


def answered(url, **extra) -> dict:
    return {"error_code": 0, "url": url, **extra}


async def read(tr: FakeTransport, **kw) -> dict:
    return (await cam.read_camera(tr, **kw)).as_dict()


def everything(reply: dict, error: Exception | None = None) -> str:
    """Every byte a caller could see, so "nowhere in the reply" means the reply, its details and the
    error's own message and hint together."""
    blob = json.dumps(reply, default=str)
    if error is not None:
        blob += str(error) + str(getattr(error, "hint", "")) + json.dumps(
            getattr(error, "details", {}), default=str)
    return blob


# --------------------------------------------------------------------------- the wire


async def test_one_1042_with_no_parameters_and_the_official_window():
    tr = transport(camera=answered(GOOD))
    await read(tr)
    params, timeout = tr.one(cam.METHOD_LIVING_VIDEO_URL)
    assert params == {}
    assert timeout == cam.CAMERA_TIMEOUT_S == 10.0


async def test_the_request_never_carries_a_toggle_or_any_other_key():
    # 1042 is "Toggle camera" in the community manual, so the params being exactly {} is the security
    # property, not a style choice: nothing here may switch a stream on or off.
    tr = transport(camera=answered(GOOD))
    await read(tr)
    assert tr.wire_text().count(f'"m": {cam.METHOD_LIVING_VIDEO_URL}') == 1
    for word in ("enable", "video_url", "page", "action=stream", "stream"):
        assert word not in tr.wire_text()


async def test_the_1054_stream_start_is_never_asked_and_nothing_else_rides_the_wire():
    tr = transport(camera=answered(GOOD))
    await read(tr)
    assert tr.methods() == [cam.METHOD_LIVING_VIDEO_URL, 1001]
    assert cam.METHOD_VIDEO_STREAM_WS == 1054           # named for the record, never sent
    assert 1054 not in tr.methods()


async def test_the_facts_request_can_be_switched_off_entirely():
    tr = transport(camera=answered(GOOD))
    await read(tr, with_camera_facts=False)
    assert tr.methods() == [cam.METHOD_LIVING_VIDEO_URL]


async def test_a_timeout_propagates_and_asks_nothing_further(no_network):
    # the transport's own words are the honest ones: the request may have landed, and a second question
    # on a session that just failed to answer is a second request nobody established.
    tr = transport(camera=Cc2TimeoutError("protocol_error", "no answer in 10s", reason="timeout",
                                           ambiguous=True))
    with pytest.raises(Cc2TimeoutError):
        await cam.read_camera(tr)
    assert tr.methods() == [cam.METHOD_LIVING_VIDEO_URL]
    assert no_network["calls"] == []


async def test_a_dead_session_is_asked_nothing_further(no_network):
    tr = transport(camera=PrinterError("not_reachable", "the session is over"))
    with pytest.raises(PrinterError):
        await cam.read_camera(tr)
    assert tr.methods() == [cam.METHOD_LIVING_VIDEO_URL]
    assert no_network["calls"] == []


# --------------------------------------------------------------------------- the envelope


async def test_an_absent_error_code_is_the_success_this_family_answers_with():
    tr = transport(camera={"url": GOOD})
    out = await read(tr)
    assert out["url"] == GOOD


async def test_a_nonzero_error_code_is_the_printers_own_no_with_its_number_kept():
    tr = transport(camera={"error_code": 1003, "url": GOOD})
    with pytest.raises(st.Cc2DataError) as caught:
        await read(tr)
    assert caught.value.details["reason"] == "declined"
    assert caught.value.details["error_code"] == 1003
    # and the printer's own camera facts ride with the refusal, because "no camera attached" is the
    # useful half of a refusal
    assert caught.value.details["printer_camera_facts"] == {"camera_connected": True,
                                                            "video_connections": 0,
                                                            "max_video_connections": 1}
    assert tr.methods() == [cam.METHOD_LIVING_VIDEO_URL, 1001]


@pytest.mark.parametrize("code", ["0", True, 1.5, None, {}])
async def test_an_unreadable_error_code_is_malformed_never_zero(code):
    tr = transport(camera={"error_code": code, "url": GOOD})
    with pytest.raises(st.Cc2DataError) as caught:
        await read(tr)
    assert caught.value.details["reason"] == "envelope_malformed"


@pytest.mark.parametrize("result", [[], "url", 7, ""])
async def test_a_reply_that_is_not_an_object_refuses(result):
    tr = FakeTransport({cam.METHOD_LIVING_VIDEO_URL: result})
    with pytest.raises(st.Cc2DataError) as caught:
        await read(tr)
    assert caught.value.details["reason"] == "camera_malformed"


async def test_a_reply_that_is_null_is_not_an_object_either():
    tr = FakeTransport({cam.METHOD_LIVING_VIDEO_URL: None})
    with pytest.raises(st.Cc2DataError) as caught:
        await read(tr)
    assert caught.value.details["reason"] == "camera_malformed"


# --------------------------------------------------------------------------- the address, vetted


@pytest.mark.parametrize("url,host,kind,port", [
    (f"http://{HOST}:8080/?action=stream", HOST, "lan", 8080),
    (f"https://{HOST}/stream", HOST, "lan", None),
    (f"rtsp://{HOST}:8554/live", HOST, "lan", 8554),
    ("ws://10.88.88.136:8080/stream", "10.88.88.136", "lan", 8080),
    ("wss://172.20.0.5/stream", "172.20.0.5", "lan", None),
    ("rtmp://10.0.0.9/live", "10.0.0.9", "lan", None),
    ("http://127.0.0.1:8080/", "127.0.0.1", "loopback", 8080),
])
async def test_a_repeatable_address_is_carried_with_what_it_turned_out_to_be(url, host, kind, port):
    tr = transport(camera=answered(url))
    out = await read(tr)
    assert out["url"] == url
    assert (out["url_host"], out["url_host_kind"], out["url_port"]) == (host, kind, port)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://192.0.2.10/stream",
    "javascript:alert(1)",
    "data:image/jpeg;base64,AAAA",
])
async def test_a_scheme_that_is_not_a_camera_address_refuses_without_repeating_it(url):
    tr = transport(camera=answered(url))
    with pytest.raises(st.Cc2DataError) as caught:
        await read(tr)
    assert caught.value.details["reason"] == "url_scheme"
    assert url not in everything({}, caught.value)


async def test_a_host_only_address_refuses_because_it_cannot_be_vetted():
    # the older websocket family answers a bare host the client prefixes with http:// itself. That is
    # evidence about that family, not a URL this server may echo: without a scheme, urlsplit parks a
    # "user:pass@host" in .path, so nothing about the string has been checked.
    tr = transport(camera=answered(f"{HOST}:8080/?action=stream"))
    with pytest.raises(st.Cc2DataError) as caught:
        await read(tr)
    assert caught.value.details["reason"] == "url_unparseable"
    assert HOST not in everything({}, caught.value)


async def test_a_login_in_the_authority_is_withheld_entirely():
    secret = f"http://admin:{FAKE_CODE}@{HOST}:8080/stream"
    tr = transport(camera=answered(secret))
    with pytest.raises(st.Cc2DataError) as caught:
        await read(tr)
    err = caught.value
    assert err.details["reason"] == "url_has_login"
    blob = everything({}, err)
    assert FAKE_CODE not in blob and HOST not in blob and "admin" not in blob


async def test_a_credential_shaped_query_parameter_keeps_its_name_and_loses_its_value():
    url = f"http://{HOST}:8080/stream?token={FAKE_CODE}&x=1"
    tr = transport(camera=answered(url))
    out = await read(tr)
    assert FAKE_CODE not in everything(out)
    assert out["url_withheld_parameters"] == ["token"]
    assert out["url_rewritten"] is True
    assert "token" in out["url"] and "x=1" in out["url"]


async def test_a_credential_wearing_a_longer_name_is_withheld_too():
    url = f"http://{HOST}:8080/stream?video_token={FAKE_CODE}"
    tr = transport(camera=answered(url))
    out = await read(tr)
    assert FAKE_CODE not in everything(out)
    assert out["url_withheld_parameters"] == ["video_token"]


async def test_an_ordinary_query_survives_untouched():
    tr = transport(camera=answered(GOOD))
    out = await read(tr)
    assert out["url"] == GOOD and out["url_rewritten"] is False
    assert "action=stream" in out["url"]


async def test_an_over_long_address_is_refused_by_its_length_not_its_contents():
    long_url = f"http://{HOST}:8080/" + "a" * 300
    tr = transport(camera=answered(long_url))
    with pytest.raises(st.Cc2DataError) as caught:
        await read(tr)
    assert caught.value.details["reason"] == "url_too_long"
    assert caught.value.details["url_characters"] > cam._MAX_URL_LEN
    assert "aaaa" not in everything({}, caught.value)


@pytest.mark.parametrize("url", ["http://[", "http://[::1:8080/", "http://192.0.2.10:8080/a\nb",
                                 "http://192.0.2.10:8080/a\x07b", "http://\x07"])
async def test_an_unparseable_address_is_described_not_repeated(url):
    tr = transport(camera=answered(url))
    with pytest.raises(st.Cc2DataError) as caught:
        await read(tr)
    assert caught.value.details["reason"] == "url_unparseable"
    assert HOST not in everything({}, caught.value)


async def test_a_port_that_is_not_a_number_refuses_without_repeating_the_host():
    tr = transport(camera=answered(f"http://{HOST}:80o0/stream"))
    with pytest.raises(st.Cc2DataError) as caught:
        await read(tr)
    assert caught.value.details["reason"] == "url_unparseable"
    assert HOST not in everything({}, caught.value)


@pytest.mark.parametrize("url", ["http:///stream", "http://:8080/stream"])
async def test_an_address_with_no_host_is_not_an_address(url):
    tr = transport(camera=answered(url))
    with pytest.raises(st.Cc2DataError) as caught:
        await read(tr)
    assert caught.value.details["reason"] == "url_no_host"


@pytest.mark.parametrize("value", [8080, True, ["http", HOST], {"host": HOST}])
async def test_an_address_that_is_not_text_refuses_with_its_type_named(value):
    tr = transport(camera=answered(value))
    with pytest.raises(st.Cc2DataError) as caught:
        await read(tr)
    assert caught.value.details["reason"] == "url_not_text"
    assert HOST not in everything({}, caught.value)


@pytest.mark.parametrize("blank", [{"url": ""}, {"url": None}, {"url": "   "}])
async def test_a_printers_silence_is_reported_as_silence_and_never_as_a_built_address(blank):
    # the integration's fallback is f"http://{printer_ip}:8080/?action=stream". Building that here
    # would be handing out an address the printer never sent, so the reply must not contain one.
    tr = transport(camera={"error_code": 0, **blank})
    out = await read(tr)
    assert out["url"] is None
    assert "url_absent" in out
    assert f"{HOST}:8080" not in everything(out)
    assert tr.methods() == [cam.METHOD_LIVING_VIDEO_URL, 1001]


async def test_a_url_that_arrived_null_says_so_rather_than_reading_as_an_empty_string():
    tr = transport(camera={"error_code": 0, "url": None})
    out = await read(tr)
    assert any("arrived as null" in note for note in out["notes"])


# --------------------------------------------------------------------------- which field it came in


async def test_no_url_field_is_said_with_its_key_names_and_not_their_values():
    tr = transport(camera={"error_code": 0, "hostname": FAKE_CODE, "streaming": True})
    with pytest.raises(st.Cc2DataError) as caught:
        await read(tr)
    assert caught.value.details["reason"] == "no_url_field"
    assert "hostname" in caught.value.details["keys"]
    assert FAKE_CODE not in everything({}, caught.value)


@pytest.mark.parametrize("name", ["video_url", "VideoUrl"])
async def test_the_other_familys_field_name_is_named_and_never_read_as_this_one(name):
    # 1042 with `video_url` (integration) and the websocket family's `VideoUrl` are both evidence about
    # which shape this firmware speaks. Fitting either into `url` would be the guess this module exists
    # to avoid, and echoing it would repeat an unvetted string.
    tr = transport(camera={"error_code": 0, name: f"http://user:{FAKE_CODE}@{HOST}:8080/x"})
    with pytest.raises(st.Cc2DataError) as caught:
        await read(tr)
    err = caught.value
    assert err.details["reason"] == "other_family_shape"
    assert name in err.message
    assert FAKE_CODE not in everything({}, err) and HOST not in everything({}, err)
    assert tr.methods() == [cam.METHOD_LIVING_VIDEO_URL, 1001]   # no re-ask in another spelling


# --------------------------------------------------------------------------- what kind of place it is


async def test_a_public_address_is_carried_with_a_warning_because_nothing_here_connects_to_it():
    tr = transport(camera=answered("http://93.184.216.34:8080/stream"))
    out = await read(tr)
    assert out["url_host_kind"] == "public"
    assert any("public address" in n for n in out["notes"])
    assert out["url_same_host_as_printer"] is False


async def test_a_name_is_reported_as_a_name_and_is_never_looked_up(no_network):
    tr = transport(camera=answered("http://printer-camera.lan:8080/stream"))
    out = await read(tr)
    assert out["url_host_kind"] == "name_not_resolved"
    assert any("not looked up" in n for n in out["notes"])
    assert no_network["calls"] == []                 # no DNS: the guard counted nothing


async def test_the_address_is_said_whether_it_is_the_printer_this_session_is_on():
    same = await read(transport(camera=answered(GOOD)))
    other = await read(transport(camera=answered("http://192.0.2.99:8080/")))
    assert (same["url_same_host_as_printer"], other["url_same_host_as_printer"]) == (True, False)


async def transport_with_no_host_named() -> dict:
    tr = FakeTransport({cam.METHOD_LIVING_VIDEO_URL: answered(GOOD), 1001: ATTRS_OK}, host=None)
    return (await cam.read_camera(tr)).as_dict()


async def test_a_transport_that_names_no_host_leaves_the_comparison_unknown():
    out = await transport_with_no_host_named()
    assert out["url_same_host_as_printer"] is None


async def test_the_mjpeg_port_earns_the_note_about_one_viewer_and_no_authentication():
    out = await read(transport(camera=answered(GOOD)))
    assert any("8080" in n and "single viewer" in n for n in out["notes"])
    assert any("no authentication" in n or "requires no auth" in n for n in out["notes"])


# --------------------------------------------------------------------------- the 1001 read-back


async def test_the_printers_own_camera_facts_sit_beside_the_address_as_a_second_source():
    out = await read(transport(camera=answered(GOOD)))
    assert out["printer_camera_facts"] == {"camera_connected": True, "video_connections": 0,
                                           "max_video_connections": 1}
    assert "1001" in out["printer_camera_facts_source"]


async def test_the_facts_are_asked_exactly_once_and_never_twice_in_one_call():
    tr = transport(camera=answered(GOOD))
    await read(tr)
    assert tr.methods().count(1001) == 1


async def test_a_camera_the_printer_does_not_report_is_said_out_loud():
    out = await read(transport(camera=answered(GOOD), attrs={**ATTRS_OK, "camera_connected": False}))
    assert out["printer_camera_facts"]["camera_connected"] is False
    assert any("no camera is connected" in w for w in out["warnings"])


async def test_at_its_viewer_limit_the_warning_says_a_viewer_may_be_displaced():
    out = await read(transport(camera=answered(GOOD),
                               attrs={**ATTRS_OK, "video_connections": 1, "max_video_connections": 1}))
    assert any("video connections in use" in w for w in out["warnings"])


async def test_a_free_slot_is_not_reported_as_a_full_one():
    out = await read(transport(camera=answered(GOOD),
                               attrs={**ATTRS_OK, "video_connections": 0, "max_video_connections": 1}))
    assert not [w for w in out["warnings"] if "in use" in w]


async def test_a_maximum_of_zero_is_no_ceiling_rather_than_a_division_by_nothing():
    out = await read(transport(camera=answered(GOOD),
                               attrs={**ATTRS_OK, "video_connections": 0, "max_video_connections": 0}))
    assert not [w for w in out["warnings"] if "in use" in w]


async def test_a_1001_that_refuses_never_costs_the_answer_it_sat_beside():
    out = await read(transport(camera=answered(GOOD), attrs={"error_code": 1003}))
    assert out["url"] == GOOD
    assert "refused" in out["printer_camera_facts_note"]
    # silence is silence: an unanswered camera_connected is left out of the facts rather than shown
    # as False, which would read as "no camera" to anything downstream
    assert "camera_connected" not in out["printer_camera_facts"]


async def test_a_1001_that_answered_no_fields_is_still_called_a_refusal_not_an_empty_profile():
    out = await read(transport(camera=answered(GOOD), attrs={}))
    assert out["url"] == GOOD
    assert "refused" in out["printer_camera_facts_note"]


async def test_a_1001_that_broke_internally_is_reported_as_silence_not_as_a_camera_off():
    # the second read must never swallow or replace the first one's answer, whatever it does
    tr = FakeTransport({cam.METHOD_LIVING_VIDEO_URL: answered(GOOD),
                        1001: RuntimeError("the reader broke")})
    out = (await cam.read_camera(tr)).as_dict()
    assert out["url"] == GOOD
    assert "no readable answer" in out["printer_camera_facts_note"]


async def test_a_refused_1042_still_gets_one_facts_request_and_no_more():
    tr = transport(camera={"error_code": 5}, attrs=ATTRS_OK)
    with pytest.raises(st.Cc2DataError):
        await read(tr)
    assert tr.methods() == [cam.METHOD_LIVING_VIDEO_URL, 1001]


# --------------------------------------------------------------------------- what every reply must say


async def test_nothing_is_claimed_as_verified_and_the_toggle_conflict_is_said_every_time():
    out = await read(transport(camera=answered(GOOD)))
    assert out["verified_on_this_printer"] is False
    assert "Toggle" in out["unverified"] or "1042" in out["unverified"]
    assert any("side effects" in w for w in out["warnings"])
    assert any("fetched" in n and "not" in n for n in out["notes"])


async def test_a_reply_is_json_safe_and_its_refusals_are_short_enough_to_survive_the_reply_cap():
    out = await read(transport(camera=answered(GOOD)))
    assert json.loads(json.dumps(out))["method"] == cam.METHOD_LIVING_VIDEO_URL
    tr = transport(camera=answered(f"http://admin:{'secret' * 20}@{HOST}:8080/a" + "b" * 300))
    with pytest.raises(st.Cc2DataError) as caught:
        await read(tr)
    assert len(caught.value.message) <= cam.NOTE_CAP


# --------------------------------------------------------------------------- the candidate posture


def test_no_camera_tool_is_registered():
    # the module is an internal candidate until a real 1042 has been captured on Nathan's firmware —
    # and until the toggle question is settled, an exposed camera tool could switch a stream by asking
    import orcaslicer_mcp.server as srv

    names = set(srv.mcp._tool_manager._tools)
    assert not [n for n in names if n.startswith("cc2_") and "camera" in n]
    assert not [n for n in srv.CC2_TOOL_NAMES if "camera" in n.lower()]
    assert not [n for n in srv._TOOL_ANNOTATIONS if n.startswith("cc2_") and "camera" in n]


@pytest.mark.parametrize("relative", ["src/orcaslicer_mcp/server.py", "README.md",
                                      "lhm.plugin.json", "mcpb/manifest.json",
                                      "scripts/gen_lhm_tools.py"])
def test_the_candidate_is_not_offered_as_a_supported_capability(relative):
    text = (ROOT / relative).read_text(encoding="utf-8")
    for name in ("cc2_camera", "cc2_camera_url", "living_video", "1042"):
        assert name not in text, f"{relative} promises {name}"


def test_the_module_reaches_for_no_transport_of_its_own():
    source = Path(inspect.getsourcefile(cam)).read_text(encoding="utf-8")
    imports = [line.strip() for line in source.splitlines()
               if line.strip().startswith(("import ", "from "))]
    forbidden = ("socket", "httpx", "subprocess", "aiohttp", "requests", "urllib.request",
                 "urllib.error", "asyncio.tasks", "multiprocessing", "ctypes", "tempfile")
    for line in imports:
        assert not any(f"import {name}" in line or f"from {name}" in line for name in forbidden), line
    assert "from urllib.parse" in source            # parsing text is all the urllib it wants
    assert "open_transport" not in source and "@mcp.tool" not in source
    # the only wire it touches is the session it is handed, and the only method it ever asks for
    # besides the read-only 1001 is 1042
    assert "METHOD_VIDEO_STREAM_WS" in source


def test_the_documented_exposure_steps_are_the_ones_a_live_test_has_to_take():
    server = (ROOT / "src/orcaslicer_mcp/server.py").read_text(encoding="utf-8")
    for structure in ("CC2_TOOL_NAMES", "_TOOL_ANNOTATIONS", "_OPEN_WORLD_TOOLS", "@mcp.tool()"):
        assert structure in server


# --------------------------------------------------------------------------- the network guards


@pytest.fixture
def no_network(monkeypatch):
    """Anything that could reach a socket or a name raises for the whole test, and counts its attempts,
    so 'this module never fetched the address it was handed' is a measured zero."""
    calls = []

    def trip(what):
        def go(*a, **k):
            calls.append(what)
            raise AssertionError(f"the camera candidate touched the network: {what}")
        return go

    monkeypatch.setattr(socket.socket, "connect", trip("socket.socket.connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", trip("socket.socket.connect_ex"))
    monkeypatch.setattr(socket.socket, "sendto", trip("socket.socket.sendto"))
    monkeypatch.setattr(socket, "getaddrinfo", trip("socket.getaddrinfo"))
    monkeypatch.setattr(socket, "create_connection", trip("socket.create_connection"))
    monkeypatch.setattr(asyncio, "open_connection", trip("asyncio.open_connection"))
    return {"calls": calls}
