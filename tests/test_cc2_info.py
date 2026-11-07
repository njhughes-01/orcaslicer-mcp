"""The three CC2 information reads (1001 attributes, 1046 file detail, 1045 file thumbnail) against
scripted replies, in the same house style as the state, control and peripheral tests: nothing opens a
socket, the transport is a fake that records every method, its params and the timeout each request
asked for, and every frame it answers is either the observed shape from test_cc2_state/test_server_cc2
or a shape the protocol doc publishes (the 1001 attributes sample and the 1046 measured reply, the
1045 base64 frame). What the tests pin down:

* the wire shape each method wants, spelled exactly — 1001 with no params, 1046 with **filename**,
  1045 with **file_name** — because these two file methods do not share a spelling and the protocol
  manual records having published 1045 wrong until Elegoo's own client was read. These two asserts are
  the guard against a future "consistency" rename reintroducing the bug;
* only ``local`` is ever asked for, with the two removable-media spellings refused from the sources
  before a request exists, and a file name held to the listing's own bar (a path, a control character
  or a padded name refuses with nothing on the wire, while any name the listing could print, including
  spaces, unicode and 255 characters, is forwarded byte for byte);
* nothing is assumed about a field that did not arrive: absent, empty-string (the measured empty
  ``mcu_version`` case) and unreadable all read as None and are named in ``not_reported``, the printer's
  ``ip``/``mac`` are never carried, a 1001 or 1046 that answered none of the documented fields is
  refused instead of reported as an empty profile, and the layer count reports the field name that
  answered — with a warning when two names disagree, never an average;
* a 1046 that answers about a different file than the one asked for says so, in words and with both
  names, rather than being handed over as an answer about this file;
* the thumbnail is decoded, typed and bounded, never cut: a data-URI prefix and embedded line breaks
  are tolerated because the sources show both, everything else is decoded strictly so a damaged body
  refuses instead of becoming a corrupt picture, the image type comes from the bytes' own signature
  (png/jpeg/gif/webp) and anything else refuses rather than wearing a guessed type, and an image over
  the size limit is refused with its size named — both before decoding, on the base64 length, and
  after it, on the byte count — with nothing sent a second time;
* a reply that does not arrive stays lost (one request, the transport's timeout untouched, never
  resent), and a refusal keeps the printer's own ``error_code``: 1003 is 1003, never renamed "file
  not found";
* no host, no serial-of-another-printer, no credential and no local path rides in any reply."""
from __future__ import annotations

import base64
import copy
import json
import struct
import zlib
from collections import deque

import pytest

from orcaslicer_mcp.printer import cc2_info as info
from orcaslicer_mcp.printer import cc2_state as st
from orcaslicer_mcp.printer.cc2_transport import Cc2TimeoutError, PrinterError

HOST = "192.0.2.10"                   # documented TEST-NET, like the whole lane; never a live address
SN = "CC2TESTSN0001"
FAKE_CODE = "SIMULATED-NOT-A-CODE"    # a stand-in credential, proven never to ride any reply
JOB = "benchy-petg-2026-11-04.gcode"


class FakeTransport:
    """A registered session that records and answers: one entry per method, a callable answering per
    call, a deque shifting (last sticks), an exception raising as given, and an unscripted method
    answering ``{}`` — which is the all-null trap these tests watch for."""

    def __init__(self, replies=None, *, host=HOST, sn=SN, display="CC2-test"):
        self.host, self.sn, self.display = host, sn, display
        self.replies = dict(replies or {})
        self.requests: list[tuple[int, dict, float | None]] = []
        self.closed = False

    async def request(self, method: int, params: dict | None = None, *, timeout_s=None) -> dict:
        self.requests.append((method, copy.deepcopy(params or {}), timeout_s))
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
        return [m for m, _, _ in self.requests]

    def one(self, method: int) -> tuple[dict, float | None]:
        """The single call this method got — params and the timeout it asked for. Asserting through
        this is what makes "asked exactly once" part of every wire-shape test."""
        calls = [(p, t) for m, p, t in self.requests if m == method]
        assert len(calls) == 1, f"method {method} was asked {len(calls)} times: {self.requests}"
        return calls[0]


# --------------------------------------------------------------------------- documented frames

ATTRS = {  # the protocol doc's attributes sample, as this printer is documented answering
    "error_code": 0, "hostname": "Carbono", "machine_model": "Centauri Carbon 2", "sn": SN,
    "ip": HOST, "mac": "AA:BB:CC:DD:EE:FF", "protocol_version": "1.0.0",
    "hardware_version": "1.0",
    "software_version": {"ota_version": "02.01.01.07", "mcu_version": "00.00.00.00",
                         "soc_version": ""},
    "resolution": "1920x1080", "xyz_size": "220x220x250", "network_type": "wifi",
    "usb_connected": False, "camera_connected": True, "remaining_memory": 1073741824,
    "max_video_connections": 1, "video_connections": 0,
}

DETAIL = {  # the 1046 reply exactly as the protocol doc records it from stock-firmware testing
    "error_code": 0, "filename": JOB, "size": 5712759, "layer": 722, "print_time": 4690,
    "total_filament_used": 24.8, "color_map": [{"color": "0B6283", "name": "PLA", "t": 3}],
    "create_time": 1772820155, "last_print_time": 0, "total_print_times": 0,
}


