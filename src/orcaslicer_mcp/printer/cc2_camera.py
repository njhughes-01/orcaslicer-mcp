"""The CC2's camera: method 1042 (LIVING_VIDEO_URL), one read-only request, asked exactly once, and
**an internal candidate — not a tool, not registered, not promised anywhere.** Nothing here fetches,
opens, streams, proxies or toggles a camera: it asks the printer for the address it advertises for its
own stream, vets that address for credentials, and reports it as unverified. See the last section for
what is established and what is not.

**The official shape, from the bundle the printer serves itself** (``webapp/lan/index.html``, the
Elegoo LAN client over its MQTT path)::

    const Te = {method: Pt.GetLivingVideoUrl}          // GetLivingVideoUrl = 1042, no params key at all
    const Be = await dn.getLivingVideoUrl(sn, Te)      // request(n, r) -> this.request(n, r, r = 5e3)
    if (Be.error_code !== 0)  toastMessages.getLivingVideoUrl   // "Failed to get video stream URL."
    livingVideoUrl.value = Be.url                      // then an <img> whose src is url + "&_t=<ts>"

so: no parameters, the answer's field is ``url`` beside ``error_code`` (and this transport hands back
the frame's ``result`` object, which is where both live), a nonzero ``error_code`` is the printer's own
"no", and the consumer is an ``<img>`` tag — an HTTP picture/MJPEG source, which is also why the URL
must arrive with its scheme already on it: the client pastes it into ``src`` unchanged and only appends
a ``_t`` cache-buster of its own. The method number is that bundle's own enum
(``GetLivingVideoUrl=1042``, between ``OTAUpgrade=1039`` and ``SetDeviceName=1043``).

**The same bundle's older websocket path is a different method family, and it is evidence about the
field name, not about 1042.** There, ``openVideoStream`` sends ``CmdVideoStreamControl`` with
``{Enable: 1|0}`` and the answer is ``{Ack, VideoUrl}``, which that code uses as
```http://${VideoUrl}``` — i.e. that field holds a bare host and port and the client supplies the
scheme. So a reply whose stream address arrives as ``video_url``/``VideoUrl`` without a scheme belongs
to that family, and this module does not read it as ``url``: it refuses, names the field it saw, and
records which shape this firmware actually speaks.

**What 1042 is not settled on being — and it is the reason this is a candidate and not a tool.** The
community protocol manual's methods table lists 1042 as ``VIDEO_STREAM — Toggle camera`` with
``{"enable": true}``, and the Home Assistant integration built on that manual does exactly that:
``set_printer_video_stream(enable=...)`` sends ``{"enable": int(enable)}`` on 1042, its camera entity
has an idle watchdog that calls it to *switch the stream off*, and its own docstring is careful to say
a read "does NOT toggle the printer video" — which is only a thing worth saying if 1042 does toggle.
That integration also reads the reply under a different name (``video_url``) and, when the printer
returns no URL at all or a proxy host is configured, **invents** one:
``f"http://{printer_ip}:8080/?action=stream"`` (its ``CC2_VIDEO_PORT``/``CC2_VIDEO_PATH``). That is a
third-party workaround, and it is the strongest hint available that a CC2 may well answer 1042 with
success and no address — so an empty ``url`` is reported as what the printer sent, and this server
never composes an address the printer did not send. Nothing here sends ``enable``, ``true``, ``false``
or a bare 1054 (``StartVideoStream``, which appears in the bundle's enum twice, defined and never
called); and because a bare 1042 has never been shown to be free of side effects on this firmware —
Elegoo's own client fires it on a button press and on a refresh, so it is not proof of inertness — the
reply says out loud that asking the question may not be neutral.

**Credentials, which is the part this module is for.** The address is the printer's text, and this
server's address policy is LAN-only with no credential in any authority, so every returned URL is vetted
before it is repeated and a URL this server cannot vet is a URL this server does not repeat at all:

* only ``http``, ``https``, ``rtsp``, ``rtmp``, ``ws`` and ``wss`` are repeatable, and a disallowed
  scheme is reported by its own (sanitised) scheme word — never by the string around it;
* a login in the authority (``scheme://user:pass@host``) is withheld entirely, the transport's own rule
  for a printer address, with only its presence reported;
* a query parameter whose *name* reads like a credential (``token``, ``key``, ``secret``,
  ``password``, ``signature``, and friends) has its value withheld and its name kept, and the reply says
  it was rewritten rather than letting a signed link ride out in one piece;
* anything unparseable, non-text, or over 255 characters is refused with its length or its type named
  and its contents nowhere in the reply — a string that will not parse is exactly a string whose
  hidden ``@`` nobody has checked;
* the host is classified, never resolved: a literal is checked against this server's own LAN-only
  rule (private/loopback is the expected answer, a public host is a warning), and a name is reported as
  a name-not-resolved rather than looked up, because a name lookup would be a network call this module
  does not make. Nothing is fetched, so a wrong or hostile address here cannot do anything from here.

**One read-only 1001 afterwards, kept apart from the answer.** ``GetSystemInfo`` is where the printer's
own camera facts live (``camera_connected``, ``video_connections``, ``max_video_connections`` — read by
the existing, tested ``cc2_info.fetch_attributes`` rather than a second reader invented here), and the
manual says the MJPEG port is unauthenticated and usually allows a single viewer. So after a 1042 that
answered — or one that refused, which is the case where "does this printer even have a camera attached"
is the useful half of the answer — one 1001 is asked and reported beside it, never as a condition and
never twice: a 1001 that fails leaves the 1042 answer standing with one note about why. A timeout, and
a session that is already dead, ask nothing further.

**Unverified, in every reply.** No 1042 answer has ever been captured from Nathan's firmware. The
fields read here are the fields Elegoo's own LAN web client reads from that method; a differently
shaped reply is refused, never fitted, so a refusal from this module is a fact about this firmware
rather than a broken printer, and a success means "the printer advertised this address in the shape
Elegoo's own client reads" and nothing stronger — not that a picture appears at it, and not that
anything here looked.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

# The envelope gate is shared with every other CC2 read on purpose: what an absent, zero, nonzero or
# unreadable error_code means cannot be allowed to drift between the file listing and the camera.
from .cc2_info import fetch_attributes
from .cc2_state import Cc2DataError, _check_envelope
from .errors import PrinterError

METHOD_LIVING_VIDEO_URL = 1042    # LIVING_VIDEO_URL — Elegoo's client sends it parameterless
METHOD_VIDEO_STREAM_WS = 1054     # StartVideoStream in the same enum: defined, never called, never sent

# The official caller passes no override, so its class default of 5 s applies to this method, against
# this server's 10 s transport default: the longer of the two first-party windows is taken, which is
# the rule cc2_canvas and cc2_peripherals record for the methods whose caller passes nothing.
CAMERA_TIMEOUT_S = 10.0

# Same ceiling as the reply sanitiser's own string cap (_CC2_STR_MAX in server.py): an over-long URL is
# refused with its length named rather than arriving as this server's half of it.
_MAX_URL_LEN = 255

# Notes and failure strings must survive server.py's 255-character reply cap whole — the discipline
# cc2_control and cc2_delete document: a note cut mid-sentence loses the half a model acts on.
NOTE_CAP = 255

# Repeatable schemes for a camera address. The first two are what the official ``<img>`` consumer
# needs; the rest are what the community sources name for this camera (MJPEG over HTTP, rtsp in the
# integration's own debug script) without opening the door to ``file:``, ``ftp:`` or ``javascript:``.
ALLOWED_SCHEMES = frozenset({"http", "https", "rtsp", "rtmp", "ws", "wss"})

# Query parameter names that read as a credential rather than as a routing parameter. Their values are
# withheld and their names kept, so the shape of what the printer sent stays reportable.
_CREDENTIAL_QUERY_NAMES = frozenset({
    "token", "access_token", "accesstoken", "accesskey", "access_key", "apikey", "api_key", "key",
    "secret", "secretkey", "password", "passwd", "pwd", "pass", "auth", "authorization", "authtoken",
    "signature", "sig", "sign", "session", "sessionid", "sid", "user", "username", "user_name",
    "login", "pin", "code", "x-token", "x_token",
})
# Names that read as a credential in any combination (?video_token=…, ?stream_signature=…).
_CREDENTIAL_QUERY_PARTS = ("token", "password", "secret", "signature", "credential", "apikey")

_SCHEME_SHAPES = 16   # how much of a refused scheme is safe to print
_WITHHED = "withheld"  # what a credential-shaped query parameter's value becomes in a reply


# --------------------------------------------------------------------------- refusals


def _refuse(why: str, reason: str, **details) -> Cc2DataError:
    return Cc2DataError("protocol_error",
                        f"The printer's {METHOD_LIVING_VIDEO_URL} reply {why}.", reason=reason,
                        method=METHOD_LIVING_VIDEO_URL, **details)


def _keys_seen(result: dict, limit: int = 12) -> str:
    """Key names a reply carried, for a refusal that has to say what arrived. Keys only — never
    values, which is where a signed or credential-bearing address would ride out in plain text."""
    return ", ".join(sorted(str(key) for key in result)[:limit]) or "(no keys at all)"


def _safe_scheme(word: Any) -> str:
    """A scheme word that is safe to print. A reply with a hostile scheme is exactly the reply whose
    text nobody has vetted, so anything off this shape is reported as unreadable, not echoed."""
    if not isinstance(word, str):
        return "(unreadable)"
    head = word[:_SCHEME_SHAPES]
    return head if head and head.replace("+", "").replace("-", "").replace(".", "").isalnum() \
        else "(unreadable)"


# --------------------------------------------------------------------------- the address


@dataclass(frozen=True)
class ScrubbedUrl:
    """A camera address that this server is willing to repeat, with everything a reader needs to judge
    it: what scheme and host kind it turned out to be, whether it is the printer this session is talking
    to, and whether anything was withheld from it. ``url`` is the only piece of the printer's own text
    that ever leaves here, and it is None whenever the vetting refused."""
    url: str | None = None
    scheme: str | None = None
    host: str | None = None
    port: int | None = None
    host_kind: str | None = None        # lan | loopback | link_local | public | name_not_resolved
    same_host_as_printer: bool | None = None
    rewritten: bool = False
    withheld_params: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def _host_kind(host: str | None) -> str | None:
    """What kind of host this is, decided from the literal text with no lookup of any kind. A name is
    reported as a name-not-resolved: resolving it would be a network call, and this module makes none.
    The categories mirror the transport's own address policy (private LAN or loopback only), which is
    why a public host is a warning rather than a refusal — this server never connects to it, but a
    printer advertising a public camera address is a fact the person needs in front of them."""
    if not host:
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "name_not_resolved"
    if ip.is_loopback:
        return "loopback"
    if ip.is_private:
        return "lan"
    if ip.is_link_local:
        return "link_local"
    return "public"


def scrub_url(value: Any, *, printer_host: str | None = None) -> ScrubbedUrl:
    """Vet the printer's camera address and return the repeatable form of it. Every refusal here is a
    ``Cc2DataError`` that names the problem without quoting the printer's string: an address that will
    not parse is an address whose contents nobody has checked, and it may be carrying a login.

    What a refusal never does is guess the address back together: the community integration builds
    ``http://<printer>:8080/?action=stream`` when the printer sends nothing, and this server does not
    hand out an address the printer did not send."""
    if isinstance(value, bool) or not isinstance(value, str):
        raise _refuse(f"sent its stream address as a {type(value).__name__} rather than as text, so "
                      "there is no address to report and nothing was printed in its place",
                      "url_not_text")
    text = value.strip()
    if len(text) > _MAX_URL_LEN:
        raise _refuse(f"sent a stream address {len(text)} characters long, over this server's "
                      f"{_MAX_URL_LEN} character ceiling for one field, so it is withheld rather than "
                      "cut down to fit — an address is not something to hand back half of",
                      "url_too_long", url_characters=len(text))
    if not text:
        raise _refuse("sent an empty stream address (the url field was there and empty), which is the "
                      "printer saying it has no address to give rather than a picture to show",
                      "url_empty",
                      hint="The community Home Assistant integration papers over exactly this case by "
                           "building http://<printer-ip>:8080/?action=stream itself. This server does "
                           "not: an address the printer never sent is an address this server cannot "
                           "vouch for, and the manual notes port 8080 answers on stock firmware "
                           "anyway. If the printer reports a camera below, that is the interesting "
                           "half of this answer.")
    try:
        parts = urlsplit(text)
        # Control characters are refused outright, not sanitised on the way past: a newline or a NUL
        # inside an address is not a URL anybody can vet, and it is how a string that looks like one
        # gets used as something else wherever it is next pasted.
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
            raise ValueError("control character")
        # A scheme-less or unparseable string is refused before anything is echoed: urlsplit happily
        # puts "user:pass@host" in .path when there is no scheme, so a bare host cannot be vetted.
        if not parts.scheme:
            raise ValueError("no scheme")
    except ValueError:
        raise _refuse(f"sent a stream address that could not be parsed as a URL, so it is withheld "
                      f"rather than repeated unquoted ({len(text)} characters)",
                      "url_unparseable", url_characters=len(text)) from None
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise _refuse(f"sent a stream address with the scheme \"{_safe_scheme(parts.scheme)}\", which "
                      "is not an address this server repeats (the repeatable set is "
                      f"{', '.join(sorted(ALLOWED_SCHEMES))})",
                      "url_scheme", scheme=_safe_scheme(parts.scheme))
    if parts.username or parts.password:
        # A login hidden in the authority is a credential, and it is the one thing in a URL that must
        # never ride out in a reply. Its presence is the whole report.
        raise _refuse("sent a stream address that carries a user name or password in it "
                      "(scheme://user:pass@host), so it is withheld entirely and only its presence is "
                      "reported",
                      "url_has_login",
                      hint="Nothing in the address is repeated, including the host. If the printer "
                           "really serves an authenticated camera, that belongs in a person's own "
                           "viewer configuration and not in a tool reply.")

    host = None
    port: int | None = None
    try:
        host = parts.hostname        # None for a host this function cannot hold
        port = parts.port            # raises for a port that is not a number
    except ValueError:
        raise _refuse("sent a stream address whose host or port could not be read, so it is withheld "
                      f"rather than repeated unquoted ({len(text)} characters)",
                      "url_unparseable", url_characters=len(text)) from None
    if not host:
        raise _refuse("sent a stream address with no host in it, so there is nothing that could be "
                      "connected to and nothing is repeated in its place", "url_no_host")
    kind = _host_kind(host)
    url = text
    withheld: tuple[str, ...] = ()
    rewritten = False
    notes: list[str] = []

    sensitive = [name for name, _ in parse_qsl(parts.query, keep_blank_values=True)
                 if name.lower() in _CREDENTIAL_QUERY_NAMES
                 or any(part in name.lower() for part in _CREDENTIAL_QUERY_PARTS)]
    if sensitive:
        # The name stays and the value does not: a reader can see that the printer's link is signed,
        # which is a fact about it, without the signature riding out of a tool reply.
        pairs = [(name, _WITHHED if name in sensitive else value)
                 for name, value in parse_qsl(parts.query, keep_blank_values=True)]
        url = parts._replace(query=urlencode(pairs, doseq=True)).geturl()
        rewritten = True
        withheld = tuple(sensitive)
        notes.append(f"its query carried parameter(s) {', '.join(sensitive)}: their names are kept and "
                     "their values replaced with the word \"withheld\" here, because those names read "
                     "as credentials, and this server has not checked that the rewritten address still "
                     "does anything")

    if kind == "public":
        notes.append("this is a public address, and this server's own printer policy is LAN-only — "
                     "nothing here connected to it, but a printer advertising a camera address off the "
                     "LAN is worth reading twice")
    elif kind == "link_local":
        notes.append("the address is link-local, which this server's printer policy also refuses to "
                     "route to; nothing here connected to it")
    if parts.port == 8080:
        notes.append("port 8080 is the camera MJPEG port the protocol manual describes: on stock "
                     "firmware it answers every request with the stream whatever the path or query "
                     "says, it requires no authentication, and it usually allows a single viewer")
    return ScrubbedUrl(
        url=url, scheme=parts.scheme.lower(), host=host, port=port, host_kind=kind,
        same_host_as_printer=(None if not host or not printer_host
                              else host == str(printer_host).strip().lower().strip("[]")),
        rewritten=rewritten, withheld_params=withheld, notes=tuple(notes))


# --------------------------------------------------------------------------- the reply


@dataclass(frozen=True)
class Cc2LivingVideo:
    """A 1042 answer, read. ``video`` is the vetted address; ``empty_url`` is the printer's own
    "success, no address", reported as itself rather than as a refusal to invent one; ``field_seen``
    names the field the address came from, because there is a second spelling of this field in the
    wild and which one arrived is the evidence a live test is for."""
    video: ScrubbedUrl | None = None
    empty_url: bool = False
    field_seen: str | None = None
    other_keys: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @classmethod
    def from_result(cls, result: Any, *, printer_host: str | None = None) -> Cc2LivingVideo:
        """A 1042 reply, read. Object, then not a refusal, then ``url``. What is refused, in order: not
        an object; a nonzero ``error_code`` (the printer's own answer, its number kept, which Elegoo's
        own client shows as "Failed to get video stream URL." and this server does not interpret); no
        ``url`` field at all, said with the key names that did arrive and, when ``video_url`` is among
        them, named as the other family's shape rather than read as this one; and any address that will
        not survive vetting (see ``scrub_url``)."""
        if not isinstance(result, dict):
            raise _refuse("was not an object at all", "camera_malformed")
        _check_envelope(result, METHOD_LIVING_VIDEO_URL)
        if "url" not in result:
            named = "video_url" if "video_url" in result else (
                "VideoUrl" if "VideoUrl" in result else None)
            if named:
                raise _refuse(
                    f"carried no url field but did carry {named}, which is the field name the "
                    "community integration and the older websocket command family use for a stream "
                    "address rather than the field Elegoo's own LAN client reads",
                    "other_family_shape",
                    keys=_keys_seen(result),
                    hint="Nothing was read from that field and nothing was asked again. A firmware "
                         "that answers 1042 with video_url instead of url is a fact worth recording "
                         "against this method — as is the scheme-less bare host:port that the "
                         "websocket path returns, which Elegoo's own older code has to prefix with "
                         "http:// before using it.")
            raise _refuse(f"carried no url field, only the key(s) {_keys_seen(result)}",
                          "no_url_field", keys=_keys_seen(result),
                          hint="Nothing was assumed in its place, nothing was fetched and no address "
                               "was constructed. Whether this firmware answers 1042 with an address at "
                               "all, or answers it only when a stream is switched on first, is one of "
                               "the open questions a live test is for.")
        raw = result["url"]
        # A null and a blank string are the same fact — the printer has no address to give — but they
        # are different bytes, and which one arrived is worth keeping for the live capture. Anything
        # else, including a number or a nested object, is scrub_url's refusal with its type named.
        empty = raw is None or (isinstance(raw, str) and not raw.strip())
        blank_note = None
        if empty:
            scrubbed = None
            blank_note = ("the url field arrived as null" if raw is None
                          else "the url field arrived as an empty string")
        else:
            scrubbed = scrub_url(raw, printer_host=printer_host)
        return cls(video=scrubbed, empty_url=empty,
                   field_seen=None if empty else "url",
                   notes=() if blank_note is None else ((
                       blank_note + ", which this server reports as no address rather than as a "
                       "refusal to build one"),),
                   other_keys=tuple(sorted(str(key) for key in result if key != "url")))

    def as_dict(self) -> dict:
        """The reply a caller gets: the address (or the exact reason it is not here), what was withheld
        from it, what kind of address it turned out to be, and the standing statements that this server
        never fetched it, never toggled anything, and has never seen this answer from this printer."""
        out: dict = {
            "method": METHOD_LIVING_VIDEO_URL,
            "url": None,
            "verified_on_this_printer": False,
        }
        notes = list(self.notes)
        if self.empty_url:
            out["url_absent"] = ("the printer answered successfully without an address it would let "
                                 "this server repeat. That is what it sent: this server does not build "
                                 "an address the printer did not send, so there is nothing to look at")
        if self.video is not None:
            v = self.video
            out["url"] = v.url
            out["url_field"] = self.field_seen
            out["url_scheme"] = v.scheme
            out["url_host"] = v.host
            out["url_port"] = v.port
            out["url_host_kind"] = v.host_kind
            out["url_same_host_as_printer"] = v.same_host_as_printer
            out["url_rewritten"] = v.rewritten
            if v.withheld_params:
                out["url_withheld_parameters"] = list(v.withheld_params)
            notes.extend(v.notes)
        notes.append("nothing here fetched, opened, streamed or screenshotted this address, and "
                     "nothing here verified that a picture appears at it")
        if self.video is not None and self.video.host_kind == "name_not_resolved":
            notes.append("the host is a name and was not looked up: this module makes no network calls, "
                         "so its LAN-only policy could not be checked against it")
        out["notes"] = notes
        out["warnings"] = [
            "1042 is read in Elegoo's own LAN client and a toggle in the community manual and the "
            "Home Assistant integration built from it, and no source proves a bare 1042 is inert on "
            "this firmware. Asking it is not established to be free of side effects on the stream.",
            "the protocol manual describes the MJPEG stream on 8080 as needing no authentication and "
            "usually allowing one viewer at a time, so opening the address may be refused, or may "
            "displace whoever is already watching",
        ]
        out["unverified"] = (
            "No 1042 reply has ever been captured from this printer. The field read here is the field "
            "Elegoo's own LAN web client reads from that method; a reply shaped differently is refused "
            "rather than fitted, so a refusal is evidence about this firmware, and a success means "
            "only that the printer advertised an address in that shape.")
        return out


# --------------------------------------------------------------------------- the read


@dataclass(frozen=True)
class Cc2CameraRead:
    """A 1042 answer beside the printer's own camera facts from one read-only 1001 — two sources, kept
    apart, because \"the printer advertised an address\" and \"the printer says a camera is connected
    and is not at its viewer limit\" are different claims with different weights."""
    video: Cc2LivingVideo
    camera_connected: bool | None = None
    video_connections: int | None = None
    max_video_connections: int | None = None
    facts_note: str | None = None

    @property
    def at_viewer_limit(self) -> bool:
        """Do the printer's own two numbers say its video connections are full? Both must arrive and
        the maximum must be a real ceiling; anything else is unknown, never assumed room for one more."""
        return (self.video_connections is not None and self.max_video_connections not in (None, 0)
                and self.video_connections >= self.max_video_connections)

    def as_dict(self) -> dict:
        out = self.video.as_dict()
        facts: dict = {}
        if self.camera_connected is not None:
            facts["camera_connected"] = self.camera_connected
        facts["video_connections"] = self.video_connections
        facts["max_video_connections"] = self.max_video_connections
        out["printer_camera_facts"] = facts
        out["printer_camera_facts_source"] = (
            "one read-only 1001 (GetSystemInfo) asked after the 1042, which is where the protocol puts "
            "these three fields. It is a second request and a second source: it does not confirm the "
            "address and the address does not confirm it.")
        if self.facts_note:
            out["printer_camera_facts_note"] = self.facts_note
        warnings = list(out.get("warnings") or [])
        if self.camera_connected is False:
            warnings.append("the printer says no camera is connected, so an advertised address here is "
                            "worth reading twice")
        if self.at_viewer_limit:
            warnings.append(f"the printer reports {self.video_connections} of "
                            f"{self.max_video_connections} video connections in use, so a viewer "
                            "started now may be refused or may displace the existing one")
        out["warnings"] = warnings
        return out


