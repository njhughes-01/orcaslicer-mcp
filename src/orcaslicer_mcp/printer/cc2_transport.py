"""The CC2 wire itself: directed LAN discovery, a registered MQTT session with the printer's own
broker, correlated requests, the mandatory heartbeat, and honest failures. Nothing in here decides
what is safe to send — it moves JSON and says truthfully what happened. Protocol facts come from
the community CC2 protocol documentation (derived from Elegoo's own elegoo-link SDK): UDP discovery
on 52700 (method 7000), MQTT 3.1.1 on 1883 as username ``elegoo``, register before commands, a PING
every 10 s, ``{"id","method","params"}`` requests answered with the same ``id``. Credentials: only
in LAN-only mode, the documented no-access-code password is used and only when discovery reports
that mode; an access-code mode must be met with the configured code or a refusal to connect.
Address policy: the adapter speaks only to an explicitly configured private LAN or loopback
address — resolved once, before discovery, and pinned, so the same address serves the UDP probe
and the MQTT session and a name whose answer changes between lookups can only mislead a lookup
we never make (the design's defence against DNS rebinding). Public, multicast, unspecified,
link-local and broadcast answers are refused; ambiguous or mixed answers fail closed; the
configured name survives only as a display word, and only when it is safe to display."""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import aiomqtt
from aiomqtt.exceptions import MqttCodeError

from ..guard import REDACTED, _strip_userinfo
from .errors import PrinterError
from .target import normalise_url

# --- what the protocol manual pins down ---------------------------------------------------------

DISCOVERY_PORT = 52700
DISCOVERY_ATTEMPTS = 2            # directed probes; the reply is known to take 1-2 s
DISCOVERY_TIMEOUT_S = 3.0         # the manual's figure for a directed probe
MQTT_PORT = 1883
MQTT_USERNAME = "elegoo"          # always this, whatever the auth mode
MQTT_KEEPALIVE_S = 60
MQTT_CONNECT_TIMEOUT_S = 10.0
DEFAULT_ACCESS_CODE = "123456"    # noqa: S105 — the documented password when no access code is set
REGISTRATION_TIMEOUT_S = 3.0
REGISTRATION_ATTEMPTS = 2         # bounded on purpose: a lost reply can leave a ghost client in one
REGISTRATION_RETRY_DELAY_S = 5.0  # of the printer's ~4 slots, and the manual asks 5-10 s between tries
HEARTBEAT_INTERVAL_S = 10.0       # the printer drops a silent client after 65 s
REQUEST_TIMEOUT_S = 10.0

ENV_URL = "ORCA_CC2_URL"
ENV_CODE = "ORCA_CC2_ACCESS_CODE"
ENV_CODE_FILE = "ORCA_CC2_ACCESS_CODE_FILE"

_URL_HINT = ("Set " + ENV_URL + " in this MCP server's settings to the printer's private LAN "
             "address — a literal address is the steadiest — for example 192.0.2.10 "
             "(LAN Only Mode printers need no port).")
_CODE_HINT = ("Enter the printer's access code on its touchscreen (Settings → Network) and set it as "
              + ENV_CODE + " in this MCP server's settings, or keep it in a file only you can read "
              "(chmod 600) and point " + ENV_CODE_FILE + " at it.")

_SN_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")  # the serial goes into MQTT topics — nothing else fits
_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,253}")  # nothing an error line fears being told


class Cc2Error(PrinterError):
    """A CC2 transport failure: the same codes every printer tool speaks, plus a `reason` detail
    saying which CC2 thing went wrong (``busy``, ``client_limit``, ``refused``, ``timeout``,
    ``lost``) so callers can act without parsing messages."""


class Cc2RegistrationError(Cc2Error):
    """The printer will not take this client: it refused, its slots are full, or it never answered.
    None of these were retried more than the bounded attempts asked for."""


class Cc2TimeoutError(Cc2Error):
    """The printer did not answer in time — ambiguous by nature: the command may still have
    reached it. Nothing here retries; that decision belongs to a caller holding the facts."""


# --- credentials and configuration ---------------------------------------------------------------


@dataclass(frozen=True)
class AccessCode:
    """An access code that never prints itself: only ``.value`` is the code."""
    value: str = field(repr=False)

    def __repr__(self) -> str:
        return f"AccessCode({REDACTED})"

    def __str__(self) -> str:
        return REDACTED


