"""The CC2 transport against a fake broker: discovery modes, bounded registration, correlated
requests, heartbeat, cleanup, credentials. The fake stands in for the printer's own MQTT broker;
nothing here opens a socket. Protocol facts come from the community CC2 protocol documentation."""
from __future__ import annotations

import asyncio
import json
import re

import pytest
from aiomqtt.exceptions import MqttConnectError

from orcaslicer_mcp.printer import cc2_transport as cc2
from orcaslicer_mcp.printer.errors import PrinterError

HOST = "192.0.2.10"
SN = "CC2TESTSN0001"
SECRET = "a7xPrivateCode"
DISC_OK = {"id": 0, "result": {"host_name": "Centauri Carbon 2", "machine_model": "Centauri Carbon 2",
                               "sn": SN, "token_status": 0, "lan_status": 1}}


# --------------------------------------------------------------------------- fake broker


class FakeMessage:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload


class FakeLink:
    """Stands in for aiomqtt.Client: records the connect parameters, publishes hand off to the
    broker, and `messages` yields what the broker delivers to this link's subscriptions."""

    def __init__(self, broker, host, password, client_id):
        self.broker = broker
        self.params = {"host": host, "password": password, "client_id": client_id}
        self.subs: list[str] = []
        self.published: list[tuple[str, dict | str]] = []
        self.closed = False
        self._queue: asyncio.Queue = asyncio.Queue()

    async def __aenter__(self):
        await self.broker.on_connect(self)
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    async def subscribe(self, topics):
        self.subs.extend([topics] if isinstance(topics, str) else topics)

    async def publish(self, topic, payload):
        self.published.append((topic, json.loads(payload) if isinstance(payload, str) else payload))
        await self.broker.on_publish(self, topic, self.published[-1][1])

    async def deliver(self, topic, payload):
        if topic in self.subs:
            await self._queue.put(FakeMessage(topic, payload))

    @property
    def messages(self):
        async def gen():
            while True:
                item = await self._queue.get()
                if item is None:
                    raise ConnectionError("broker closed the connection")
                yield item
        return gen()

    def kill(self):
        self._queue.put_nowait(None)


class FakeBroker:
    """`replies` scripts the registration answers per attempt ("ok", "fail", "too many clients",
    None = no answer; the last entry repeats). `auth_rc` refuses the MQTT connect. `responder`
    answers command requests: a dict of method -> result, or a callable(link, request)."""

    def __init__(self, replies=None, auth_rc=None, responder=None):
        self.replies = ["ok"] if replies is None else list(replies)
        self.auth_rc = auth_rc
        self.responder = responder
        self.links: list[FakeLink] = []
        self.reg_attempts = 0

    async def on_connect(self, link):
        if self.auth_rc is not None:
            raise MqttConnectError(self.auth_rc)
        self.links.append(link)

    async def on_publish(self, link, topic, msg):
        sn = topic.split("/")[1]
        if topic == f"elegoo/{sn}/api_register":
            attempt = self.reg_attempts
            self.reg_attempts += 1
            reply = self.replies[min(attempt, len(self.replies) - 1)]
            if reply is None:
                return
            await link.deliver(f"elegoo/{sn}/{msg['request_id']}/register_response",
                               json.dumps({"client_id": msg["client_id"], "error": reply}))
            return
        if self.responder is None:
            return
        resp = self.responder(link, msg) if callable(self.responder) else self._auto(msg)
        if resp is not None:
            client_id = topic.split("/")[2]
            await link.deliver(f"elegoo/{sn}/{client_id}/api_response", json.dumps(resp))

    def _auto(self, msg):
        method = msg.get("method")
        if isinstance(self.responder, dict) and method in self.responder:
            return {"id": msg["id"], "method": method, "result": self.responder[method]}
        return None


def config(access_code=None, url=HOST):
    env = {cc2.ENV_URL: url}
    if access_code is not None:
        env[cc2.ENV_CODE] = access_code
    return cc2.load_config(env)


def discovery(token_status=0, lan_status=1, sn=SN):
    return cc2.Cc2Discovery(host=HOST, sn=sn, model="Centauri Carbon 2", name=None,
                            token_status=token_status, lan_status=lan_status)