def camera_params() -> dict:
    """The 1042 request parameters: none, and there is no argument for any — including no ``enable``,
    which would be a toggle. Elegoo's LAN client sends ``{method: 1042}`` with no params key at all;
    this transport's envelope always carries a ``params`` object, empty, which is the shape this
    firmware has answered for every other parameterless method here (1001, 1002, 1036, 2005). Whether
    it cares about absent versus empty is one of the things a live test watches."""
    return {}


async def read_camera(transport: Any, *, timeout_s: float | None = None,
                      with_camera_facts: bool = True) -> Cc2CameraRead:
    """Ask the printer for its camera address (method 1042, no parameters) exactly once and vet what
    comes back, then ask one read-only 1001 for the printer's own camera facts. One 1042, never in
    another spelling, never again after a refusal or a timeout; no ``enable`` is ever sent, nothing is
    fetched, and a dead session is asked nothing further. A 1001 that fails never costs the 1042
    answer: it leaves a note beside it. The wait is the official caller's own window (its class default,
    since it passes no override) taken as the longer of that and this transport's default."""
    params = camera_params()
    timeout = CAMERA_TIMEOUT_S if timeout_s is None else timeout_s
    printer_host = getattr(transport, "host", None)
    # One request, and whatever the transport says about it goes straight out: a timeout means the
    # request may have landed, and a dead session cannot be asked a second question. Nothing here
    # retries, and no follow-up 1001 is sent on either path — the parsing below only runs on a reply.
    result = await transport.request(METHOD_LIVING_VIDEO_URL, params, timeout_s=timeout)
    try:
        video = Cc2LivingVideo.from_result(result, printer_host=printer_host)
    except Cc2DataError as e:
        # The printer's own camera facts are the useful half of a refusal: "it has no camera attached"
        # and "it refused the question" are different answers, so one read-only 1001 is still asked and
        # what it says rides along inside the error instead of replacing it.
        if with_camera_facts:
            facts = await _camera_facts(transport)
            if facts is not None:
                e.details.setdefault("printer_camera_facts", facts)
        raise
    camera_connected = video_connections = max_video = None
    facts_note = None
    if with_camera_facts:
        facts = await _camera_facts(transport)
        if facts is None:
            facts_note = ("the one read-only 1001 asked afterwards gave no readable answer, so the "
                          "printer's own camera_connected and viewer-count fields are unknown — that "
                          "is silence about the camera, not a statement about it")
        elif "note" in facts:
            facts_note = ("the one read-only 1001 asked afterwards refused, so the printer's own "
                          "camera_connected and viewer-count fields are unknown: silence about the "
                          f"camera, not a statement about it. It said: {facts['note']}")
        else:
            camera_connected = facts.get("camera_connected")
            video_connections = facts.get("video_connections")
            max_video = facts.get("max_video_connections")
    return Cc2CameraRead(video=video, camera_connected=camera_connected,
                         video_connections=video_connections, max_video_connections=max_video,
                         facts_note=facts_note)


async def _camera_facts(transport: Any) -> dict | None:
    """One read-only 1001, read by the information reader that already exists for it, narrowed to the
    three fields that bear on a camera. A refusal comes back as ``{"note": ...}`` so the caller can say
    which silence it was; a session that gave no answer at all comes back as None. Neither is ever read
    as "no camera": an unanswered question and a negative answer are different facts."""
    try:
        attrs = await fetch_attributes(transport)
    except PrinterError as e:
        return {"note": e.message[:NOTE_CAP]}
    except Exception:  # a bug in this second read must not swallow the 1042 answer it sits beside
        return None
    return {
        "camera_connected": attrs.camera_connected,
        "video_connections": attrs.video_connections,
        "max_video_connections": attrs.max_video_connections,
    }