def png_bytes(width: int = 2, height: int = 2, rgb: bytes = b"\x00\xb0\xf0") -> bytes:
    """A real, minimal PNG — so the image tests carry bytes a client could actually open, not a
    prefix pretending to be one."""
    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + rgb * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG = png_bytes()
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20
GIF = b"GIF89a" + b"\x00" * 20
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 20


def b64(data: bytes, *, wrap: int = 0) -> str:
    text = base64.b64encode(data).decode()
    if wrap:
        text = "\n".join(text[i:i + wrap] for i in range(0, len(text), wrap))
    return text


def thumb_of(data: bytes, **over) -> dict:
    return {"error_code": 0, "thumbnail": b64(data), **over}


# --------------------------------------------------------------------------- 1. the wire shapes


async def test_attributes_are_asked_with_no_parameters_at_all():
    t = FakeTransport({info.METHOD_ATTRIBUTES: ATTRS})
    await info.fetch_attributes(t)
    params, timeout_s = t.one(info.METHOD_ATTRIBUTES)
    # the protocol's methods table spells 1001 with "None" params and Elegoo's own client sends the
    # method and nothing else; {} is what the transport publishes for a paramless request, exactly as
    # the already-observed 1002 read sends it
    assert params == {}
    assert timeout_s is None            # the transport's own default, not a number invented here
    assert t.methods() == [info.METHOD_ATTRIBUTES]


async def test_file_detail_is_asked_with_filename_and_local_storage():
    t = FakeTransport({info.METHOD_FILE_DETAIL: DETAIL})
    await info.fetch_file_detail(t, JOB)
    params, timeout_s = t.one(info.METHOD_FILE_DETAIL)
    assert params == {"storage_media": "local", "filename": JOB}
    assert timeout_s is None


async def test_file_thumbnail_is_asked_with_file_name_and_local_storage():
    t = FakeTransport({info.METHOD_FILE_THUMBNAIL: thumb_of(PNG)})
    await info.fetch_file_thumbnail(t, JOB)
    params, _ = t.one(info.METHOD_FILE_THUMBNAIL)
    assert params == {"storage_media": "local", "file_name": JOB}


def test_the_two_file_methods_do_not_share_a_parameter_spelling():
    """The trap this module exists to get right, written twice so a rename has to break a test: 1046
    is sent ``filename`` and 1045 is sent ``file_name``. The protocol manual corrected its own 1045
    section to say so after reading Elegoo's working client, and a client written from the old table
    got a 1003 for a file that exists. ``file_path`` is a third shape, sent by the web client for
    1047, which this module does not implement."""
    assert info.detail_params(JOB) == {"storage_media": "local", "filename": JOB}
    assert info.thumbnail_params(JOB) == {"storage_media": "local", "file_name": JOB}
    assert "file_name" not in info.detail_params(JOB)
    assert "filename" not in info.thumbnail_params(JOB)


async def test_a_caller_supplied_timeout_reaches_the_request():
    t = FakeTransport({info.METHOD_ATTRIBUTES: ATTRS, info.METHOD_FILE_DETAIL: DETAIL,
                       info.METHOD_FILE_THUMBNAIL: thumb_of(PNG)})
    await info.fetch_attributes(t, timeout_s=1.5)
    await info.fetch_file_detail(t, JOB, timeout_s=2.5)
    await info.fetch_file_thumbnail(t, JOB, timeout_s=3.5)
    assert [t_s for _, _, t_s in t.requests] == [1.5, 2.5, 3.5]


def test_the_methods_are_the_protocol_owns_numbers():
    # 1001 GET_ATTRIBUTES, 1045 GET_FILE_THUMBNAIL, 1046 GET_FILE_DETAIL — the manual's table, the
    # integration's const.py and the web client's own enum agree on all three
    assert (info.METHOD_ATTRIBUTES, info.METHOD_FILE_THUMBNAIL, info.METHOD_FILE_DETAIL) == (
        1001, 1045, 1046)


# --------------------------------------------------------------------------- 2. the name bar

BAD_NAMES = {
    "empty": "",
    "path-slash": "dir/" + JOB,
    "path-backslash": "dir\\" + JOB,
    "absolute": "/etc/passwd",
    "parent-dir": "..",
    "dot": ".",
    "leading-space": " " + JOB,
    "trailing-space": JOB + " ",
    "tab-inside": "bench\ty.gcode",
    "newline-inside": "bench\ny.gcode",
    "delete-char": "bench\x7fy.gcode",
    "too-long": "a" * 256,
    "not-text": 42,
    "none": None,
    "list": [JOB],
}


@pytest.mark.parametrize("why", list(BAD_NAMES), ids=list(BAD_NAMES))
async def test_a_name_the_printers_own_listing_could_never_print_refuses_before_the_wire(why):
    t = FakeTransport({})
    for fetch in (lambda: info.fetch_file_detail(t, BAD_NAMES[why]),
                  lambda: info.fetch_file_thumbnail(t, BAD_NAMES[why])):
        with pytest.raises(PrinterError) as exc:
            await fetch()
        assert exc.value.details["reason"] == "name_rejected"
        assert "cc2_list_files" in exc.value.hint     # it says where a real name comes from
    assert t.requests == []                          # nothing was asked of any printer


@pytest.mark.parametrize("name", [
    JOB, "Benchy.GCODE", "two words.gcode", "  ".join(["a.b"]), "ünïcødé-ghéã.gcode", "a" * 255,
    "hash#and-percent%.gcode", "1970-01-01 (1).gcode",
], ids=["observed", "uppercase", "spaces", "inner-space", "unicode", "max-length", "punctuation",
        "parens"])