async def open_session(broker, cfg=None, *, disc=None, **kw):
    kw.setdefault("registration_retry_delay_s", 0.0)
    transport = await cc2.open_transport(
        cfg or config(), discovery=disc or discovery(),
        link_factory=lambda host, password, client_id: FakeLink(broker, host, password, client_id), **kw)
    transport.broker = broker  # test-side handle only
    return transport


# --------------------------------------------------------------------------- config


async def test_config_needs_the_address():
    with pytest.raises(PrinterError) as e:
        cc2.load_config({})
    assert e.value.code == "not_configured" and cc2.ENV_URL in e.value.message


async def test_config_address_rejects_logins_paths_and_ports():
    for bad in (f"http://user:{SECRET}@{HOST}", f"http://{HOST}:1884", f"http://{HOST}/api"):
        with pytest.raises(PrinterError) as e:
            cc2.load_config({cc2.ENV_URL: bad})
        assert e.value.code == "not_configured"


async def test_config_reads_a_private_code_file(tmp_path):
    f = tmp_path / "code"
    f.write_text(SECRET + "\n")
    f.chmod(0o600)
    cfg = cc2.load_config({cc2.ENV_URL: HOST, cc2.ENV_CODE_FILE: str(f)})
    assert cfg.access_code.value == SECRET  # trailing newline stripped


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o620])
async def test_code_file_must_not_be_group_or_world_readable(tmp_path, mode):
    f = tmp_path / "code"
    f.write_text(SECRET)
    f.chmod(mode)
    with pytest.raises(PrinterError) as e:
        cc2.load_config({cc2.ENV_URL: HOST, cc2.ENV_CODE_FILE: str(f)})
    assert e.value.code == "not_configured" and str(f) in e.value.message
    assert SECRET not in e.value.message  # the complaint quotes the path, never the code


async def test_code_file_missing_or_not_a_file(tmp_path):
    env = {cc2.ENV_URL: HOST, cc2.ENV_CODE_FILE: str(tmp_path / "absent")}
    with pytest.raises(PrinterError) as e:
        cc2.load_config(env)
    assert e.value.code == "not_configured"
    with pytest.raises(PrinterError) as e:
        cc2.load_config({cc2.ENV_URL: HOST, cc2.ENV_CODE_FILE: str(tmp_path)})
    assert e.value.code == "not_configured"


async def test_the_two_code_sources_must_not_both_be_set(tmp_path):
    f = tmp_path / "code"
    f.write_text("file-code")
    f.chmod(0o600)
    with pytest.raises(PrinterError) as e:
        cc2.load_config({cc2.ENV_URL: HOST, cc2.ENV_CODE_FILE: str(f), cc2.ENV_CODE: "env-code"})
    assert e.value.code == "not_configured"


async def test_never_a_secret_in_a_repr():
    cfg = config(SECRET)
    for shown in (repr(cfg), str(cfg), repr(cfg.access_code), str(cfg.access_code)):
        assert SECRET not in shown
    assert "<redacted>" in repr(cfg)


# --------------------------------------------------------------------------- discovery


async def _stub_udp(monkeypatch, reply=None, error=None, record=None):
    async def exchange(host, payload, timeout_s):
        if record is not None:
            record.append((host, json.loads(payload)))
        if error is not None:
            raise error
        return reply
    monkeypatch.setattr(cc2, "_udp_exchange", exchange)


async def test_discovery_reads_identity_and_modes(monkeypatch):
    await _stub_udp(monkeypatch, reply=DISC_OK)
    d = await cc2.discover(HOST)
    assert (d.sn, d.token_status, d.lan_status) == (SN, 0, 1)


async def test_discovery_probes_only_the_configured_address(monkeypatch):
    sent = []
    await _stub_udp(monkeypatch, reply=DISC_OK, record=sent)
    await cc2.discover(HOST)
    assert sent == [(HOST, {"id": 0, "method": 7000})]  # directed, never a broadcast


async def test_discovery_cloud_mode_is_unsupported(monkeypatch):
    bad = json.loads(json.dumps(DISC_OK)); bad["result"]["lan_status"] = 0
    await _stub_udp(monkeypatch, reply=bad)
    with pytest.raises(PrinterError) as e:
        await cc2.discover(HOST)
    assert e.value.code == "unsupported_connection" and "LAN Only" in e.value.message


