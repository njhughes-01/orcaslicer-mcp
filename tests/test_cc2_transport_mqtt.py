"""The CC2 transport against a *real* MQTT client. `stub_broker` below speaks MQTT 3.1.1 on
loopback well enough for paho, so what goes on the wire is proved against the library that has to
accept it. The fake link in `tests/test_cc2_transport.py` cannot prove that: it accepted any
subscribe shape, which is how one list of bare topic strings — a shape real paho answers with
`ValueError: too many values to unpack` — reached a real Centauri Carbon 2 and turned three ordinary
reads into generic "Error executing tool" replies on 2026-11-04.

Loopback only, no printer and no internet: the broker binds 127.0.0.1, the transport is pointed at
127.0.0.1, and discovery is answered from a scripted object so no UDP probe leaves either. QoS 0
only, no persistence, no will. The one credential this protocol carries (the access code, sent as
the MQTT password) is deliberately never parsed or stored by the broker: it checks the user name
against the documented ``elegoo`` and forgets the rest.

The last test drives `printer/cc2_pool.py` over the same stub broker. That belongs here rather than
only in the pool's own mocked tests because the fault pooling fixes — one registration per tool call,
against a broker that takes about four clients — is only visible to a client the broker counts: a
connect, a registration and a subscription pair are real MQTT protocol here, not a fake's method
call."""
from __future__ import annotations

import asyncio
import json
import time

import aiomqtt
import pytest

from orcaslicer_mcp.printer import cc2_pool
from orcaslicer_mcp.printer import cc2_transport as cc2
from orcaslicer_mcp.printer.errors import PrinterError

HOST = "127.0.0.1"
SN = "CC2MQTTTEST01"


def _matches(topic_filter: str, topic: str) -> bool:
    """MQTT's own matching, the two wildcards and nothing else."""
    if topic_filter == topic:
        return True
    f, t = topic_filter.split("/"), topic.split("/")
    for i, part in enumerate(f):
        if part == "#":
            return True
        if i >= len(t):
            return False
        if part != "+" and part != t[i]:
            return False
    return len(f) == len(t)