async def test_any_name_the_listing_can_show_is_forwarded_exactly(name):
    # the bar is the listing's bar and no stricter: these tools compare exactly, so a name is never
    # case-folded, trimmed, normalised or shortened on the way to the printer
    t = FakeTransport({info.METHOD_FILE_DETAIL: dict(DETAIL, filename=name),
                       info.METHOD_FILE_THUMBNAIL: thumb_of(PNG)})
    detail = await info.fetch_file_detail(t, name)
    shot = await info.fetch_file_thumbnail(t, name)
    assert t.one(info.METHOD_FILE_DETAIL)[0] == {"storage_media": "local", "filename": name}
    assert t.one(info.METHOD_FILE_THUMBNAIL)[0] == {"storage_media": "local", "file_name": name}
    assert detail.filename == name and detail.requested == name
    assert detail.as_dict()["name_matches_request"] is True
    assert shot.filename == name


# --------------------------------------------------------------------------- 3. storage media


@pytest.mark.parametrize("media", ["u-disk", "udisk", "sd-card", "sdcard", "U-DISK", "local ", "",
                                   None, 0])
async def test_a_storage_nobody_has_asked_this_printer_about_refuses(media):
    # the measured 1045/1046 capture used "local" as "the only value tried", and the sources do not
    # even spell the others the same way, so guessing a spelling would be inventing a wire shape
    t = FakeTransport({})
    with pytest.raises(PrinterError) as exc:
        await info.fetch_file_detail(t, JOB, storage=media)
    assert exc.value.details["reason"] == "storage_unsupported"
    assert "local" in exc.value.message
    with pytest.raises(PrinterError) as exc:
        await info.fetch_file_thumbnail(t, JOB, storage=media)
    assert exc.value.details["reason"] == "storage_unsupported"
    assert t.requests == []


async def test_local_is_the_default_and_needs_no_argument():
    t = FakeTransport({info.METHOD_FILE_DETAIL: DETAIL})
    await info.fetch_file_detail(t, JOB)
    assert t.one(info.METHOD_FILE_DETAIL)[0]["storage_media"] == "local"


# --------------------------------------------------------------------------- 4. the envelope


@pytest.mark.parametrize("code", [1003, 1002, 1010, 1, -1])
@pytest.mark.parametrize("method", ["detail", "thumbnail", "attributes"])
async def test_a_refusal_keeps_the_printers_own_code_and_is_never_read_as_a_result(
        method, code):
    t = FakeTransport({info.METHOD_FILE_DETAIL: {"error_code": code},
                       info.METHOD_FILE_THUMBNAIL: {"error_code": code},
                       info.METHOD_ATTRIBUTES: {"error_code": code}})
    fetch = {"detail": lambda: info.fetch_file_detail(t, JOB),
             "thumbnail": lambda: info.fetch_file_thumbnail(t, JOB),
             "attributes": lambda: info.fetch_attributes(t)}[method]
    with pytest.raises(st.Cc2DataError) as exc:
        await fetch()
    assert exc.value.details["error_code"] == code     # 1003 stays 1003: nobody has established what
    assert exc.value.details["method"] in (1001, 1045, 1046)   # it means per-file on this firmware
    assert len(t.requests) == 1                        # and it is never asked again to find out


@pytest.mark.parametrize("code", [True, False, 0.0, "0", None, [], {}],
                         ids=["true", "false", "float-zero", "text-zero", "null", "list", "dict"])
async def test_an_error_code_that_cannot_be_read_refuses_rather_than_becoming_zero(code):
    # `_check_envelope` is the rule every CC2 read uses; 0 invented by a default would turn a reply
    # the printer did not vouch for into a success
    t = FakeTransport({info.METHOD_ATTRIBUTES: {"error_code": code},
                       info.METHOD_FILE_DETAIL: {"error_code": code, "size": 1},
                       info.METHOD_FILE_THUMBNAIL: {"error_code": code, "thumbnail": b64(PNG)}})
    for fetch in (lambda: info.fetch_attributes(t), lambda: info.fetch_file_detail(t, JOB),
                  lambda: info.fetch_file_thumbnail(t, JOB)):
        with pytest.raises(st.Cc2DataError):
            await fetch()
    assert len(t.requests) == 3                        # one each, no retries


@pytest.mark.parametrize("bad", ["", "not json", 0, 1, 1.0, [], [1]],
                         ids=["empty-text", "text", "zero", "one", "float", "empty-list", "list"])
async def test_a_reply_that_is_not_an_object_at_all_refuses(bad):
    t = FakeTransport({info.METHOD_ATTRIBUTES: bad, info.METHOD_FILE_DETAIL: bad,
                       info.METHOD_FILE_THUMBNAIL: bad})
    for fetch in (lambda: info.fetch_attributes(t), lambda: info.fetch_file_detail(t, JOB),
                  lambda: info.fetch_file_thumbnail(t, JOB)):
        with pytest.raises(st.Cc2DataError):
            await fetch()


async def test_a_missing_error_code_reads_as_success_like_every_other_cc2_read():
    # the observed success answers carry no code at all, and the shared envelope gate treats absence
    # as silence, not as a refusal
    t = FakeTransport({info.METHOD_ATTRIBUTES: {k: v for k, v in ATTRS.items()
                                                if k != "error_code"}})
    attrs = await info.fetch_attributes(t)
    assert attrs.model == "Centauri Carbon 2"