async def test_discovery_unknown_token_mode_is_unsupported(monkeypatch):
    bad = json.loads(json.dumps(DISC_OK)); bad["result"]["token_status"] = 2
    await _stub_udp(monkeypatch, reply=bad)
    with pytest.raises(PrinterError) as e:
        await cc2.discover(HOST)
    assert e.value.code == "unsupported_connection"


@pytest.mark.parametrize("mutate", [
    lambda r: r.pop("result"),
    lambda r: r["result"].update(sn="evil/#+topic"),
    lambda r: r["result"].update(token_status=True),
    lambda r: r["result"].update(lan_status="1"),
])
async def test_discovery_rejects_malformed_replies(monkeypatch, mutate):
    bad = json.loads(json.dumps(DISC_OK)); mutate(bad)
    await _stub_udp(monkeypatch, reply=bad)
    with pytest.raises(PrinterError) as e:
        await cc2.discover(HOST)
    assert e.value.code == "protocol_error"


async def test_discovery_answers_are_bounded(monkeypatch):
    sent = []
    await _stub_udp(monkeypatch, error=TimeoutError(), record=sent)
    with pytest.raises(PrinterError) as e:
        await cc2.discover(HOST)
    assert e.value.code == "not_reachable"
    assert len(sent) == cc2.DISCOVERY_ATTEMPTS  # retries, but never forever


# --------------------------------------------------------------------------- address safety

PUB = "8.8.8.8"  # a public address: never a printer, never a probe target
PUB2 = "93.184.216.34"  # a second public one — unlike TEST-NET (192.0.2/24, 203.0.113/24: fair
                        # fake-test stands-ins), these must never be accepted


def never_resolve(monkeypatch):
    """A literal address that reaches the resolver is both a bug and a rebinding door."""
    calls = []

    async def lookup(host):
        calls.append(host)
        raise AssertionError("a literal address must never be handed to the resolver")
    monkeypatch.setattr(cc2, "_lookup", lookup)
    return calls


def fake_dns(monkeypatch, *answers):
    """Stand in for the system resolver: every call records, and every call answers the same list."""
    calls = []

    async def lookup(host):
        calls.append(host)
        return list(answers)
    monkeypatch.setattr(cc2, "_lookup", lookup)
    return calls


@pytest.mark.parametrize("addr", ["192.0.2.10", "10.16.69.190", "172.16.5.5", "192.168.1.50",
                                  "127.0.0.1", "::1", "fd12:3456::1", "[fd12:3456::1]"])
async def test_private_literal_addresses_are_pinned_as_they_stand(monkeypatch, addr):
    calls = never_resolve(monkeypatch)
    probed = []
    await _stub_udp(monkeypatch, reply=DISC_OK, record=probed)
    d = await cc2.discover(addr)
    assert d.host == addr.strip("[]") and calls == []  # no lookup, nothing that could be rebound
    assert probed[0][0] == addr.strip("[]")            # the probe went to that address and no other


@pytest.mark.parametrize("addr,why", [
    (PUB, "public"), (PUB2, "public"), ("2001:4860:4860::8888", "public"),
    ("::ffff:8.8.8.8", "public"),  # an IPv4 public dressed as IPv6 is still public
    ("224.0.0.1", "multicast"), ("239.255.255.250", "multicast"), ("ff02::1", "multicast"),
    ("0.0.0.0", "unspecified"), ("::", "unspecified"),
    ("169.254.1.1", "link-local"), ("fe80::1", "link-local"),
    ("255.255.255.255", "broadcast"),
])
async def test_forbidden_addresses_are_refused_before_anything_is_asked(monkeypatch, addr, why):
    never_resolve(monkeypatch)
    probed = []
    await _stub_udp(monkeypatch, reply=DISC_OK, record=probed)
    with pytest.raises(PrinterError) as e:
        await cc2.discover(addr)
    assert e.value.code == "not_configured" and why in e.value.message
    assert probed == []  # refused before the first packet left


async def test_a_public_literal_never_opens_a_session(monkeypatch):
    never_resolve(monkeypatch)
    broker = FakeBroker()
    with pytest.raises(PrinterError) as e:
        await open_session(broker, config(url=PUB2))
    assert e.value.code == "not_configured" and broker.links == []