@dataclass(frozen=True)
class Cc2Config:
    """The address of the one printer this server may reach, and the code it may have to offer."""
    host: str
    access_code: AccessCode | None = None


def _read_code_file(path: Path) -> AccessCode | None:
    """The code from a private file. A file anyone else on the machine can read is refused: the
    point of the file is that the code stays out of process settings and logs."""
    def unreadable(why: str) -> PrinterError:
        return PrinterError("not_configured",
                            f"The access code file '{path}' {why}.", hint=_CODE_HINT)
    try:
        st = path.stat()
    except OSError:
        raise unreadable("can't be read") from None
    if not stat.S_ISREG(st.st_mode):
        raise unreadable("is not a regular file")
    if st.st_mode & 0o077:
        raise PrinterError("not_configured",
                           f"The access code file '{path}' can be read by others "
                           f"(mode {stat.S_IMODE(st.st_mode):03o}).",
                           hint=f"Make it yours alone: chmod 600 '{path}'. " + _CODE_HINT)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise unreadable("can't be read") from None
    code = text.strip()  # a trailing newline from an editor is not part of the code
    return AccessCode(code) if code else None


def load_config(env: dict[str, str] | None = None) -> Cc2Config:
    """Read the CC2 settings from the environment (os.environ by default). No address, no deal;
    the two access-code sources must not both be set; a code file must be a private regular file."""
    env = os.environ if env is None else env
    raw = (env.get(ENV_URL) or "").strip()
    if not raw:
        raise PrinterError("not_configured", f"No CC2 printer address is set ({ENV_URL} is empty).",
                           hint=_URL_HINT)
    url, auth = normalise_url(raw)
    parts = urlsplit(url)
    host = parts.hostname
    bad = None
    if auth is not None:
        bad = (f"The CC2 address {_strip_userinfo(raw)} carries a login, which this adapter does "
               f"use: the access code belongs in {ENV_CODE} or {ENV_CODE_FILE}, not in the address.")
    else:
        try:
            port = parts.port
        except ValueError:
            port = -1
        if not host:
            bad = f"The CC2 address {_strip_userinfo(raw)} has no host name."
        elif port is not None:
            bad = (f"The CC2 address {_strip_userinfo(raw)} sets a port. The CC2 speaks MQTT on "
                   f"{MQTT_PORT} by itself — leave the port out.")
        elif parts.path not in ("", "/"):
            bad = f"The CC2 address {_strip_userinfo(raw)} carries a path, which this adapter ignores."
    if bad:
        raise PrinterError("not_configured", bad, hint=_URL_HINT)

    file_raw = (env.get(ENV_CODE_FILE) or "").strip()
    code_raw = (env.get(ENV_CODE) or "").strip()
    if file_raw and code_raw:
        raise PrinterError("not_configured",
                           f"Both {ENV_CODE_FILE} and {ENV_CODE} are set. They must not be.",
                           hint=f"Keep the access code in one place; {ENV_CODE_FILE} with a "
                                f"chmod-600 file is the better home.")
    code = _read_code_file(Path(file_raw).expanduser()) if file_raw else \
        (AccessCode(code_raw) if code_raw else None)
    return Cc2Config(host=host, access_code=code)


def mqtt_password(disc: Cc2Discovery, config: Cc2Config) -> str:
    """The MQTT password the printer is met with. The documented no-access-code password only when
    the printer itself reports that mode; a configured code always wins over it. Never a silent
    fallback: when the printer asks for a code we don't have, that is an error, not a guess."""
    if config.access_code is not None:
        return config.access_code.value
    if disc.token_status == 0:
        return DEFAULT_ACCESS_CODE
    raise PrinterError("auth_required",
                       f"The printer at {disc.display or disc.host} says it has an access code, and none is "
                       f"configured, so the connection is refused rather than guessed. Set "
                       f"{ENV_CODE} (or {ENV_CODE_FILE} pointing at a private file) in this MCP "
                       "server's settings.",
                       hint=_CODE_HINT)


# --- the one address we will speak to -------------------------------------------------------------

_BROADCAST_V4 = ipaddress.IPv4Address("255.255.255.255")