# --------------------------------------------------------------------------- 5. attributes (1001)


async def test_the_attributes_reply_carries_what_the_printer_answered_and_nothing_else():
    t = FakeTransport({info.METHOD_ATTRIBUTES: ATTRS})
    out = (await info.fetch_attributes(t)).as_dict()
    assert out["printer"] == {"model": "Centauri Carbon 2", "name": "Carbono", "serial": SN}
    assert out["firmware"] == {"ota_version": "02.01.01.07", "mcu_version": "00.00.00.00",
                               "soc_version": None}
    assert out["hardware"] == {"hardware_version": "1.0", "protocol_version": "1.0.0"}
    assert out["build"] == {"resolution": "1920x1080", "xyz_size": "220x220x250",
                            "remaining_memory_bytes": 1073741824}
    assert out["network"] == {"type": "wifi"}
    assert out["attachments"] == {"camera_connected": True, "usb_connected": False,
                                  "video_connections": 0, "max_video_connections": 1}
    # soc_version arrived empty — not a version — and the empty version is named, not swallowed
    assert out["not_reported"] == ["software_version.soc_version"]


async def test_the_ip_and_the_mac_are_not_carried_and_never_the_raw_reply():
    # the printer answers them; this server declines to pass them on: nothing here addresses the
    # printer (the user set the address) and a MAC is a permanent hardware identifier with no job
    t = FakeTransport({info.METHOD_ATTRIBUTES: ATTRS})
    out = (await info.fetch_attributes(t)).as_dict()
    text = json.dumps(out)
    assert ATTRS["ip"] not in text and HOST not in text
    assert ATTRS["mac"] not in text
    assert "mac" not in text
    assert "raw" not in text          # the reply as it arrived is not what a caller is handed


async def test_the_measured_empty_version_strings_read_as_silence_not_as_a_version():
    # the one real capture of this printer reported mcu_version and soc_version empty
    t = FakeTransport({info.METHOD_ATTRIBUTES: {
        **ATTRS, "software_version": {"ota_version": "02.01.01.07", "mcu_version": "",
                                      "soc_version": ""}}})
    attrs = await info.fetch_attributes(t)
    out = attrs.as_dict()
    assert out["firmware"] == {"ota_version": "02.01.01.07", "mcu_version": None,
                               "soc_version": None}
    assert set(out["not_reported"]) == {"software_version.mcu_version",
                                       "software_version.soc_version"}


async def test_fields_that_never_arrived_are_null_and_named():
    # an absent camera is not "no camera" and an absent version is not "up to date": a null plus a
    # line in not_reported is the only honest reading, and it is what stops a model inventing a fact
    t = FakeTransport({info.METHOD_ATTRIBUTES: {"error_code": 0, "machine_model": "X",
                                                "software_version": {}}})
    out = (await info.fetch_attributes(t)).as_dict()
    assert out["printer"]["model"] == "X"
    assert out["attachments"]["camera_connected"] is None
    assert out["attachments"]["usb_connected"] is None
    assert out["attachments"]["video_connections"] is None
    assert out["build"]["xyz_size"] is None
    assert "camera_connected" in out["not_reported"]
    assert "xyz_size" in out["not_reported"]
    assert "software_version.ota_version" in out["not_reported"]


async def test_a_flag_or_a_count_that_cannot_be_read_is_unknown_not_false_or_zero():
    # one readable field keeps this a profile; every unreadable one stays null and named. A float, a
    # bool, a negative number and a word are not measurements of the things they arrived for.
    t = FakeTransport({info.METHOD_ATTRIBUTES: {
        "error_code": 0, "machine_model": "Centauri Carbon 2",
        "camera_connected": "yes", "usb_connected": 2,
        "remaining_memory": -1, "video_connections": True, "max_video_connections": 1.0,
        "hostname": "  ", "xyz_size": 5}})
    attrs = await info.fetch_attributes(t)
    out = attrs.as_dict()
    assert out["attachments"] == {"camera_connected": None, "usb_connected": None,
                                  "video_connections": None, "max_video_connections": None}
    assert out["build"]["remaining_memory_bytes"] is None
    assert out["printer"]["name"] is None          # whitespace is not a printer name
    assert out["build"]["xyz_size"] is None        # a number is not an "WxHxD" size
    for key in ("camera_connected", "usb_connected", "remaining_memory", "video_connections",
                "max_video_connections", "hostname", "xyz_size"):
        assert key in out["not_reported"], key


async def test_a_reply_answering_none_of_the_documented_fields_refuses_instead_of_reporting_nothing():
    # a stub {} reply would otherwise come back as a profile of nulls and be read as "an unknown
    # printer", which is a failure dressed as a result
    t = FakeTransport({info.METHOD_ATTRIBUTES: {"error_code": 0}})
    with pytest.raises(st.Cc2DataError) as exc:
        await info.fetch_attributes(t)
    assert exc.value.details["reason"] == "reply_empty"
    assert "error_code" in exc.value.message       # it names the only key it did get
    assert "machine_model" not in exc.value.message


async def test_the_software_version_block_being_wrong_is_still_silence_not_an_error():
    t = FakeTransport({info.METHOD_ATTRIBUTES: {**ATTRS, "software_version": "02.01.01.07"}})
    attrs = await info.fetch_attributes(t)
    out = attrs.as_dict()
    assert out["firmware"]["ota_version"] is None   # a string where an object belongs answers nothing
    assert "software_version.ota_version" in out["not_reported"]