async def test_a_name_resolving_public_is_refused(monkeypatch):
    calls = fake_dns(monkeypatch, PUB)
    probed = []
    await _stub_udp(monkeypatch, reply=DISC_OK, record=probed)
    with pytest.raises(PrinterError) as e:
        await cc2.discover("printer.example")
    assert e.value.code == "not_configured" and e.value.details["reason"] == "unsafe_address"
    assert "public" in e.value.message and "rebinding" in e.value.message
    assert calls == ["printer.example"] and probed == []


async def test_mixed_dns_answers_fail_closed(monkeypatch):
    # one friendly answer must not launder the hostile one, whatever order they come in
    fake_dns(monkeypatch, "192.0.2.10", PUB)
    probed = []
    await _stub_udp(monkeypatch, reply=DISC_OK, record=probed)
    with pytest.raises(PrinterError) as e:
        await cc2.discover("printer.example")
    assert e.value.code == "not_configured" and e.value.details["reason"] == "unsafe_address"
    assert "public" in e.value.message and probed == []


async def test_mixed_dns_answers_fail_closed_the_other_way_round(monkeypatch):
    fake_dns(monkeypatch, PUB, "192.0.2.10")
    with pytest.raises(PrinterError) as e:
        await cc2.discover("printer.example")
    assert e.value.details["reason"] == "unsafe_address"


async def test_ambiguous_dns_answers_fail_closed(monkeypatch):
    fake_dns(monkeypatch, "192.0.2.10", "192.0.2.11")  # both private, but which one is the printer?
    probed = []
    await _stub_udp(monkeypatch, reply=DISC_OK, record=probed)
    with pytest.raises(PrinterError) as e:
        await cc2.discover("printer.example")
    assert e.value.code == "not_configured" and e.value.details["reason"] == "ambiguous_address"
    assert "192.0.2.11" in e.value.message  # tells you what it saw, rather than picking blindly
    assert probed == []


async def test_an_answer_of_duplicates_stays_one_address(monkeypatch):
    fake_dns(monkeypatch, "192.0.2.10", "192.0.2.10")  # the same answer twice is still one printer
    t = await cc2.resolve_target("printer.example")
    assert t.ip == "192.0.2.10"


async def test_empty_dns_answer_reports_cleanly(monkeypatch):
    fake_dns(monkeypatch)
    with pytest.raises(PrinterError) as e:
        await cc2.discover("printer.example")
    assert e.value.code == "not_configured" and "no address" in e.value.message


async def test_an_unparseable_dns_answer_fails_closed(monkeypatch):
    fake_dns(monkeypatch, "not-an-address")
    probed = []
    await _stub_udp(monkeypatch, reply=DISC_OK, record=probed)
    with pytest.raises(PrinterError) as e:
        await cc2.discover("printer.example")
    assert e.value.code == "not_configured" and probed == []


async def test_the_pinned_address_serves_discovery_and_mqtt_alike(monkeypatch):
    calls = []

    async def rebinding_dns(host):
        # private the one time asked, public ever after: a second lookup would hand the session
        # to an address we refused — the rebinding this single-lookup-and-pin rule exists to stop.
        calls.append(host)
        return ["192.0.2.10"] if len(calls) == 1 else [PUB]
    monkeypatch.setattr(cc2, "_lookup", rebinding_dns)
    probed = []
    await _stub_udp(monkeypatch, reply=DISC_OK, record=probed)
    broker = FakeBroker()

    def factory(host, password, client_id):
        return FakeLink(broker, host, password, client_id)
    t = await cc2.open_transport(config(url="printer.example"), registration_retry_delay_s=0.0,
                                 link_factory=factory)
    try:
        assert calls == ["printer.example"]  # looked up exactly once, before discovery
        assert probed == [("192.0.2.10", {"id": 0, "method": 7000})]  # discovery spoke to the pin
        assert broker.links[0].params["host"] == "192.0.2.10"  # and so does MQTT
        assert t.host == "192.0.2.10"
    finally:
        await t.close()