class StubBroker:
    """A toy MQTT 3.1.1 broker: it accepts a CONNECT (or refuses it with `connack_rc`, the way the
    printer's roster and access code do), answers SUBSCRIBE with the granted filters it recorded,
    and delivers PUBLISH between connections by matching filters. `responder` plays the printer:
    registration is answered on the topic the client just subscribed, and a command gets its
    `method -> result` answer back on the same client's api_response topic."""

    def __init__(self):
        self.server: asyncio.Server | None = None
        self.port: int | None = None
        self.events: list[tuple[str, str]] = []      # ordered: connect/subscribe/publish/disconnect
        self.subs: list[tuple[str, str, int]] = []   # (client_id, topic filter, qos)
        self.publishes: list[tuple[str, str, bytes]] = []
        self.connack_rc: int | None = None           # None accepts; 3/4/5 refuse as the printer does
        self.refuse_subscribes = False               # answer SUBACK with 0x80 and deliver nothing
        self.responder: dict | None = None
        self.username_ok = False                     # the documented user name, checked, not stored
        self._conns: dict[asyncio.StreamWriter, tuple[str, list[str]]] = {}

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._serve, HOST, 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()
        for writer in list(self._conns):
            writer.close()
        for _ in range(50):  # let every handler see its EOF and retire, so no task outlives a test
            if not self._conns:
                break
            await asyncio.sleep(0.02)
        self._conns.clear()

    # -- the wire -----------------------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                b1 = (await reader.readexactly(1))[0]
                length, mult = 0, 1
                while True:
                    byte = (await reader.readexactly(1))[0]
                    length += (byte & 0x7F) * mult
                    mult *= 128
                    if not byte & 0x80:
                        break
                body = await reader.readexactly(length) if length else b""
                if b1 & 0xF0 == 0x10:
                    self._connect(body, writer)
                elif b1 & 0xF0 == 0x80:
                    self._subscribe(body, writer)
                elif b1 & 0xF0 == 0x30:
                    await self._publish(b1, body, writer)
                elif b1 & 0xF0 == 0xC0:
                    writer.write(b"\xd0\x00")  # PINGRESP
                    await writer.drain()
                elif b1 & 0xF0 == 0xE0:
                    self.events.append(("disconnect", self._conns.get(writer, ("", []))[0]))
                    return
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError, ValueError):
            pass
        finally:
            self._conns.pop(writer, None)
            writer.close()

    def _connect(self, body: bytes, writer: asyncio.StreamWriter) -> None:
        i = 2 + int.from_bytes(body[:2], "big")        # protocol name
        flags = body[i + 1]
        i += 4                                          # level, flags, keepalive
        size = int.from_bytes(body[i:i + 2], "big")
        client_id = body[i + 2:i + 2 + size].decode()
        i += 2 + size
        if flags & 0x80:                                # a user name follows; the password after it
            usize = int.from_bytes(body[i:i + 2], "big")
            self.username_ok = body[i + 2:i + 2 + usize].decode() == cc2.MQTT_USERNAME
        self._conns[writer] = (client_id, [])
        self.events.append(("connect", client_id))
        rc = 0 if self.connack_rc is None else self.connack_rc
        writer.write(b"\x20\x02\x00" + bytes([rc]))
        return None

    def _subscribe(self, body: bytes, writer: asyncio.StreamWriter) -> None:
        pid, rest, granted = body[:2], body[2:], []
        while rest:
            size = int.from_bytes(rest[:2], "big")
            granted.append((rest[2:2 + size].decode(), rest[2 + size]))
            rest = rest[3 + size:]
        codes = [0x80] * len(granted) if self.refuse_subscribes else [q for _t, q in granted]
        if not self.refuse_subscribes:
            client_id, filters = self._conns[writer]
            filters.extend(t for t, _q in granted)
            self.subs.extend((client_id, t, q) for t, q in granted)
            self.events.extend([("subscribe", t) for t, _q in granted])
        payload = pid + bytes(codes or [0x00])
        writer.write(b"\x90" + bytes([len(payload)]) + payload)

    async def _publish(self, first: int, body: bytes, writer: asyncio.StreamWriter) -> None:
        size = int.from_bytes(body[:2], "big")
        topic = body[2:2 + size].decode()
        payload = body[2 + size + (2 if (first >> 1) & 0x03 else 0):]
        await self._on_publish(self._conns.get(writer, ("", []))[0], topic, payload)

    async def _on_publish(self, from_id: str, topic: str, payload: bytes) -> None:
        self.publishes.append((from_id, topic, payload))
        self.events.append(("publish", topic))
        sn, _, rest = topic.split("/", 2)
        if sn != "elegoo":
            return
        try:
            msg = json.loads(payload)
        except ValueError:
            return
        if topic == f"elegoo/{SN}/api_register" and self.responder is not None:
            rid = msg.get("request_id")
            await self.publish(f"elegoo/{SN}/{rid}/register_response",
                               json.dumps({"client_id": msg.get("client_id"), "error": "ok"}))
        elif self.responder is not None and isinstance(msg, dict) and "method" in msg:
            result = self.responder.get(msg["method"]) if isinstance(self.responder, dict) else None
            if result is not None:
                await self.publish(f"elegoo/{SN}/{rest.split('/')[0]}/api_response",
                                   json.dumps({"id": msg.get("id"), "method": msg["method"],
                                               "result": result}))

    async def publish(self, topic: str, payload: str) -> int:
        """Hand a message to every connection that may have it; the count says how many did."""
        sent = 0
        for writer, (_cid, filters) in list(self._conns.items()):
            if self.refuse_subscribes or not any(_matches(f, topic) for f in filters):
                continue
            packet = json_pack(topic, payload)
            writer.write(packet)
            await writer.drain()
            sent += 1
        return sent

    async def wait_until(self, what, timeout_s: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_s
        while not what():
            if time.monotonic() > deadline:
                raise AssertionError("the stub broker never saw what the transport was expected to send")
            await asyncio.sleep(0.02)

    def saw_publish(self, topic: str, payload: str | None = None) -> bool:
        return any(t == topic and (payload is None or p == payload.encode())
                   for _c, t, p in self.publishes)


def json_pack(topic: str, payload: str) -> bytes:
    body = len(topic).to_bytes(2, "big") + topic.encode() + payload.encode()
    out, n = bytearray(), len(body)
    while True:
        b = n % 128
        n //= 128
        out.append(b | 0x80 if n else b)
        if not n:
            break
    return bytes([0x30]) + bytes(out) + body


# --------------------------------------------------------------------------- a session on loopback


def discovery() -> cc2.Cc2Discovery:
    """The scripted discovery answer: LAN-only mode, no access code, so the MQTT password the stub
    broker sees is the documented no-code one and no credential of Nathan's is anywhere here."""
    return cc2.Cc2Discovery(host=HOST, sn=SN, model="Centauri Carbon 2", name=None,
                            token_status=0, lan_status=1)


@pytest.fixture()
async def broker(monkeypatch):
    b = StubBroker()
    b.responder = {}          # answers registration; a test replaces it to answer commands too
    await b.start()
    monkeypatch.setattr(cc2, "MQTT_PORT", b.port)
    try:
        yield b
    finally:
        await b.stop()


async def open_session(**kw):
    """One real session: the production link factory, the pinned loopback address, a scripted
    discovery answer instead of a UDP probe, and the heartbeat off unless the test asks."""
    kw.setdefault("heartbeat_interval_s", 0)
    return await cc2.open_transport(cc2.load_config({cc2.ENV_URL: HOST}), discovery=discovery(), **kw)


# --------------------------------------------------------------------------- the wire contract


async def test_the_reply_topics_go_on_the_wire_one_subscribe_packet_each_before_asking(broker):
    """The 2026-11-04 fault, proved against the library that refused it: two awaited single-topic
    subscriptions, both answered, before the first publish. A list of bare strings never gets here."""
    t = await open_session()
    try:
        assert broker.username_ok  # the documented user name, and the password never inspected
        assert [f for _c, f, _q in broker.subs] == [t.register_topic, t.response_topic]
        assert {q for *_x, q in broker.subs} == {0}  # the printer's broker is not asked for more
        assert broker.events[:3] == [("connect", t.client_id), ("subscribe", t.register_topic),
                                     ("subscribe", t.response_topic)]
        assert broker.events[3] == ("publish", f"elegoo/{SN}/api_register")
        assert t.last_error is None
    finally:
        await t.close()
    # The broker reads its DISCONNECT a loop turn later, so wait for the event rather than trusting
    # the ordering of a close we just asked for.
    await broker.wait_until(lambda: bool(broker.events) and broker.events[-1][0] == "disconnect")
    assert broker.events[-1] == ("disconnect", t.client_id)  # a clean close, DISCONNECT and all


async def test_paho_refuses_a_list_of_bare_topic_strings_and_takes_one_topic_per_call(broker):
    """The contract pin the fake link could not give: real paho reads a list as (topic, qos) pairs,
    so a list of topic strings raises ValueError before the broker is asked anything, while one topic
    string per awaited call is accepted. If paho ever changes its mind, this test says so."""
    topics = [f"elegoo/{SN}/a", f"elegoo/{SN}/b"]
    async with aiomqtt.Client(HOST, port=broker.port, username=cc2.MQTT_USERNAME,
                              identifier="0clirca001", protocol=aiomqtt.ProtocolVersion.V311,
                              keepalive=cc2.MQTT_KEEPALIVE_S, clean_session=True, timeout=5.0) as c:
        with pytest.raises(ValueError) as e:
            await c.subscribe(topics, timeout=2.0)
        assert "too many values to unpack" in str(e.value)
        assert broker.subs == []  # nothing reached the broker: the form was refused on the way out
        await c.subscribe(topics[0], timeout=2.0)
        await c.subscribe([(topics[1], 0)], timeout=2.0)
    assert [f for _c, f, _q in broker.subs] == topics


async def test_a_command_round_trips_on_the_topics_the_broker_granted(broker):
    """Not a fake's self-report: the stub broker only ever delivers a message to a connection whose
    granted filter matches, so an answer arriving proves the subscription is live on the wire."""
    broker.responder = {1045: {"hello": "world"}}
    t = await open_session()
    try:
        assert await t.request(1045, {"file": "gcode.3mf"}, timeout_s=5) == {"hello": "world"}
        assert broker.saw_publish(f"elegoo/{SN}/{t.client_id}/api_request")
    finally:
        await t.close()


async def test_the_heartbeat_and_its_pong_cross_the_real_broker(broker):
    t = await open_session(heartbeat_interval_s=0.05)
    try:
        api_request = f"elegoo/{SN}/{t.client_id}/api_request"
        await broker.wait_until(lambda: broker.saw_publish(api_request, '{"type": "PING"}'))
        t._last_pong = time.monotonic() - 30  # 30 s of silence; the manual's budget is 65 s
        assert await broker.publish(f"elegoo/{SN}/{t.client_id}/api_response",
                                    '{"type": "PONG"}') == 1
        await broker.wait_until(lambda: t.pong_age_s() is not None and t.pong_age_s() < 1)
        assert t.last_error is None  # and the PONG was routed, not merely received
    finally:
        await t.close()


@pytest.mark.parametrize("rc,code,reason", [(4, "auth_rejected", None), (5, "auth_rejected", None),
                                            (3, "not_reachable", "busy")])
async def test_a_real_connack_refusal_keeps_its_own_kind(broker, rc, code, reason):
    """The printer's own refusals, answered by a real broker, so the rc is read the way paho hands
    it over rather than the way a fake guesses it."""
    broker.connack_rc = rc
    with pytest.raises(PrinterError) as e:
        await open_session()
    assert e.value.code == code
    assert reason is None or e.value.details["reason"] == reason
    assert broker.subs == []  # a refused connect never got as far as subscribing


async def test_a_broker_that_grants_nothing_times_out_honestly_and_is_a_known_limitation(broker):
    """An open limitation, pinned instead of papered over: paho hands a SUBACK of 0x80 back as a
    *return value* and stays connected, so this transport cannot see a refused subscription at all.
    What the caller gets is the registration timeout — honest about the silence, and not about the
    cause. Reading those reason codes is the follow-up this records, not something to guess at."""
    broker.refuse_subscribes = True
    with pytest.raises(cc2.Cc2TimeoutError) as e:
        await open_session(registration_attempts=1)
    assert e.value.details["reason"] == "timeout"
    assert len(broker.subs) == 0 and any(t == f"elegoo/{SN}/api_register"
                                         for _c, t, _p in broker.publishes)


# --------------------------------------------------------------------------- the shared session, on a
# client the broker counts


def pooled() -> cc2_pool.Cc2SessionPool:
    """The pool as the MCP server builds it — the transport's own config reader, and an open that does
    the real thing — pointed at this stub broker instead of a printer. The heartbeat stays off, as
    `open_session` defaults it, so what the broker sees is only what the code asked for."""
    async def open_real(config):
        return await open_session(), None
    return cc2_pool.Cc2SessionPool(lambda: cc2.load_config({cc2.ENV_URL: HOST}), open_real)


async def test_one_registration_answers_several_borrows_of_the_shared_session(broker):
    """The fault pooling exists for, on the wire: four tool calls used to mean four clients on a broker
    that takes about four, and the fourth was refused by an idle printer. Through the pool, two
    sequential borrows and a concurrent pair are one CONNECT, one registration and one subscription
    pair, each answer still reaches the caller that asked, and the close is one DISCONNECT."""
    broker.responder = {1045: {"thumbnail": "AA=="}, 1046: {"filename": "cat.gcode"},
                        1001: {"machine_model": "Centauri Carbon 2"}, 2005: {"canvas_info": {}}}
    pool = pooled()
    try:
        first = await pool.acquire()
        assert await first.transport.request(1045, {"file_name": "cat.gcode"}, timeout_s=5) \
            == {"thumbnail": "AA=="}
        await first.release()

        second = await pool.acquire()                    # the next tool call, minutes later in real life
        assert await second.transport.request(1046, {"filename": "cat.gcode"}, timeout_s=5) \
            == {"filename": "cat.gcode"}

        both = await asyncio.gather(second.transport.request(1001, timeout_s=5),
                                    second.transport.request(2005, timeout_s=5))
        assert both == [{"machine_model": "Centauri Carbon 2"}, {"canvas_info": {}}]
        assert pool.holds == 1                           # the borrow in progress; nothing else is in
    finally:
        await second.release()
        await pool.close()

    client_id = first.transport.client_id
    connects = [e for e in broker.events if e[0] == "connect"]
    registers = [p for _c, t, p in broker.publishes if t == f"elegoo/{SN}/api_register"]
    commands = [p for _c, t, p in broker.publishes if t == f"elegoo/{SN}/{client_id}/api_request"]
    assert connects == [("connect", client_id)]          # one MQTT client for all four requests
    assert len(registers) == 1 and len(commands) == 4
    assert [f for _c, f, _q in broker.subs] == [first.transport.register_topic,
                                                first.transport.response_topic]
    ids = [json.loads(p.decode())["id"] for p in commands]
    assert len(set(ids)) == 4                            # correlated by id, which is why sharing is safe
    assert first.transport is second.transport and first.transport.closed is True
    await broker.wait_until(lambda: broker.events[-1] == ("disconnect", client_id))