async def test_the_serial_is_cross_checked_against_the_serial_this_session_pinned():
    agreed = FakeTransport({info.METHOD_ATTRIBUTES: ATTRS}, sn=SN)
    assert (await info.fetch_attributes(agreed)).serial_agree is True
    other = FakeTransport({info.METHOD_ATTRIBUTES: dict(ATTRS, sn="SOMETHINGELSE")}, sn=SN)
    out = (await info.fetch_attributes(other)).as_dict()
    assert out["identity"]["serial_agree"] is False
    assert "may not describe" in out["identity"]["warning"]
    # and when either side is unknown, the answer is unknown, never an assumed agreement
    unknown = FakeTransport({info.METHOD_ATTRIBUTES: {k: v for k, v in ATTRS.items() if k != "sn"}})
    assert (await info.fetch_attributes(unknown)).serial_agree is None


async def test_attributes_answer_the_transport_timeout_untouched_and_ask_once():
    t = FakeTransport({info.METHOD_ATTRIBUTES: Cc2TimeoutError(
        "protocol_error", "The printer did not answer method 1001 within 10s.", reason="timeout",
        ambiguous=True)})
    with pytest.raises(Cc2TimeoutError) as exc:
        await info.fetch_attributes(t)
    assert exc.value.details["ambiguous"] is True       # the transport's own honest word, kept whole
    assert len(t.requests) == 1


# --------------------------------------------------------------------------- 6. file detail (1046)


async def test_the_measured_detail_reply_is_read_field_for_field():
    t = FakeTransport({info.METHOD_FILE_DETAIL: DETAIL})
    out = (await info.fetch_file_detail(t, JOB)).as_dict()
    assert out["requested"] == JOB and out["name"] == JOB
    assert out["name_matches_request"] is True
    assert out["size_bytes"] == 5712759
    assert out["layers"] == 722 and out["layers_from_field"] == "layer"
    assert out["est_print_s"] == 4690                    # the slicer's estimate, in seconds
    assert out["est_filament_g"] == pytest.approx(24.8)  # grams
    assert out["total_print_times"] == 0
    assert out["last_printed"] is None and out["never_printed"] is True   # 0 is the doc's word
    assert out["filaments"] == [{"tool": 3, "color": "#0B6283", "name": "PLA"}]
    assert out["created"].startswith("2026-")
    assert out["not_reported"] == []


async def test_the_reply_uses_the_listings_own_words_for_the_same_facts():
    # a model should not have to learn two vocabularies for one file: these keys are cc2_list_files'
    t = FakeTransport({info.METHOD_FILE_DETAIL: DETAIL})
    out = (await info.fetch_file_detail(t, JOB)).as_dict()
    for key in ("name", "size_bytes", "layers", "est_print_s", "est_filament_g", "created"):
        assert key in out, key


@pytest.mark.parametrize("field", ["layer", "TotalLayers", "total_layer"])
async def test_every_documented_layer_spelling_is_understood_and_named(field):
    body = {k: v for k, v in DETAIL.items() if k not in ("layer", "TotalLayers", "total_layer")}
    body[field] = 500
    t = FakeTransport({info.METHOD_FILE_DETAIL: body})
    out = (await info.fetch_file_detail(t, JOB)).as_dict()
    assert out["layers"] == 500
    assert out["layers_from_field"] == field        # and which name answered stays on the record


async def test_two_layer_names_that_disagree_are_said_not_averaged():
    t = FakeTransport({info.METHOD_FILE_DETAIL: {**DETAIL, "layer": 722, "TotalLayers": 700}})
    out = (await info.fetch_file_detail(t, JOB)).as_dict()
    assert out["layers_conflict"] == [{"field": "layer", "value": 722},
                                      {"field": "TotalLayers", "value": 700}]
    assert out["layers"] == 722                     # the first preferred name, declared as such
    assert out["layers_from_field"] == "layer"
    assert any("disagree" in w for w in out["warnings"])


async def test_answering_about_another_file_is_said_out_loud():
    # the printer answered with a filename; if it is not the one asked for, this detail may not be
    # about the file the caller named, and quietly dropping that would be the whole point of asking
    t = FakeTransport({info.METHOD_FILE_DETAIL: dict(DETAIL, filename="other-print.gcode")})
    out = (await info.fetch_file_detail(t, JOB)).as_dict()
    assert out["name"] == "other-print.gcode"
    assert out["requested"] == JOB
    assert out["name_matches_request"] is False
    assert any("different file" in w for w in out["warnings"])


async def test_a_detail_with_no_documented_field_at_all_refuses():
    # the Home Assistant client logs this same "no enrichment markers" case and does not cache it;
    # an all-null detail reported as a result would be a failure wearing a profile
    t = FakeTransport({info.METHOD_FILE_DETAIL: {"error_code": 0}})
    with pytest.raises(st.Cc2DataError) as exc:
        await info.fetch_file_detail(t, JOB)
    assert exc.value.details["reason"] == "reply_empty"


async def test_one_answered_field_is_an_answer_and_the_rest_stay_named_nulls():
    t = FakeTransport({info.METHOD_FILE_DETAIL: {"error_code": 0, "filename": JOB}})
    out = (await info.fetch_file_detail(t, JOB)).as_dict()
    assert out["name"] == JOB
    assert out["size_bytes"] is None and out["layers"] is None and out["est_print_s"] is None
    assert "size" in out["not_reported"] and "print_time" in out["not_reported"]
    assert "layer" in out["not_reported"]      # the layer field is named even under its three names