async def test_the_configured_name_survives_as_display_only(monkeypatch):
    fake_dns(monkeypatch, "192.0.2.10")
    t = await cc2.resolve_target("printer.example")
    assert t.ip == "192.0.2.10" and t.display == "printer.example"
    assert t.shown == "printer.example (192.0.2.10)"  # what a human may be told
    probed = []
    await _stub_udp(monkeypatch, reply=DISC_OK, record=probed)
    d = await cc2.discover("printer.example")
    assert d.host == "192.0.2.10" and d.display == "printer.example"
    assert probed[0][0] == "192.0.2.10"  # the name is a word, never a destination


async def test_a_display_name_is_shown_when_it_is_safe_and_hidden_when_it_is_not(monkeypatch):
    fake_dns(monkeypatch, PUB)
    with pytest.raises(PrinterError) as e:
        await cc2.resolve_target("printer.example")
    assert "printer.example" in e.value.message  # safe to show
    with pytest.raises(PrinterError) as e:
        await cc2.resolve_target("bad\nname")
    assert "bad" not in e.value.message and "\n" not in e.value.message  # never an echo


async def test_address_refusals_never_leak_the_access_code(monkeypatch):
    fake_dns(monkeypatch, PUB)
    cfg = config(SECRET, url="printer.example")
    with pytest.raises(PrinterError) as e:
        await cc2.open_transport(cfg)
    err = e.value
    assert SECRET not in err.message and SECRET not in str(err) and SECRET not in err.hint
    assert all(SECRET not in str(v) for v in err.details.values())


# --------------------------------------------------------------------------- password and registration


async def test_default_password_only_in_no_access_code_mode():
    broker = FakeBroker()
    async with await open_session(broker) as t:
        assert t.broker.links[0].params["password"] == cc2.DEFAULT_ACCESS_CODE
        assert broker.links[0].params["host"] == HOST
        assert re.fullmatch(r"0cli[0-9a-f]{6}", broker.links[0].params["client_id"])


async def test_a_configured_code_wins_over_the_documented_default():
    broker = FakeBroker()
    async with await open_session(broker, config(SECRET), disc=discovery(token_status=0)):
        assert broker.links[0].params["password"] == SECRET


async def test_access_code_mode_refuses_to_guess():
    factory_calls = []

    def factory(host, password, client_id):
        factory_calls.append(password)
        return FakeLink(FakeBroker(), host, password, client_id)

    with pytest.raises(PrinterError) as e:
        await cc2.open_transport(config(), discovery=discovery(token_status=1), link_factory=factory)
    assert e.value.code == "auth_required" and cc2.ENV_CODE in e.value.message
    assert factory_calls == []  # fail closed: no silent default, no silent fallback


async def test_access_code_mode_uses_the_code():
    broker = FakeBroker()
    async with await open_session(broker, config(SECRET), disc=discovery(token_status=1)):
        assert broker.links[0].params["password"] == SECRET


async def test_registration_handshake_publishes_the_pair_on_the_documented_topic():
    broker = FakeBroker()
    async with await open_session(broker) as t:
        link = broker.links[0]
        topic, msg = link.published[0]
        assert topic == f"elegoo/{SN}/api_register"
        assert set(msg) == {"client_id", "request_id"} and msg["client_id"] == link.params["client_id"]
        assert f"elegoo/{SN}/{msg['request_id']}/register_response" in link.subs
        assert f"elegoo/{SN}/{t.client_id}/api_response" in link.subs


@pytest.mark.parametrize("error,reason", [("fail", "refused"), ("too many clients", "client_limit")])
async def test_registration_refusals_are_final(error, reason):
    broker = FakeBroker(replies=[error])
    with pytest.raises(cc2.Cc2RegistrationError) as e:
        await open_session(broker)
    assert e.value.details["reason"] == reason and e.value.code == "protocol_error"
    assert broker.reg_attempts == 1  # an explicit refusal is never retried
    assert broker.links[0].closed


async def test_lost_reply_then_client_slot_exhaustion():
    broker = FakeBroker(replies=[None, "too many clients"])
    with pytest.raises(cc2.Cc2RegistrationError) as e:
        await open_session(broker)
    assert e.value.details["reason"] == "client_limit"
    assert broker.reg_attempts == 2 and broker.links[0].closed