def _refusal(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """A spoken word for why this address is no printer of ours, None when it is a private LAN or
    loopback one we will speak to. is_private answers for the private ranges — RFC1918 and IPv6
    loopback and unique-local, and the documented test ranges too, which is what makes a fake
    printer on 192.0.2.10 a fair stand-in — while the four dangers are refused by name first,
    because is_private calls link-local and the unspecified address private. An IPv4-mapped IPv6
    answer is judged as the IPv4 it carries, so ::ffff:8.8.8.8 is refused as public and
    ::ffff:192.168.1.5 accepted as private."""
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_link_local:
        return "link-local"
    if ip == _BROADCAST_V4:
        return "broadcast"
    return None if ip.is_private else "public"


@dataclass(frozen=True)
class ResolvedTarget:
    """The one address every request goes to, pinned before anything leaves, and the configured
    name kept only to tell a human which printer was meant — never used to reach anything."""
    ip: str
    display: str | None = None

    @property
    def shown(self) -> str:
        return f"{self.display} ({self.ip})" if self.display else self.ip


async def _lookup(host: str) -> list[str]:
    """One address lookup, over every family the resolver answers for — so a mixed answer cannot
    hide a public record behind a private first pick. Repeat answers are the same address twice."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, MQTT_PORT, type=socket.SOCK_DGRAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


async def resolve_target(host: str) -> ResolvedTarget:
    """Resolve the configured address once, before any packet leaves, and pin the answer: that one
    address serves discovery and MQTT alike, so a name whose answer changes between two lookups —
    a rebinding — can only mislead a lookup we never make. A literal address is checked as it
    stands and never handed to the resolver. A forbidden answer, a mixed answer carrying one
    forbidden address, or an answer naming several addresses fails closed; the configured name
    survives only as a display word, and only when it is safe to display."""
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]  # the bracket form of an IPv6 literal; the brackets are not the address
    name = host if _SAFE_NAME_RE.fullmatch(host) and not host.replace(".", "").isdigit() else None
    shown = name or REDACTED
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if why := _refusal(literal):
            raise PrinterError("not_configured",
                               f"The CC2 address {shown} is a {why} address. This adapter connects "
                               "only to the private LAN or loopback address of the one printer it "
                               "serves.", hint=_URL_HINT)
        return ResolvedTarget(ip=str(literal))
    answers = await _lookup(host)
    if not answers:
        raise PrinterError("not_configured",
                           f"The CC2 address {shown} resolves to no address at all.", hint=_URL_HINT)
    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for answer in answers:
        try:
            ips.append(ipaddress.ip_address(answer))
        except ValueError:
            raise PrinterError("not_configured",
                               f"The name {shown} resolved to '{answer}', which is not an address "
                               "at all, so nothing was connected to.", hint=_URL_HINT) from None
    ips = list(dict.fromkeys(ips))  # the same address twice, in whatever spelling, is one answer
    for ip in ips:
        if why := _refusal(ip):
            raise PrinterError("not_configured",
                               f"The name {shown} resolves to a {why} address ({ip})"
                               + (" among several answers" if len(ips) > 1 else "")
                               + ", which this adapter refuses to connect to: it serves the one "
                               "printer at the private LAN or loopback address you configure, and "
                               "a name whose answer can change between lookups is how a rebinding "
                               f"works. Pin {ENV_URL} to the printer's literal LAN address to "
                               "close that door.",
                               hint=_URL_HINT, reason="unsafe_address")
    if len(ips) > 1:
        listed = ", ".join(str(ip) for ip in ips)
        raise PrinterError("not_configured",
                           f"The name {shown} resolves to {len(ips)} different addresses ({listed}), "
                           "and this adapter cannot tell which one is the printer — refusing rather "
                           f"than guessing. Pin {ENV_URL} to the printer's literal private LAN "
                           "address.",
                           hint=_URL_HINT, reason="ambiguous_address")
    return ResolvedTarget(ip=str(ips[0]), display=name)


# --- discovery ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Cc2Discovery:
    """What the printer itself said about who it is and how it may be approached."""
    host: str
    sn: str
    model: str | None
    name: str | None
    token_status: int   # 0 = no access code, 1 = access code set
    lan_status: int     # 1 = LAN-only mode (the only one this adapter speaks), 0 = cloud mode
    display: str | None = None  # the configured name, safe to show, never a destination


async def _udp_exchange(host: str, payload: bytes, timeout_s: float) -> dict:
    """One directed probe of host:52700, answered by the first reply. Raises TimeoutError on
    silence. The socket is connected, so only that printer's answer can arrive — a broadcast
    answer from some other device is structurally impossible here."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    class Probe(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr) -> None:
            if not fut.done():
                fut.set_result(data)

    transport, _proto = await loop.create_datagram_endpoint(Probe, remote_addr=(host, DISCOVERY_PORT))
    try:
        transport.sendto(payload)
        data = await asyncio.wait_for(fut, timeout_s)
    finally:
        transport.close()
    try:
        body = json.loads(data.decode("utf-8", "replace"))
    except ValueError as e:
        raise PrinterError("protocol_error", "The discovery answer from the printer wasn't JSON.") from e
    if not isinstance(body, dict):
        raise PrinterError("protocol_error", "The discovery answer from the printer wasn't a JSON object.")
    return body


def _mode(value: Any, name: str) -> int:
    # a bool is an int in Python, and it is not a mode: True must not read as token_status 1
    if isinstance(value, bool) or not isinstance(value, int):
        raise PrinterError("protocol_error",
                           f"The discovery answer's {name} wasn't a whole number, so the printer's "
                           "access mode can't be trusted.")
    return value


def _parse_discovery(target: ResolvedTarget, body: dict) -> Cc2Discovery:
    host = target.shown
    result = body.get("result")
    if not isinstance(result, dict):
        raise PrinterError("protocol_error", "The discovery answer from the printer had no result object.")
    sn = result.get("sn")
    if not isinstance(sn, str) or not _SN_RE.fullmatch(sn):
        raise PrinterError("protocol_error",
                           "The discovery answer from the printer carried a serial that can't be used "
                           "as part of a topic.")
    disc = Cc2Discovery(host=target.ip, sn=sn, display=target.display,
                        model=result.get("machine_model") if isinstance(result.get("machine_model"), str) else None,
                        name=result.get("host_name") if isinstance(result.get("host_name"), str) else None,
                        token_status=_mode(result.get("token_status"), "token_status"),
                        lan_status=_mode(result.get("lan_status"), "lan_status"))
    if disc.lan_status == 0:
        raise PrinterError("unsupported_connection",
                           f"The printer at {host} runs in cloud mode, which this adapter does not "
                           "support: it speaks LAN Only Mode (direct local MQTT) and nothing else.",
                           hint="Switch it to LAN Only Mode on the touchscreen (Settings → Network), "
                                "then try again.")
    if disc.lan_status != 1:
        raise PrinterError("protocol_error",
                           f"The printer reported LAN mode {disc.lan_status}, which is not a mode "
                           "this adapter knows.")
    if disc.token_status not in (0, 1):
        raise PrinterError("unsupported_connection",
                           f"The printer reports access mode {disc.token_status}, which this "
                           "adapter does not know.", hint=_CODE_HINT)
    return disc


async def discover(host: str, *, timeout_s: float = DISCOVERY_TIMEOUT_S,
                   attempts: int = DISCOVERY_ATTEMPTS) -> Cc2Discovery:
    """Ask the configured printer, directly, who it is. The address is resolved and pinned first —
    once — so the probe can only ever go where the policy allows, and the reply says which access
    mode it expects, which decides whether a code is required before any connection is attempted."""
    target = await resolve_target(host)
    probe = json.dumps({"id": 0, "method": 7000}).encode()
    for _ in range(max(1, attempts)):
        try:
            body = await _udp_exchange(target.ip, probe, timeout_s)
        except TimeoutError:
            continue
        return _parse_discovery(target, body)
    raise PrinterError("not_reachable",
                       f"The printer at {target.shown} did not answer the LAN discovery probe.",
                       hint="Is it switched on and on the same network as this server, in LAN Only "
                            "Mode? If its address changed, " + _URL_HINT)


# --- the session -----------------------------------------------------------------------------------


def _aiomqtt_link(host: str, password: str, client_id: str) -> Any:
    """The production link: MQTT 3.1.1 to the printer's own broker, as the manual draws it. The
    host is always the pinned literal address, so paho reaches that address with a connect to a
    literal rather than a name lookup of its own. The password only ever lives in this client
    object — never in an attribute of ours."""
    return aiomqtt.Client(host, port=MQTT_PORT, username=MQTT_USERNAME, password=password,
                          identifier=client_id, protocol=aiomqtt.ProtocolVersion.V311,
                          keepalive=MQTT_KEEPALIVE_S, clean_session=True,
                          timeout=MQTT_CONNECT_TIMEOUT_S)


def _client_id() -> str:
    """The web-client format the printer expects: '0cli' + the last 5 hex digits of the current
    millisecond + random hex, cut to exactly 10."""
    ts = format(int(time.time() * 1000), "x")[-5:]
    return f"0cli{ts}{format(secrets.randbelow(4096), 'x')}"[:10]


def _request_id() -> str:
    """Registration's correlation id: 16 hex digits + the timestamp in hex. It names the topic the
    answer comes back on, so it must be ours alone on a broker every client can see."""
    return "".join(secrets.choice("0123456789abcdef") for _ in range(16)) + format(int(time.time() * 1000), "x")


def _connect_refusal(e: MqttCodeError, host: str) -> PrinterError:
    """A CONNACK refusal, told apart the way the manual numbers them: 4/5 are credentials, 3 is
    the broker taking no more clients right now, the rest we can only report."""
    rc = getattr(e, "rc", None)
    rc = rc.value if hasattr(rc, "value") else rc
    if rc in (4, 5):
        return PrinterError("auth_rejected",
                            f"The printer at {host} refused the access code over MQTT.", hint=_CODE_HINT)
    if rc == 3:
        return PrinterError("not_reachable",
                            f"The printer at {host} is busy: its broker took no further connections "
                            "right now.", reason="busy",
                            hint="Close other clients (the web interface, ElegooSlicer, Home "
                                 "Assistant) or wait a moment, then try again.")
    return PrinterError("not_reachable",
                        f"The printer at {host} refused the MQTT connection (code {rc}).")


class Cc2Transport:
    """One registered session with one printer: register, heartbeat, correlated requests, close.
    Obtain it with open_transport(). A lost answer stays lost — nothing here resends a command,
    because on this protocol a missing reply does not mean the printer ignored it."""

    def __init__(self, link: Any, *, disc: Cc2Discovery, client_id: str, request_id: str,
                 stack: AsyncExitStack, heartbeat_interval_s: float, request_timeout_s: float):
        self.host = disc.host
        self.sn = disc.sn
        self.client_id = client_id
        self._request_id = request_id
        self._link = link
        self._stack = stack
        self._heartbeat_interval_s = heartbeat_interval_s
        self._request_timeout_s = request_timeout_s
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._tasks: list[asyncio.Task] = []
        self._closed = False
        self._dead: PrinterError | None = None
        self._last_pong: float | None = time.monotonic()  # open counts as live; PONGs keep it that way
        self._reg_event = asyncio.Event()
        self._reg_result: dict | None = None

    # topics: every name the protocol puts on the wire, built from the printer's own serial
    @property
    def register_request_topic(self) -> str:
        return f"elegoo/{self.sn}/api_register"

    @property
    def register_topic(self) -> str:
        return f"elegoo/{self.sn}/{self._request_id}/register_response"

    @property
    def request_topic(self) -> str:
        return f"elegoo/{self.sn}/{self.client_id}/api_request"

    @property
    def response_topic(self) -> str:
        return f"elegoo/{self.sn}/{self.client_id}/api_response"

    @property
    def last_error(self) -> PrinterError | None:
        """The failure that ended this session, once there is one."""
        return self._dead

    def pong_age_s(self) -> float | None:
        """Seconds since the printer last said PONG (opening the session counts as a fresh start).
        The manual's own budget is 65 s before the printer drops a silent client."""
        if self._last_pong is None:
            return None
        return time.monotonic() - self._last_pong

    async def subscribe(self, topic: str) -> None:
        """Take on another topic for the life of the session (the status pushes of later tasks)."""
        await self._link.subscribe(topic)

    async def request(self, method: int, params: dict | None = None,
                      *, timeout_s: float | None = None) -> dict:
        """One request, one answer, matched by id. The result object comes back as it arrived;
        what an error_code means is the caller's judgement, not this transport's. A timeout is
        reported as such and never retried: the command may have landed."""
        if self._dead is not None:
            raise self._dead
        if self._closed:
            raise PrinterError("not_reachable", f"The CC2 session to {self.host} is closed.")
        timeout = self._request_timeout_s if timeout_s is None else timeout_s
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._link.publish(self.request_topic,
                                     json.dumps({"id": rid, "method": method, "params": params or {}}))
        except asyncio.CancelledError:
            self._pending.pop(rid, None)
            raise
        except Exception as e:
            self._pending.pop(rid, None)
            self._mark_dead("could not take a command")
            raise self._dead from e
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            if self._dead is not None:
                raise self._dead from None
            raise Cc2TimeoutError(
                "protocol_error",
                f"The printer did not answer method {method} within {timeout:g}s. The command may "
                "or may not have reached it: nothing was retried.",
                hint="Read the printer's state before deciding anything about this command.",
                reason="timeout", ambiguous=True) from None

    async def close(self) -> None:
        """End the session: heartbeat and reader stopped, the MQTT link released. Idempotent —
        closing a dead or closed session is a no-op, and every path out of open_transport goes
        through here or its teardown."""
        if self._closed:
            return
        self._closed = True
        await self._teardown()

    async def _teardown(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        with contextlib.suppress(Exception):  # cleanup never masks the failure that caused it
            await self._stack.aclose()

    async def __aenter__(self) -> "Cc2Transport":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    @classmethod
    async def _open(cls, link, *, disc, client_id, registration_attempts, registration_retry_delay_s,
                    heartbeat_interval_s, request_timeout_s, sleep) -> "Cc2Transport":
        stack = AsyncExitStack()
        try:
            await stack.enter_async_context(link)
        except MqttCodeError as e:
            await stack.aclose()
            raise _connect_refusal(e, disc.host) from e
        except (aiomqtt.MqttError, OSError) as e:
            await stack.aclose()
            raise PrinterError("not_reachable",
                               f"The printer at {disc.host} did not take the MQTT connection.",
                               hint="Is it switched on, in LAN Only Mode, and reachable from here?") from e
        t = cls(link, disc=disc, client_id=client_id, request_id=_request_id(), stack=stack,
                heartbeat_interval_s=heartbeat_interval_s, request_timeout_s=request_timeout_s)
        try:
            # Read first, then ask: an answer that arrives while we are still subscribing must not
            # fall on the floor.
            await link.subscribe([t.register_topic, t.response_topic])
            t._start_reader()
            await t._register(registration_attempts, registration_retry_delay_s, sleep)
        except BaseException:
            await t._teardown()
            raise
        t._start_heartbeat()  # the manual: start it immediately after registering
        return t

    async def _register(self, attempts: int, retry_delay_s: float, sleep) -> None:
        """Bounded registration: an explicit refusal or a full client roster is final, and only a
        lost answer is tried again — at most up to `attempts` publishes in total, because a ghost
        client of ours may already be eating one of the printer's ~4 slots."""
        msg = json.dumps({"client_id": self.client_id, "request_id": self._request_id})
        attempts = max(1, attempts)
        for attempt in range(1, attempts + 1):
            self._reg_event.clear()
            self._reg_result = None
            await self._link.publish(self.register_request_topic, msg)
            try:
                await asyncio.wait_for(self._reg_event.wait(), REGISTRATION_TIMEOUT_S)
            except asyncio.TimeoutError:
                if self._dead is not None:
                    raise self._dead from None
                if attempt < attempts:
                    await sleep(retry_delay_s)
                    continue
                raise Cc2TimeoutError(
                    "protocol_error",
                    f"The printer never answered this client's registration (tried {attempts} "
                    "time(s), so far without a reply).",
                    hint="A lost reply can leave a half-registered client holding one of the "
                         "printer's few MQTT slots — if it now says 'too many clients', close "
                         "other connections (web interface, ElegooSlicer, Home Assistant) or "
                         "wait a minute. " + _CODE_HINT,
                    reason="timeout") from None
            if self._dead is not None:
                raise self._dead from None
            err = self._reg_result.get("error") if isinstance(self._reg_result, dict) else None
            if err == "ok":
                return
            if err == "too many clients":
                raise Cc2RegistrationError(
                    "protocol_error",
                    f"The printer at {self.host} has no free client slots for us.",
                    hint="Close the web interface, ElegooSlicer or Home Assistant connections to "
                         "the printer, then try again.",
                    reason="client_limit")
            shown = err if isinstance(err, str) and len(err) <= 80 else "an unknown answer"
            raise Cc2RegistrationError("protocol_error",
                                       f"The printer refused this client's registration "
                                       f"('{shown}'), without a reason.",
                                       hint=_CODE_HINT, reason="refused")

    def _start_reader(self) -> None:
        self._tasks.append(asyncio.create_task(self._read_loop(), name=f"cc2-reader-{self.client_id}"))

    async def _read_loop(self) -> None:
        try:
            async for msg in self._link.messages:
                self._route(msg)
            self._mark_dead("was closed by the printer")
        except asyncio.CancelledError:
            raise
        except Exception:
            self._mark_dead("dropped")

    def _route(self, msg: Any) -> None:
        """Every incoming message finds its own place: a PONG renews the heartbeat clock, a
        registration answer wakes _register, a command answer matches its id. Anything else — a
        foreign id, broken JSON, an envelope we don't know — is dropped on the floor, never
        guessed at: the api_response topic is not ours alone on this broker."""
        raw = getattr(msg, "payload", None)
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8", "replace")
        if not isinstance(raw, str):
            return
        try:
            body = json.loads(raw)
        except ValueError:
            return
        if not isinstance(body, dict):
            return
        if body.get("type") == "PONG":
            self._last_pong = time.monotonic()
            return
        if str(getattr(msg, "topic", "")) == self.register_topic:
            if self._reg_result is None:
                self._reg_result = body
                self._reg_event.set()
            return
        rid = body.get("id")
        if isinstance(rid, bool) or not isinstance(rid, int):
            return
        fut = self._pending.pop(rid, None)
        if fut is None or fut.done():
            return
        result = body.get("result")
        if isinstance(result, dict):
            fut.set_result(result)
        else:
            fut.set_exception(PrinterError(
                "protocol_error",
                f"The printer's answer to method {body.get('method')} carried no result object."))

    def _start_heartbeat(self) -> None:
        if self._heartbeat_interval_s > 0:
            self._tasks.append(asyncio.create_task(self._heartbeat_loop(),
                                                   name=f"cc2-heartbeat-{self.client_id}"))

    async def _heartbeat_loop(self) -> None:
        """PING every interval, fire-and-forget as the manual says — a PONG is not awaited before
        the next one. But a PING that cannot even be published means the session is over: the
        printer counts a silent client as dead after 65 s, and any pending command just lost its
        only channel."""
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval_s)
                if self._dead is not None or self._closed:
                    return
                try:
                    await self._link.publish(self.request_topic, json.dumps({"type": "PING"}))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._mark_dead("stopped answering (its heartbeat could no longer be sent)")
                    return
        except asyncio.CancelledError:
            raise

    def _mark_dead(self, because: str) -> None:
        """One death, reported the same way to everyone waiting: an ended session is never
        silently resurrected and no command is ever resent on the strength of it."""
        if self._dead is None:
            self._dead = PrinterError(
                "not_reachable",
                f"The MQTT session with the printer at {self.host} {because}. A command sent just "
                "before this may or may not have reached the printer; nothing will be retried.",
                reason="lost", ambiguous=True)
        self._reg_event.set()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(self._dead)
        self._pending.clear()


async def open_transport(config: Cc2Config, *, discovery: Cc2Discovery | None = None,
                         link_factory: Callable[[str, str, str], Any] | None = None,
                         registration_attempts: int = REGISTRATION_ATTEMPTS,
                         registration_retry_delay_s: float = REGISTRATION_RETRY_DELAY_S,
                         heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
                         request_timeout_s: float = REQUEST_TIMEOUT_S,
                         sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> Cc2Transport:
    """Open one registered CC2 session: ask the configured printer who it is (unless discovery
    was already done), meet its stated access mode with the right password, connect, register —
    bounded — and start the heartbeat. The caller owns the session and must close it; if any step
    fails, nothing is left half-open and the reason arrives as its own error kind. The configured
    address is resolved once and pinned before discovery, and that one private address serves the
    UDP probe and the MQTT session alike — commands go only to it, by directed probe, never a
    broadcast and never a second lookup that a rebinding could steer elsewhere."""
    target = await resolve_target(config.host)
    disc = discovery or await discover(target.ip)
    password = mqtt_password(disc, config)
    client_id = _client_id()
    link = (link_factory or _aiomqtt_link)(target.ip, password, client_id)
    return await Cc2Transport._open(link, disc=disc, client_id=client_id,
                                    registration_attempts=registration_attempts,
                                    registration_retry_delay_s=registration_retry_delay_s,
                                    heartbeat_interval_s=heartbeat_interval_s,
                                    request_timeout_s=request_timeout_s, sleep=sleep)