async def test_a_size_of_zero_is_a_size_and_a_missing_size_is_not_zero():
    t = FakeTransport({info.METHOD_FILE_DETAIL: {"error_code": 0, "filename": JOB, "size": 0}})
    assert (await info.fetch_file_detail(t, JOB)).size_bytes == 0
    t2 = FakeTransport({info.METHOD_FILE_DETAIL: {"error_code": 0, "filename": JOB}})
    assert (await info.fetch_file_detail(t2, JOB)).size_bytes is None


@pytest.mark.parametrize("grams", [True, False, "24.8", [24.8], None])
async def test_a_filament_weight_that_cannot_be_read_is_none(grams):
    t = FakeTransport({info.METHOD_FILE_DETAIL: {**DETAIL, "total_filament_used": grams}})
    assert (await info.fetch_file_detail(t, JOB)).filament_grams is None


async def test_a_colour_that_is_not_a_colour_refuses_the_reply_rather_than_half_reading_the_file():
    # the same rule cc2_list_files holds, so the two reads cannot drift
    t = FakeTransport({info.METHOD_FILE_DETAIL: {
        **DETAIL, "color_map": [{"color": "plastic-blue", "name": "PLA", "t": 0}]}})
    with pytest.raises(st.Cc2DataError) as exc:
        await info.fetch_file_detail(t, JOB)
    assert exc.value.details["reason"] == "detail_malformed"


@pytest.mark.parametrize("map_", [{}, "color_map", 1, None], ids=["object", "text", "one", "null"])
async def test_a_colour_map_that_is_not_a_list_refuses_unless_it_is_absent(map_):
    if map_ is None:
        t = FakeTransport({info.METHOD_FILE_DETAIL: {**DETAIL, "color_map": None}})
        assert (await info.fetch_file_detail(t, JOB)).filaments == ()   # absent: nothing to read
        return
    t = FakeTransport({info.METHOD_FILE_DETAIL: {**DETAIL, "color_map": map_}})
    with pytest.raises(st.Cc2DataError):
        await info.fetch_file_detail(t, JOB)


async def test_a_colour_map_entry_that_is_not_an_object_refuses():
    t = FakeTransport({info.METHOD_FILE_DETAIL: {**DETAIL, "color_map": ["#00B0F0"]}})
    with pytest.raises(st.Cc2DataError):
        await info.fetch_file_detail(t, JOB)


@pytest.mark.parametrize("tool", [-1, True, "0", 0.5], ids=["negative", "bool", "text", "half"])
async def test_a_tool_index_that_could_not_come_from_the_gcode_is_none_never_tool_zero(tool):
    # and the field is called `tool`, because color_map[].t is the G-code tool index, not a CANVAS
    # tray — the manual says so twice and the two are easy to confuse
    t = FakeTransport({info.METHOD_FILE_DETAIL: {
        **DETAIL, "color_map": [{"color": "#00B0F0", "name": "PLA", "t": tool}]}})
    out = (await info.fetch_file_detail(t, JOB)).as_dict()
    assert out["filaments"] == [{"tool": None, "color": "#00B0F0", "name": "PLA"}]
    assert "tray" not in json.dumps(out)


async def test_a_timestamp_the_calendar_cannot_hold_is_no_time():
    t = FakeTransport({info.METHOD_FILE_DETAIL: {**DETAIL, "create_time": 10 ** 18,
                                                 "last_print_time": 10 ** 18}})
    detail = await info.fetch_file_detail(t, JOB)
    assert detail.created is None
    assert detail.last_printed is None
    assert detail.last_print_time == 10 ** 18        # the number itself is still reported


async def test_last_print_time_zero_is_never_printed_but_an_absent_one_is_unknown():
    printed = await info.fetch_file_detail(
        FakeTransport({info.METHOD_FILE_DETAIL: {**DETAIL, "last_print_time": 1772900000,
                                                 "total_print_times": 2}}), JOB)
    assert printed.last_printed is not None
    assert printed.as_dict()["never_printed"] is False
    unknown = await info.fetch_file_detail(
        FakeTransport({info.METHOD_FILE_DETAIL: {k: v for k, v in DETAIL.items()
                                                 if k != "last_print_time"}}), JOB)
    assert unknown.as_dict()["never_printed"] is None


# --------------------------------------------------------------------------- 7. thumbnail (1045)


@pytest.mark.parametrize("data, kind", [
    (PNG, "png"), (JPEG, "jpeg"), (GIF, "gif"), (WEBP, "webp"),
], ids=["png", "jpeg", "gif", "webp"])
async def test_the_image_type_comes_from_the_bytes_own_signature(data, kind):
    t = FakeTransport({info.METHOD_FILE_THUMBNAIL: thumb_of(data)})
    shot = await info.fetch_file_thumbnail(t, JOB)
    assert shot.kind == kind and shot.data == data


async def test_a_type_this_server_has_not_established_refuses_rather_than_wearing_a_guessed_one():
    # the official web client stamps every thumbnail data:image/png whatever it received; the honest
    # reply names the bytes it actually got and sends no image on under a lie
    t = FakeTransport({info.METHOD_FILE_THUMBNAIL: thumb_of(b"BM" + b"\x00" * 40)})
    with pytest.raises(st.Cc2DataError) as exc:
        await info.fetch_file_thumbnail(t, JOB)
    assert exc.value.details["reason"] == "thumbnail_type_unknown"
    assert "424d" in exc.value.message                 # the first bytes, so the next person can name it
    assert "not sent on under a guessed type" in exc.value.message