async def test_registration_timeout_is_bounded_and_pauses_between_attempts():
    broker = FakeBroker(replies=[None])
    slept = []

    async def nap(s):
        slept.append(s)

    with pytest.raises(cc2.Cc2TimeoutError) as e:
        await open_session(broker, registration_attempts=2, registration_retry_delay_s=0.5, sleep=nap)
    assert e.value.code == "protocol_error" and e.value.details["reason"] == "timeout"
    assert broker.reg_attempts == 2 and slept == [0.5] and broker.links[0].closed


@pytest.mark.parametrize("rc,code,reason", [(4, "auth_rejected", None), (5, "auth_rejected", None),
                                            (3, "not_reachable", "busy")])
async def test_connect_refusals_are_classified(rc, code, reason):
    broker = FakeBroker(auth_rc=rc)
    with pytest.raises(PrinterError) as e:
        await open_session(broker, config(SECRET), disc=discovery(token_status=1))
    assert e.value.code == code
    assert reason is None or e.value.details["reason"] == reason


async def test_auth_refusal_never_echoes_the_code():
    broker = FakeBroker(auth_rc=4)
    with pytest.raises(PrinterError) as e:
        await open_session(broker, config(SECRET), disc=discovery(token_status=1))
    err = e.value
    assert SECRET not in err.message and SECRET not in str(err) and SECRET not in repr(err)
    assert all(SECRET not in str(v) for v in err.details.values())
    assert cc2.ENV_CODE in err.hint  # the hint names the settings, not the secret


async def test_unreachable_broker_reports_not_reachable():
    broker = FakeBroker()

    async def boom(link):
        raise OSError("[Errno 111] Connection refused")
    broker.on_connect = boom
    with pytest.raises(PrinterError) as e:
        await open_session(broker)
    assert e.value.code == "not_reachable"


# --------------------------------------------------------------------------- correlated requests


async def test_requests_correlate_by_id_across_out_of_order_answers():
    order = []
    spawned = []

    def responder(link, msg):
        delay = 0.05 if msg["id"] == 1 else 0.0  # answer the second request first
        order.append(msg["id"])

        async def later():
            await asyncio.sleep(delay)
            await link.deliver(f"elegoo/{SN}/{link.params['client_id']}/api_response",
                               json.dumps({"id": msg["id"], "method": msg["method"],
                                           "result": {"answer": msg["method"]}}))
        spawned.append(asyncio.create_task(later()))  # keep a strong reference to the task
        return None

    broker = FakeBroker(responder=responder)
    async with await open_session(broker, disc=discovery()) as t:
        r1, r2 = await asyncio.gather(t.request(1002), t.request(1044))
    assert (r1, r2) == ({"answer": 1002}, {"answer": 1044})  # each answer finds its own caller
    assert sorted(order) == [1, 2]


async def test_foreign_and_malformed_answers_do_not_harm_a_pending_request():
    broker = FakeBroker()

    def responder(link, msg):
        client = link.params["client_id"]

        async def noisy():
            await link.deliver(f"elegoo/{SN}/{client}/api_response", json.dumps({"id": 99, "method": 1002,
                                                                                 "result": {"not": "mine"}}))
            await link.deliver(f"elegoo/{SN}/{client}/api_response", "not json at all")
            await link.deliver(f"elegoo/{SN}/{client}/api_response",
                               json.dumps({"id": msg["id"], "method": msg["method"], "result": {"ok": 1}}))
        asyncio.create_task(noisy())
        return None

    broker.responder = responder
    async with await open_session(broker) as t:
        assert await t.request(1002) == {"ok": 1}


async def test_request_timeout_asks_once_and_says_so_without_claiming_failure():
    sent = []
    broker = FakeBroker(responder=lambda link, msg: sent.append(msg["id"]) or None)
    async with await open_session(broker) as t:
        with pytest.raises(cc2.Cc2TimeoutError) as e:
            await t.request(1002, timeout_s=0.05)
        cmds = [m for topic, m in broker.links[0].published
                if topic == t.request_topic and m.get("method") == 1002]
    assert sent == [1] and len(cmds) == 1  # published once, never retried
    assert e.value.details.get("ambiguous") is True