async def test_a_riff_that_is_not_webp_is_not_called_webp():
    t = FakeTransport({info.METHOD_FILE_THUMBNAIL: thumb_of(b"RIFF\x00\x00\x00\x00AVI ")})
    with pytest.raises(st.Cc2DataError) as exc:
        await info.fetch_file_thumbnail(t, JOB)
    assert exc.value.details["reason"] == "thumbnail_type_unknown"


def test_image_kind_reads_signatures_and_nothing_else():
    assert info.image_kind(PNG) == "png"
    assert info.image_kind(b"") is None
    assert info.image_kind(b"\x89P") is None       # a truncated signature is not a signature
    assert info.image_kind(b"\x00\x01\x02\x03") is None


async def test_a_data_uri_prefix_and_wrapped_lines_are_tolerated_because_the_sources_show_both():
    # the integration defends against a thumbnail that already starts with "data:" and a base64 body
    # may be line-wrapped; both are formatting, never content, so neither is a reason to refuse
    wrapped = {"error_code": 0, "thumbnail": b64(PNG, wrap=24)}
    t = FakeTransport({info.METHOD_FILE_THUMBNAIL: wrapped})
    shot = await info.fetch_file_thumbnail(t, JOB)
    assert shot.data == PNG and shot.kind == "png"

    t2 = FakeTransport({info.METHOD_FILE_THUMBNAIL: {
        "error_code": 0, "thumbnail": "data:image/png;base64," + b64(PNG)}})
    assert (await info.fetch_file_thumbnail(t2, JOB)).data == PNG

    t3 = FakeTransport({info.METHOD_FILE_THUMBNAIL: {
        "error_code": 0, "thumbnail": "DATA:IMAGE/PNG;BASE64," + b64(PNG) + "\n"}})
    assert (await info.fetch_file_thumbnail(t3, JOB)).data == PNG


@pytest.mark.parametrize("value, reason", [
    ("not base64 at all !!!", "thumbnail_malformed"),
    ("data:image/png;base64", "thumbnail_malformed"),
    ("data:image/png;base64,   ", "thumbnail_malformed"),
    ("QQ==QQ==", "thumbnail_malformed"),        # padded base64 glued to itself is not one body
    (1234, "thumbnail_malformed"),
    ([], "thumbnail_malformed"),
    ({"b64": "..."}, "thumbnail_malformed"),
], ids=["not-base64", "uri-no-comma", "uri-empty-body", "double-padded", "int", "list", "object"])
async def test_a_thumbnail_body_that_cannot_be_decoded_refuses_and_is_never_repaired(value, reason):
    # a lenient base64 decode silently DROPS characters the alphabet does not know, which would turn a
    # damaged reply into a valid-looking corrupt picture instead of a refusal
    t = FakeTransport({info.METHOD_FILE_THUMBNAIL: {"error_code": 0, "thumbnail": value}})
    with pytest.raises(st.Cc2DataError) as exc:
        await info.fetch_file_thumbnail(t, JOB)
    assert exc.value.details["reason"] == reason


@pytest.mark.parametrize("result, why", [
    ({"error_code": 0}, "no thumbnail field at all"),
    ({"error_code": 0, "thumbnail": ""}, "an empty thumbnail field"),
    ({"error_code": 0, "thumbnail": None}, "a null thumbnail field"),
    ({"error_code": 0, "thumbnail": "   "}, "whitespace where the body should be"),
], ids=["absent", "empty", "null", "spaces"])
async def test_a_file_with_no_thumbnail_says_so_in_stead_of_sending_a_blank_picture(result, why):
    # the protocol documents that a file with no embedded thumbnail may omit the field, and the
    # integration logs the same reply shape; a blank image would be a claim about the file
    t = FakeTransport({info.METHOD_FILE_THUMBNAIL: result})
    with pytest.raises(st.Cc2DataError) as exc:
        await info.fetch_file_thumbnail(t, JOB)
    assert exc.value.details["reason"] == "no_thumbnail", why
    assert "cc2_file_detail" in exc.value.hint          # it says where the metadata still lives
    assert "cc2_file_detail" in exc.value.message or "cc2_file_detail" in exc.value.hint


def test_the_two_sentences_are_different():
    """"The printer had no thumbnail for this file" and "the printer declined" are different facts and
    must not arrive in the same words: one points at the slicer, the other at the printer's code."""
    quiet = None
    try:
        info.thumbnail_from_result({"error_code": 0}, filename=JOB)
    except st.Cc2DataError as e:
        quiet = e
    assert quiet is not None and quiet.details["reason"] == "no_thumbnail"
    assert "error_code" not in quiet.message
    declined = None
    try:
        info.thumbnail_from_result({"error_code": 1003}, filename=JOB)
    except st.Cc2DataError as e:
        declined = e
    assert declined is not None and declined.details["error_code"] == 1003
    assert "no thumbnail" not in declined.message


# --------------------------------------------------------------------------- 8. thumbnail size


async def test_an_image_bigger_than_one_reply_may_carry_is_refused_with_its_size_named():
    big = "A" * (info._MAX_THUMBNAIL_CHARS + 1)
    t = FakeTransport({info.METHOD_FILE_THUMBNAIL: {"error_code": 0, "thumbnail": big}})
    with pytest.raises(st.Cc2DataError) as exc:
        await info.fetch_file_thumbnail(t, JOB)
    assert exc.value.details["reason"] == "thumbnail_too_large"
    assert "truncated" in exc.value.message
    assert exc.value.details["max_bytes"] == info.MAX_THUMBNAIL_BYTES
    assert exc.value.details["base64_chars"] == info._MAX_THUMBNAIL_CHARS + 1
    assert len(t.requests) == 1                        # never asked for a smaller one either


async def test_the_decoded_size_is_checked_too_not_only_the_wire_length(monkeypatch):
    """The two size guards are not one check written twice. The cheap one reads the base64 length
    before anything is decoded; the exact one reads the bytes after it. With the byte cap lowered to 8
    and the character ceiling left at the import-time 2 MiB figure, a 9-byte image can only be caught
    by the second — which is the proof that it does work rather than a second copy of the first."""
    monkeypatch.setattr(info, "MAX_THUMBNAIL_BYTES", 8)
    assert info._MAX_THUMBNAIL_CHARS == 2796204          # the untouched wire ceiling
    over_by_one = b"\x89PNG\r\n\x1a\n" + b"\x00"        # 9 bytes: one over the patched cap
    assert len(over_by_one) == 9 and len(b64(over_by_one)) == 12
    t = FakeTransport({info.METHOD_FILE_THUMBNAIL: thumb_of(over_by_one)})
    with pytest.raises(st.Cc2DataError) as exc:
        await info.fetch_file_thumbnail(t, JOB)
    assert exc.value.details["reason"] == "thumbnail_too_large"
    assert exc.value.details["bytes"] == 9
    assert "decoded bytes" in exc.value.message          # the byte guard spoke, not the character one
    assert len(t.requests) == 1


async def test_the_boundary_is_carried_whole_and_one_byte_over_is_refused(monkeypatch):
    # a size test that only ever tests an enormous number has not tested the boundary: at the limit the
    # image arrives byte for byte, and one byte over is refused with its size named
    monkeypatch.setattr(info, "MAX_THUMBNAIL_BYTES", 8)
    at_limit = b"\x89PNG\r\n\x1a\n"                    # the signature alone: exactly 8 bytes
    over = at_limit + b"\x00"                             # nine
    assert len(at_limit) == 8 and len(over) == 9
    t = FakeTransport({info.METHOD_FILE_THUMBNAIL: thumb_of(at_limit)})
    shot = await info.fetch_file_thumbnail(t, JOB)
    assert shot.data == at_limit and shot.kind == "png"
    t2 = FakeTransport({info.METHOD_FILE_THUMBNAIL: thumb_of(over)})
    with pytest.raises(st.Cc2DataError) as exc:
        await info.fetch_file_thumbnail(t2, JOB)
    assert exc.value.details["reason"] == "thumbnail_too_large"
    assert exc.value.details["bytes"] == len(over)
    assert len(t2.requests) == 1


def test_the_two_size_guards_are_the_same_limit_seen_twice():
    # the wire-length guard is the byte guard's cheap twin, so an oversized reply is refused before it
    # is decoded and, failing that, after it: neither is decorative, and the test below proves the
    # second one is not redundant
    limit, chars = info.MAX_THUMBNAIL_BYTES, info._MAX_THUMBNAIL_CHARS
    assert limit == 2 * 1024 * 1024
    assert chars == -(-limit // 3) * 4                 # the exact base64 length of a full-size image
    assert len(b64(b"x" * limit)) == chars             # at the cap: not over it, so it is carried
    assert len(b64(b"x" * (limit + 1))) == chars       # one byte over still fits the wire guard —
    assert (limit + 1) > limit                         # which is why the decoded guard has to exist


async def test_a_thumbnail_reply_that_never_arrives_stays_lost():
    t = FakeTransport({info.METHOD_FILE_THUMBNAIL: Cc2TimeoutError(
        "protocol_error", "The printer did not answer method 1045 within 10s.", reason="timeout",
        ambiguous=True)})
    with pytest.raises(Cc2TimeoutError):
        await info.fetch_file_thumbnail(t, JOB)
    assert len(t.requests) == 1


async def test_the_thumbnail_metadata_names_the_file_and_how_much_it_was():
    t = FakeTransport({info.METHOD_FILE_THUMBNAIL: thumb_of(PNG)})
    out = (await info.fetch_file_thumbnail(t, JOB)).as_dict()
    assert out == {"method": 1045, "filename": JOB, "image_kind": "png", "bytes": len(PNG),
                   "base64_chars": len(b64(PNG))}
    assert "thumbnail" not in str(out)                 # the base64 body never rides the metadata


# --------------------------------------------------------------------------- 9. nothing leaks


@pytest.mark.parametrize("media", ["detail", "thumbnail", "attributes"])
async def test_no_host_no_credential_and_no_local_path_rides_any_reply(media):
    t = FakeTransport({info.METHOD_ATTRIBUTES: ATTRS, info.METHOD_FILE_DETAIL: DETAIL,
                       info.METHOD_FILE_THUMBNAIL: thumb_of(PNG)}, host="203.0.113.9")
    if media == "detail":
        out = (await info.fetch_file_detail(t, JOB)).as_dict()
    elif media == "thumbnail":
        out = (await info.fetch_file_thumbnail(t, JOB)).as_dict()
    else:
        out = (await info.fetch_attributes(t)).as_dict()
    text = json.dumps(out)
    assert "203.0.113.9" not in text
    assert HOST not in text
    assert FAKE_CODE not in text
    assert "/home/" not in text and "/tmp/" not in text