async def test_a_wrongly_shaped_answer_is_a_protocol_error():
    broker = FakeBroker(responder=lambda link, msg: {"id": msg["id"], "method": msg["method"],
                                                     "result": "not an object"})
    async with await open_session(broker) as t:
        with pytest.raises(PrinterError) as e:
            await t.request(1002, timeout_s=0.5)
    assert e.value.code == "protocol_error"


async def test_a_dropped_connection_lost_pending_requests_without_retrying():
    sent = []

    def responder(link, msg):
        sent.append(msg["id"])
        link.kill()  # the printer hangs up instead of answering
        return None

    broker = FakeBroker(responder=responder)
    async with await open_session(broker) as t:
        with pytest.raises(PrinterError) as e:
            await t.request(1002, timeout_s=0.5)
    assert e.value.code == "not_reachable" and e.value.details["reason"] == "lost"
    assert sent == [1]  # no retry happened on the dead connection
    with pytest.raises(PrinterError):
        await t.request(1002, timeout_s=0.5)  # and the session is closed for good


# --------------------------------------------------------------------------- heartbeat


async def test_heartbeat_pings_and_tracked_pongs():
    broker = FakeBroker()
    async with await open_session(broker, heartbeat_interval_s=0.02) as t:
        assert t.pong_age_s() is not None  # open marks the session live
        api_req = f"elegoo/{SN}/{t.client_id}/api_request"
        pings = []
        for _ in range(50):  # wait for the first heartbeat beat
            pings = [m for topic, m in broker.links[0].published if topic == api_req and m.get("type") == "PING"]
            if pings:
                break
            await asyncio.sleep(0.01)
        assert pings and all(m == {"type": "PING"} for m in pings)
        await broker.links[0].deliver(f"elegoo/{SN}/{t.client_id}/api_response", json.dumps({"type": "PONG"}))
        await asyncio.sleep(0.01)
        assert t.pong_age_s() is not None and t.pong_age_s() < 1
        assert t.last_error is None


async def test_a_dropped_connection_marks_the_session_lost():
    broker = FakeBroker()
    async with await open_session(broker, heartbeat_interval_s=0.02) as t:
        broker.links[0].kill()  # the read loop notices the broker hung up
        await asyncio.sleep(0.01)
        assert t.last_error is not None and t.last_error.details["reason"] == "lost"
        with pytest.raises(PrinterError) as e:
            await t.request(1002, timeout_s=0.2)
    assert e.value.details["reason"] == "lost"


async def test_a_failing_ping_marks_the_session_lost():
    broker = FakeBroker()
    async with await open_session(broker, heartbeat_interval_s=0.02) as t:

        async def die(*args):
            raise ConnectionError("broker gone")
        broker.links[0].publish = die
        await asyncio.sleep(0.06)
        assert t.last_error is not None
        with pytest.raises(PrinterError) as e:
            await t.request(1002, timeout_s=0.2)
    assert e.value.details["reason"] == "lost"


# --------------------------------------------------------------------------- cleanup


async def test_close_is_idempotent_and_everything_stops():
    before = len(asyncio.all_tasks())
    broker = FakeBroker()
    t = await open_session(broker, heartbeat_interval_s=0.02)
    await t.close()
    await t.close()
    assert broker.links[0].closed
    with pytest.raises(PrinterError) as e:
        await t.request(1002, timeout_s=0.1)
    assert e.value.code == "not_reachable"
    await asyncio.sleep(0.06)  # longer than the heartbeat interval: nothing rescheduled itself
    assert len(asyncio.all_tasks()) == before


@pytest.mark.parametrize("replies,auth_rc", [(["fail"], None), (["too many clients"], None),
                                              ([None], None), ([], 4)])
async def test_a_failed_open_leaves_no_session_behind(replies, auth_rc):
    broker = FakeBroker(replies=replies or ["ok"], auth_rc=auth_rc)
    before = len(asyncio.all_tasks())
    with pytest.raises(PrinterError):
        await open_session(broker, registration_attempts=1, heartbeat_interval_s=0.02)
    if broker.links:
        assert broker.links[0].closed
    await asyncio.sleep(0.06)
    assert len(asyncio.all_tasks()) == before  # no heartbeat or reader outlived the failed open
