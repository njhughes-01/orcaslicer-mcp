"""The CC2 CANVAS tray filament-info write (method 2003) — its data and its argument bar — in the
house style of the other CC2 control tests: nothing opens a socket, nothing contacts a printer, and
what is pinned is what belongs to this command alone.

* the command's own numbers, the vendor catalog's 50 rows and the three spot rows a caller is most
  likely to send (PLA 0x0000 190-230, PETG 0x0100 230-260, PPS-CF 0x0E01 340-360), with the whole
  table rebuilt from ``filament-catalog.txt`` so a transcription drift between the vendor bundle and
  the embedded table fails a test rather than a print;
* the frame: nine fields, in the vendor dialog's own order, with the byte-for-byte JSON pinned, the
  write's ``filament_min_temp``/``filament_max_temp`` spelling held apart from the 2005 read's
  ``min_nozzle_temp``/``max_nozzle_temp``, and ``parse_request``'s own signature held to the five
  values a caller may pass — which is the mechanical form of "type, code and temperatures are derived
  from the chosen name, never supplied";
* every refusal the argument bar holds: an id that is not a whole number ≥ 0, a name that is not a
  catalog key (with the catalog's names handed over whole, in a field, so no list is ever cut off
  mid-name), a colour that is not ``#RRGGBB`` and a brand that is not a 1-32 character label — each
  one saying nothing was sent, each one fitting the direct tools' reply cap;
* the two normalisations the vendor client performs and this module must reproduce (colour upper-case,
  brand stripped) and the one place the module deliberately does **not** check: a tray number above 3
  is not refused here, because whether a (canvas_id, tray_id) pair exists is the printer's roster's
  answer, not an upper bound invented in a module that cannot see it;
* the roster read itself (``tray_now``): the tray's seven fields in the **read** side's own spellings
  (``min_nozzle_temp``, no ``filament_code``), reported exactly as they arrived — bare colour with no
  hash, an empty tray's empty strings and 0/0 and all — or None when the printer does not list that
  tray, including when its CANVAS unit reports itself disconnected;
* the two gates both phases stand on, and the answers that refuse them with **no 2003 on the wire
  at all**: a machine that is not idle for a job — printing, paused, or a 1002 that will not answer
  or cannot be read, because an unreadable state is never an idle one (the idle wait is this
  module's own bar, said in the IDLE_GATE note beside the vendor dialog's freedom, and the preview
  carries the reading as ``machine_now``) — and, on an idle machine, a roster that will not answer
  or cannot be read as one, a pair the printer does not list, or a unit that says it is not
  connected — this is where a tray number above 3 lands, because the printer is the one who says
  whether it has that tray;
* the three wire sequences and nothing else: ``[1002, 2005]`` for a preview, ``[1002, 2005, 2003,
  2005]`` for a confirmed command the printer accepted (or never answered), ``[1002, 2005, 2003]``
  for an on-record refusal and for an answer with no readable meaning; never a resend, never a read
  after a refusal, never a read a dead session could not carry;
* accepted / refused / ambiguous in ``cc2_control``'s words, the two readings (``tray_now`` before,
  ``observed`` after) kept apart from each other and from the command, the contradiction note said
  only when the follow-up look really disagrees and worded so it may not be read as an offer to send
  again ("the read may lag"), every note inside the direct tools' reply cap, every reply saying
  ``verified_on_this_printer: false``, and no address, serial or token riding on anything."""
from __future__ import annotations

import ast
import copy
import dataclasses
import inspect
import json
import pathlib
import re
from collections import deque

import pytest

from orcaslicer_mcp.printer import cc2_control as job
from orcaslicer_mcp.printer import cc2_state as st
from orcaslicer_mcp.printer import cc2_tray as tray
from orcaslicer_mcp.printer.cc2_transport import Cc2TimeoutError, PrinterError

# The vendor catalog this module's CATALOG is transcribed from. It lives in the change folder, not in
# the package, so an installed checkout may legitimately not ship it: the test that rebuilds the table
# from it skips when it is absent, and every other test here works from the embedded table alone.
CATALOG_FILE = (pathlib.Path(__file__).resolve().parents[1]
                / "openspec" / "changes" / "cc2-tray-filament-info" / "filament-catalog.txt")

# A call in the shape the tool will hand it: tray A1 on CANVAS 0, PLA, the vendor palette's orange.
GOOD = {"tray_id": 1, "filament_name": "PLA", "colour": "#f88d36", "brand": "Generic", "canvas_id": 0}

# The nine fields the vendor dialog writes, in the dialog's own order (see protocol-evidence.md).
WIRE_FIELDS = ["canvas_id", "tray_id", "brand", "filament_type", "filament_name", "filament_code",
               "filament_color", "filament_min_temp", "filament_max_temp"]


def request(**over) -> dict:
    """``parse_request`` with the documented-good arguments, one of them replaced by the test."""
    args = dict(GOOD)
    args.update(over)
    return tray.parse_request(**args)


# --------------------------------------------------------------------------- the command's own numbers


def test_the_command_is_the_one_named_beside_the_auto_refill_switch():
    # the enum that names SetAutoFillFilament=2004 and GetCanvasInfo=2005 names SetFilamentInfo=2003
    assert tray.METHOD_SET_FILAMENT_INFO == 2003
    assert tray.ACTION == "set_tray_filament"
    # the client passes no timeout override for 2003 (so its 5 s default applies) and the HA
    # integration waits 10 s for every command: the longer window is the one that is waited
    assert tray.SET_WAIT_S == 10.0


def test_every_reply_will_say_this_command_has_never_met_a_cc2():
    assert "2003" in tray.UNVERIFIED
    assert "never" in tray.UNVERIFIED.lower()
    # the honesty note is a field, not a docstring, and a caveat cut in half is the half that gets
    # trusted, so it is written inside the reply cap rather than trimmed on its way out
    assert len(tray.UNVERIFIED) <= job.NOTE_CAP


# --------------------------------------------------------------------------- the vendor catalog


def test_the_catalog_holds_the_vendor_table_exactly():
    assert len(tray.CATALOG) == 50
    assert len({row.code for row in tray.CATALOG.values()}) == 50
    assert tray.catalog_names() == list(tray.CATALOG)          # the dialog's order, not sorted
    assert tray.catalog_names()[0] == "PLA" and tray.catalog_names()[-1] == "PPS-CF"
    # the dialog's brand filter: every row answers to "Generic", and only the vendor's own to "ELEGOO"
    assert all(row.generic for row in tray.CATALOG.values())
    assert all(isinstance(row.elegoo, bool) for row in tray.CATALOG.values())
    assert tray.CATALOG["PLA"].elegoo and not tray.CATALOG["PPS-CF"].elegoo


@pytest.mark.skipif(not CATALOG_FILE.exists(), reason="the vendor catalog note ships with the source")
def test_the_embedded_table_is_the_vendor_file_row_for_row():
    """The one test that proves the copy: rebuild the table from ``filament-catalog.txt`` and hold it
    against the embedded one. Its columns are (key, type, name, code, min, max, elegoo, generic), its
    ``!0``/``!1`` are the bundle's minified ``true``/``false``, and its ``name`` column is identical to
    its ``key`` column — all three of which are checked here rather than asserted anywhere."""
    rows = [ast.literal_eval(line) for line in CATALOG_FILE.read_text().splitlines()
            if line.startswith("(")]
    assert len(rows) == len(tray.CATALOG) == 50
    rebuilt = {}
    for key, material, name, code, low, high, elegoo, generic in rows:
        assert key == name, f"{key!r} is stored under a second name {name!r}"
        rebuilt[key] = tray.Filament(type=material, code=code, min=int(low), max=int(high),
                                     elegoo=elegoo == "!0", generic=generic == "!0")
    assert rebuilt == tray.CATALOG


@pytest.mark.parametrize("name,material,code,low,high", [
    ("PLA", "PLA", "0x0000", 190, 230),
    ("PETG", "PETG", "0x0100", 230, 260),
    ("PPS-CF", "PPS", "0x0E01", 340, 360),
], ids=["PLA", "PETG", "PPS-CF"])
def test_the_three_rows_a_caller_reaches_for_first(name, material, code, low, high):
    row = tray.CATALOG[name]
    assert (row.type, row.code, row.min, row.max) == (material, code, low, high)
    # the code stays the string the bundle writes: nothing on this wire is a decimal filament id
    assert isinstance(row.code, str) and row.code.startswith("0x")


def test_a_catalog_row_cannot_be_rettyped_by_the_caller_that_reads_it():
    # one caller re-typing a shared row would silently change every caller's frame after it
    with pytest.raises(dataclasses.FrozenInstanceError):
        tray.CATALOG["PLA"].min = 999       # type: ignore[misc]


# --------------------------------------------------------------------------- the frame itself


def test_the_frame_is_the_nine_fields_the_dialog_writes_in_its_own_order():
    sent = request()
    assert list(sent) == WIRE_FIELDS
    assert sent == {"canvas_id": 0, "tray_id": 1, "brand": "Generic", "filament_type": "PLA",
                    "filament_name": "PLA", "filament_code": "0x0000", "filament_color": "#F88D36",
                    "filament_min_temp": 190, "filament_max_temp": 230}
    # byte-for-byte, key order included: the same nine keys the vendor client writes, and this
    # project's own key order is the dialog's, not an artefact of how this dict was built
    assert json.dumps(sent) == ('{"canvas_id": 0, "tray_id": 1, "brand": "Generic", '
                                '"filament_type": "PLA", "filament_name": "PLA", '
                                '"filament_code": "0x0000", "filament_color": "#F88D36", '
                                '"filament_min_temp": 190, "filament_max_temp": 230}')


def test_the_write_spells_temps_the_way_the_write_does_and_never_the_read():
    # the 2005 read answers min_nozzle_temp/max_nozzle_temp and reports no code; the 2003 write says
    # filament_min_temp/filament_max_temp and carries filament_code. Both spellings are right, on
    # opposite halves of the wire, and mixing them is how a tray ends up with no temperatures.
    sent = request(filament_name="PETG")
    assert {"filament_min_temp", "filament_max_temp", "filament_code"} <= set(sent)
    assert not [k for k in sent if "nozzle" in k]
    assert sent["filament_min_temp"] == 230 and sent["filament_max_temp"] == 260


def test_no_caller_can_supply_a_type_a_code_or_a_temperature():
    # the dialog asks the human for four things and derives the rest; the signature is the mechanical
    # form of that rule, so a future argument that smuggles a temperature in fails here first
    assert list(inspect.signature(tray.parse_request).parameters) == [
        "tray_id", "filament_name", "colour", "brand", "canvas_id"]


@pytest.mark.parametrize("name", ["PLA+", "PLA Silk", "RAPID TPU 95A", "PC-FR", "PPS-CF"])
def test_every_derivation_comes_from_the_chosen_name(name):
    row = tray.CATALOG[name]
    sent = request(filament_name=name)
    assert sent["filament_name"] == name
    assert (sent["filament_type"], sent["filament_code"], sent["filament_min_temp"],
            sent["filament_max_temp"]) == (row.type, row.code, row.min, row.max)


# --------------------------------------------------------------------------- the ids


@pytest.mark.parametrize("value", [0, 1, 3], ids=["A0", "A1", "A3"])
def test_the_tray_id_is_the_printers_own_numbering(value):
    assert request(tray_id=value)["tray_id"] == value


@pytest.mark.parametrize("value", [4, 9, 4096], ids=["four", "nine", "large"])
def test_a_tray_number_above_three_is_not_this_module_s_call(value):
    # A0-A3 is the CANVAS the printer in question has; whether a (canvas_id, tray_id) pair exists is
    # the roster read's answer, and refusing 4 here would be a ceiling invented by a module that
    # cannot see the printer's trays (the real gate: read_preview / set_tray_filament)
    assert request(tray_id=value)["tray_id"] == value


@pytest.mark.parametrize("field", ["tray_id", "canvas_id"])
@pytest.mark.parametrize("value", [-1, -100, "1", "one", 1.0, True, False, None, [1]],
                         ids=["minus-one", "minus-large", "text-one", "text", "float", "true-bool",
                              "false-bool", "missing", "list"])
def test_an_id_that_is_not_a_whole_number_from_zero_up_refuses(field, value):
    with pytest.raises(PrinterError) as caught:
        request(**{field: value})
    assert caught.value.code == "protocol_error"
    assert caught.value.details["reason"] == field
    assert field in caught.value.message and "Nothing was sent." in caught.value.message
    assert caught.value.hint and len(caught.value.hint) <= job.NOTE_CAP


# --------------------------------------------------------------------------- the material name


@pytest.mark.parametrize("bad", ["pla", "Petg", "PLA ", " pla", "PLA++", "PETG / ASA", "", "red",
                                 None, 3, ["PLA"]],
                         ids=["lower-case", "title-case", "trailing-space", "leading-space",
                              "near-miss", "two-names", "empty", "colour-word", "missing", "number",
                              "list"])
def test_a_name_the_vendor_lookup_does_not_hold_refuses_with_the_whole_list_beside_it(bad):
    # the vendor lookup answers an unknown name with empty strings and a 0-0 range: on this wire that
    # is a tray labelled with a material that has no type, no code and no temperature, so the fork
    # refuses instead of passing it through — and it hands over the names in a field, because 50 of
    # them do not fit a capped message and a list cut mid-name is worse than no list
    with pytest.raises(PrinterError) as caught:
        request(filament_name=bad)
    err = caught.value
    assert err.code == "protocol_error" and err.details["reason"] == "filament_name"
    assert "Nothing was sent." in err.message and "catalog" in err.message.lower()
    assert err.details["catalog_names"] == tray.catalog_names() == list(tray.CATALOG)
    assert "PLA" in err.hint and "catalog_names()" in err.hint


def test_the_material_list_survives_the_reply_cap_because_it_is_its_own_field():
    # _cc2_out caps strings at 255 characters and cuts a long tail with an ellipsis: the names go out
    # as a list, where the cap is a length in items and no name is ever truncated
    assert len(", ".join(tray.catalog_names())) > job.NOTE_CAP
    assert all(len(name) <= job.NOTE_CAP for name in tray.catalog_names())
    assert re.fullmatch(r"_?[a-z][a-z0-9_]+", "catalog_names")     # the server's own key shape


# --------------------------------------------------------------------------- colour and brand


@pytest.mark.parametrize("colour,wanted", [
    ("#F88D36", "#F88D36"), ("#f88d36", "#F88D36"), ("#0be2a0", "#0BE2A0"), ("#000000", "#000000"),
    ("#ABC123", "#ABC123"), ("#abc123", "#ABC123"), ("  #d4b1dd  ", "#D4B1DD"),
], ids=["upper", "lower", "mixed", "black", "upper-2", "lower-2", "padded"])
def test_the_colour_is_read_any_case_and_sent_the_way_the_palette_writes_it(colour, wanted):
    assert request(colour=colour)["filament_color"] == wanted


@pytest.mark.parametrize("bad", ["red", "black", "FFFFFF", "#FFF", "#FFFF", "#FFFFFFF", "#F88D3",
                                 "#GGGGGG", "# F88D36", "", "  ", "#0x0000", None, 0xFFFFFF],
                         ids=["name", "name-2", "no-hash", "three", "four", "seven", "five",
                              "not-hex", "space-inside", "empty", "spaces", "hex-prefix", "missing",
                              "number"])
def test_a_colour_that_is_not_six_hex_digits_after_a_hash_refuses(bad):
    with pytest.raises(PrinterError) as caught:
        request(colour=bad)
    err = caught.value
    assert err.code == "protocol_error" and err.details["reason"] == "filament_color"
    assert "#RRGGBB" in err.message and "Nothing was sent." in err.message
    # no expansion, no nearest match: guessing a colour is how a tray's label stops meaning anything
    assert "F88D36" in err.hint and len(err.message) <= job.NOTE_CAP


@pytest.mark.parametrize("brand,wanted", [
    ("Generic", "Generic"), ("ELEGOO", "ELEGOO"), ("  ELEGOO  ", "ELEGOO"),
    ("Bambu Lab", "Bambu Lab"), ("eSun", "eSun"), ("Polymaker", "Polymaker"),
    ("M" * 32, "M" * 32),
], ids=["generic", "elegoo", "padded", "free-text-space", "free-text-case", "free-text", "32-chars"])
def test_the_brand_is_stripped_and_free_text_is_allowed_because_the_dialog_allows_it(brand, wanted):
    # the dialog's brand field filters the catalog, it does not validate what was typed; the bar here
    # is the one that keeps a reply-shaped string out of a tray label, and the firmware's taste in
    # brands nobody has seen is exactly what verified_on_this_printer: false is for
    assert request(brand=brand)["brand"] == wanted


@pytest.mark.parametrize("bad", ["", "   ", "\t\n", "M" * 33, "M" * 200, None, 0, 1, ["Generic"],
                                 {"brand": "Generic"}],
                         ids=["empty", "spaces", "newlines", "33-chars", "long", "missing", "zero",
                              "one", "list", "object"])
def test_a_brand_that_is_not_a_usable_label_refuses_and_names_the_two_the_dialog_offers(bad):
    with pytest.raises(PrinterError) as caught:
        request(brand=bad)
    err = caught.value
    assert err.code == "protocol_error" and err.details["reason"] == "brand"
    assert "32" in err.message and "Nothing was sent." in err.message
    assert "ELEGOO" in err.hint and "Generic" in err.hint
    assert len(err.hint) <= job.NOTE_CAP


# --------------------------------------------------------------------------- the whole bar at once


def test_every_refusal_says_the_same_words_and_no_refusal_can_have_reached_a_printer():
    # parse_request takes no transport, no address, no token and no environment variable, so the only
    # thing a bad argument can produce is this refusal — and every one of them says so in the same
    # sentence, in both fields, inside the reply cap the sanitiser would otherwise cut
    refusals = [dict(filament_name="pla"), dict(colour="red"), dict(brand=""), dict(tray_id=-1),
                dict(canvas_id="0"), dict(tray_id=True), dict(colour="#FFF"), dict(brand="b" * 33),
                dict(filament_name="P" * 200), dict(colour="#" + "F" * 60), dict(tray_id="9" * 60),
                dict(brand="b" * 4000)]
    for over in refusals:
        with pytest.raises(PrinterError) as caught:
            request(**over)
        err = caught.value
        assert "Nothing was sent." in err.message
        assert len(err.message) <= job.NOTE_CAP and len(err.hint or "") <= job.NOTE_CAP
        assert not any(token in err.message for token in ("10.88", "192.0.2", "token"))


# =========================================================================== the roster, the gate,
# =========================================================================== the preview and the one command

HOST = "192.0.2.10"                  # documented TEST-NET, like the whole lane; never a live address
SN = "CC2TESTSN0001"
FAKE_TOKEN = "SIMULATED-NOT-A-CODE"  # a stand-in credential, proven never to ride any reply
JOB = "benchy-petg-2026-11-04.gcode"  # a stand-in filename, like the other CC2 control tests


def _1002_frame(*, status=1, sub=0, state="", filename="") -> dict:
    """A 1002 reply in the observed frames of ``tests/test_cc2_motion.py``: machine 1/0 with the
    firmware's idle word "" is the idle machine every sequence below stands on; 2/2075 with state
    "printing" and a filename is its observed live shape; 2/2502 with state "paused" is the paused
    job ``cc2_state`` spells a job, not a clear bed. A frame with no state block at all is what
    "an unreadable 1002" means for this adapter: from_result refuses it, and a state it cannot read
    is never idle."""
    return {"error_code": 0,
            "machine_status": {"exception_status": [], "progress": 0 if status == 1 else 21,
                               "status": status, "sub_status": sub, "sub_status_reason_code": 0},
            "print_status": {"bed_mesh_detect": True, "current_layer": 0 if status == 1 else 51,
                             "enable": True, "filament_detect": False, "filename": filename,
                             "print_duration": 0, "remaining_time_sec": 0, "state": state,
                             "total_duration": 0, "uuid": ""},
            "extruder": {"target": 0, "temperature": 21},
            "heater_bed": {"target": 0, "temperature": 16}}


IDLE_1002 = _1002_frame()
PRINTING_1002 = _1002_frame(status=2, sub=2075, state="printing", filename=JOB)
PAUSED_1002 = _1002_frame(status=2, sub=2502, state="paused", filename=JOB)


def _tray(tray_id, status, *, ftype="PLA", brand="Generic", name=None, colour="F88D36",
          mn=190, mx=230) -> dict:
    """One tray exactly as a 2005 ``tray_list`` answers it: the read side's spellings
    (``min_nozzle_temp``, no ``filament_code``) and the printer's own habit of writing the colour
    bare, with no hash. ``status`` is the firmware's number (0 empty, 1 present, 2 active)."""
    return {"tray_id": tray_id, "status": status, "filament_type": ftype, "brand": brand,
            "filament_name": ftype if name is None else name, "filament_color": colour,
            "min_nozzle_temp": mn, "max_nozzle_temp": mx}


# The observed CC2's roster, the four trays the printer names on CANVAS 0 (the shape
# ``tests/test_server_cc2.py``'s CANVAS uses). Tray A1 is the one every write below targets, and it
# already reads Generic PLA #F88D36 190-230, which is what ``GOOD`` asks for, so the accepted path's
# follow-up look agrees unless a test aims it at something else. The second unit is **disconnected**:
# it is here to prove the gate reads ``connected`` and not merely "the printer mentioned that id".
ROSTER = {"error_code": 0, "canvas_info": {
    "active_canvas_id": 0, "active_tray_id": 2, "auto_refill": False,
    "canvas_list": [
        {"canvas_id": 0, "connected": 1, "tray_list": [
            _tray(0, 0, ftype="", brand="", name="", colour="000000", mn=0, mx=0),   # empty
            _tray(1, 1),                                                             # A1: PLA
            _tray(2, 2, ftype="PETG", colour="000000", mn=230, mx=260),              # A2 active
            _tray(3, 1, ftype="PETG", brand="ELEGOO", colour="000000", mn=230, mx=260),
        ]},
        {"canvas_id": 1, "connected": 0, "tray_list": [
            _tray(1, 1, ftype="ABS", colour="2850DF", mn=240, mx=280)]},
    ]}}


def frame_2005(**over) -> dict:
    """The roster frame, with ``canvas_info`` fields replaced by whatever a test needs (``over``).
    ``canvas_info=None`` removes the block entirely — the reply that is an envelope and nothing else,
    which is a roster this adapter cannot read."""
    frame = copy.deepcopy(ROSTER)
    info = frame["canvas_info"]
    for key, value in over.items():
        if key == "canvas_info":
            if value is None:
                frame.pop("canvas_info")
            else:
                frame["canvas_info"] = value
        elif value is None:
            info.pop(key)
        else:
            info[key] = value
    return frame


class FakeTransport:
    """Copied from ``tests/test_cc2_canvas.py``'s ``FakeCanvasTransport``: stands in for the
    registered session as ``Cc2Transport`` behaves, recording every request with the timeout it was
    given and answering from a method -> script map (a deque shifts, last value sticks, exceptions
    raise as given, an unscripted method answers {}). The module under test is the thing that must
    not ask twice, so nothing here ever refuses twice on its own."""

    def __init__(self, replies=None, *, host=HOST, sn=SN):
        self.host, self.sn = host, sn
        self.replies = dict(replies or {})
        self.requests: list[tuple[int, dict, float | None]] = []

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

    def methods(self) -> list[int]:
        return [m for m, _, _ in self.requests]

    def calls(self, method: int) -> list[tuple[int, dict, float | None]]:
        return [r for r in self.requests if r[0] == method]


def timeout() -> PrinterError:
    return Cc2TimeoutError("protocol_error", "simulated: the printer never answered",
                           reason="timeout", ambiguous=True)


def dead_session() -> PrinterError:
    return PrinterError("not_reachable", "simulated: the session ended")


def scripted(*, canvas=None, command=None, status=None) -> FakeTransport:
    """1002 answers ``status`` (default the observed idle machine every sequence stands on), 2005
    answers ``canvas`` (default the observed four-tray roster) and 2003 answers ``command`` (default
    the explicit success every source agrees means "the printer did not refuse")."""
    return FakeTransport({st.METHOD_STATUS: IDLE_1002 if status is None else status,
                          st.METHOD_CANVAS: frame_2005() if canvas is None else canvas,
                          tray.METHOD_SET_FILAMENT_INFO: {"error_code": 0} if command is None
                          else command})


def call(**over) -> dict:
    """A confirmed call's params, built the only legal way: through ``parse_request``."""
    return tray.parse_request(**GOOD | over)


# --------------------------------------------------------------------------- the roster reading


def test_the_roster_reading_keeps_the_read_side_s_own_spelling_and_what_arrived():
    now = tray.tray_now(frame_2005(), 0, 1)
    assert set(now) == {"tray_id", *tray.TRAY_FIELDS}
    # the printer's own words and numbers, unimproved: the colour stays bare because the printer
    # wrote it bare, and the temperatures keep the READ spelling (filament_min_temp belongs to the
    # write and appears nowhere a read is reported)
    assert now == {"tray_id": 1, "status": 1, "filament_type": "PLA", "filament_name": "PLA",
                   "brand": "Generic", "filament_color": "F88D36",
                   "min_nozzle_temp": 190, "max_nozzle_temp": 230}
    assert not [k for k in now if k.startswith("filament_m") or k == "filament_code"]


def test_an_empty_tray_is_reported_as_the_printer_words_it_rather_than_as_a_guess():
    # A0 says empty in its own way: no material word, no brand word, and 0/0 for a rating. None of
    # that is turned into None, "PLA", or a plausible range: the preview shows the printer's sentence
    assert tray.tray_now(frame_2005(), 0, 0) == {
        "tray_id": 0, "status": 0, "filament_type": "", "filament_name": "", "brand": "",
        "filament_color": "000000", "min_nozzle_temp": 0, "max_nozzle_temp": 0}


@pytest.mark.parametrize("frame, canvas_id, tray_id", [
    (frame_2005(), 0, 4),                      # a tray number this printer never listed (A4, A5...)
    (frame_2005(), 7, 1),                      # a CANVAS unit this printer does not have
    (frame_2005(), 1, 1),                      # the unit exists and lists tray A1 but says disconnected
    (frame_2005(canvas_list=[]), 0, 1),        # no units at all
    (frame_2005(canvas_list="nope"), 0, 1),    # a roster-shaped field that is no roster
    (frame_2005(canvas_list=[{"canvas_id": 0, "connected": 1}]), 0, 1),   # a unit with no tray list
    ({"error_code": 0}, 0, 1),                 # an envelope with no roster inside
    ({"error_code": 1003}, 0, 1),              # a refused read
    ({}, 0, 1),                                # nothing to read at all
], ids=["tray-not-listed", "canvas-not-listed", "unit-disconnected", "no-units", "not-a-list",
        "no-tray-list", "envelope-only", "refused-read", "nothing"])
def test_a_tray_the_printer_does_not_name_is_no_reading(frame, canvas_id, tray_id):
    assert tray.tray_now(frame, canvas_id, tray_id) is None


def test_the_bare_inner_roster_object_reads_like_the_enveloped_one():
    # cc2_state's reader and cc2_canvas both accept the block without its envelope; so must the gate,
    # or a printer that answers correctly would be refused for the shape of its reply
    assert tray.tray_now(frame_2005()["canvas_info"], 0, 1)["filament_name"] == "PLA"


# --------------------------------------------------------------------------- the gate (no 2003)

GATE_REFUSALS = [
    (frame_2005(), 0, 5, "tray_not_listed", "A5 is not a tray this printer named"),
    (frame_2005(), 9, 1, "tray_not_listed", "no CANVAS unit 9"),
    (frame_2005(), 1, 1, "canvas_disconnected", "the unit says it is not connected"),
    (timeout(), 0, 1, "canvas_unreadable", "the roster would not answer"),
    ({"error_code": 0}, 0, 1, "canvas_unreadable", "an envelope with no roster"),
    (frame_2005(canvas_info=None), 0, 1, "canvas_unreadable", "no canvas_info block"),
    (frame_2005(canvas_list="nope"), 0, 1, "canvas_unreadable", "a roster-shaped field that is no list"),
    (frame_2005(canvas_list=[{"canvas_id": 0, "connected": 1}]), 0, 1, "canvas_unreadable",
     "a unit with no tray list to read against"),
]


@pytest.mark.parametrize("canvas, canvas_id, want_tray, reason, why", GATE_REFUSALS,
                         ids=[f"{row[3]}-{row[4]}".replace(" ", "-") for row in GATE_REFUSALS])
async def test_a_tray_the_printer_cannot_name_refuses_both_phases_with_no_command_on_the_wire(
        canvas, canvas_id, want_tray, reason, why):
    # the gate is not a courtesy: an unreadable roster, an unlisted pair or a disconnected unit each
    # end the call before a 2003 exists, in both phases, because a tray write without a named tray is
    # a write to nowhere (this is the difference from the auto-refill switch, which previews through a
    # silent CANVAS read because a flag needs no CANVAS behind it)
    args = call(tray_id=want_tray, canvas_id=canvas_id)
    tr = scripted(canvas=canvas)
    with pytest.raises(PrinterError) as caught:
        await tray.read_preview(tr, args)
    err = caught.value
    assert err.code == "protocol_error" and err.details["reason"] == reason
    assert "Nothing was sent." in err.message and "2005" in err.message
    assert len(err.message) <= job.NOTE_CAP and len(err.hint or "") <= job.NOTE_CAP
    assert tr.methods() == [st.METHOD_STATUS, st.METHOD_CANVAS] and tr.calls(2003) == []

    tr2 = scripted(canvas=canvas)
    with pytest.raises(PrinterError) as caught2:
        await tray.set_tray_filament(tr2, args)
    assert caught2.value.details["reason"] == reason
    assert tr2.methods() == [st.METHOD_STATUS, st.METHOD_CANVAS] and tr2.calls(2003) == []


def test_the_gate_is_where_a_tray_number_above_three_actually_lands():
    # 1.1 pins that parse_request invents no ceiling; the printer's roster is the ceiling, and A4 of
    # this four-tray CANVAS refuses exactly as an unknown word would, with no command sent
    assert call(tray_id=4)["tray_id"] == 4
    assert tray.tray_now(frame_2005(), 0, 4) is None


# --------------------------------------------------------------------------- the idle gate (1002)


@pytest.mark.parametrize("frame,word", [(PRINTING_1002, "printing"), (PAUSED_1002, "paused")],
                         ids=["printing", "paused"])
async def test_a_machine_that_is_not_idle_refuses_both_phases_before_anything_else_is_read(
        frame, word):
    # the wait for an idle machine is this module's own bar (IDLE_GATE says so, beside the vendor
    # dialog's freedom): a job — printing or paused — owns the machine, and the tray edit refuses
    # with the printer's own state named. A refusal that sends nothing also asks for nothing after
    # it: the roster is not even read, and no 2003 exists anywhere on the wire.
    args = call()
    tr = scripted(status=frame)
    with pytest.raises(PrinterError) as caught:
        await tray.read_preview(tr, args)
    err = caught.value
    assert err.code == "protocol_error" and err.details["reason"] == "machine_not_idle"
    assert word in err.message and "Nothing was sent." in err.message
    assert len(err.message) <= job.NOTE_CAP and len(err.hint or "") <= job.NOTE_CAP
    assert err.details["printer_says"] and word in err.details["printer_says"]
    assert tr.methods() == [st.METHOD_STATUS] and tr.calls(2003) == []

    tr2 = scripted(status=frame)
    with pytest.raises(PrinterError) as caught2:
        await tray.set_tray_filament(tr2, args)
    assert caught2.value.details["reason"] == "machine_not_idle"
    assert tr2.methods() == [st.METHOD_STATUS] and tr2.calls(2003) == []


@pytest.mark.parametrize("status", [{}, {"error_code": 0}, timeout(),
                                    PrinterError("not_reachable", "simulated: no answer to 1002")],
                         ids=["empty", "no-state-block", "timeout", "session-dead"])
async def test_a_machine_state_that_cannot_be_read_refuses_both_phases_and_is_never_idle(status):
    # an unreadable state is the same refusal shape: the printer's own words about why ride clipped
    # inside the cap, and a state this adapter cannot read is never read as idle — the write does
    # not ask a machine it cannot hear to promise anything
    tr = scripted(status=status)
    with pytest.raises(PrinterError) as caught:
        await tray.read_preview(tr, call())
    err = caught.value
    assert err.code == "protocol_error" and err.details["reason"] == "machine_unreadable"
    assert "Nothing was sent." in err.message
    assert len(err.message) <= job.NOTE_CAP and len(err.hint or "") <= job.NOTE_CAP
    assert tr.methods() == [st.METHOD_STATUS] and tr.calls(2003) == []

    tr2 = scripted(status=status)
    with pytest.raises(PrinterError) as caught2:
        await tray.set_tray_filament(tr2, call())
    assert caught2.value.details["reason"] == "machine_unreadable"
    assert tr2.methods() == [st.METHOD_STATUS] and tr2.calls(2003) == []


@pytest.mark.parametrize("bad", [{"canvas_id": 0, "tray_id": 1},
                                 {**call(), "min_nozzle_temp": 200},
                                 {**call(), "filament_color": "red"},
                                 {k: v for k, v in call().items() if k != "filament_code"},
                                 {**call(), "auto_refill": True}, "true", None],
                         ids=["two-fields", "read-spelling-rides-along",
                              "an-unchecked-value-hidden-in-a-complete-frame",
                              "a-derived-field-missing", "another-command-s-field", "text", "nothing"])
async def test_a_hand_rolled_frame_refuses_before_the_wire_and_can_never_override_the_derivation(
        bad):
    # parse_request is the only door to the nine fields; a caller that skipped it and hand-rolled a
    # frame — with the read side's temp spelling, a tenth field, or a temperature of its own — gets a
    # refusal and an empty wire rather than a printer told a number nobody derived
    tr = scripted()
    with pytest.raises(PrinterError) as caught:
        await tray.read_preview(tr, bad)
    assert caught.value.code == "protocol_error"
    assert tr.requests == []
    tr2 = scripted()
    with pytest.raises(PrinterError):
        await tray.set_tray_filament(tr2, bad)
    assert tr2.requests == []


# --------------------------------------------------------------------------- the preview


async def test_the_preview_reads_the_gates_once_and_never_sends_the_command():
    tr = FakeTransport({st.METHOD_STATUS: IDLE_1002,                    # 2003 unscripted on purpose:
                        st.METHOD_CANVAS: frame_2005()})                # reaching one would still be
    out = await tray.read_preview(tr, call())                # a recorded fact
    assert tr.methods() == [st.METHOD_STATUS, st.METHOD_CANVAS], \
        "the preview reads the state and the roster and sends nothing else"
    assert tr.calls(st.METHOD_CANVAS)[0][1] == {}
    assert out["confirm_required"] is True and out["action"] == tray.ACTION
    p = out["preview"]
    # what a confirmed call would send: the nine fields, four of them the caller's, five the catalog's
    assert p["will_send"] == {"method": 2003, "params": call()}
    assert p["waits_up_to_s"] == 10.0
    # what the printer says about that tray now, in the preview's own words, values unimproved
    assert p["tray_now"] == {"tray_id": 1, "material": "PLA", "name": "PLA", "brand": "Generic",
                            "colour": "F88D36", "min_nozzle_temp": 190, "max_nozzle_temp": 230,
                            "status": 1}
    # the machine state the idle gate stood on: the printer's own number and cc2_state's word
    assert p["machine_now"] == {"machine_status": 1, "state": "idle"}
    assert json.dumps(p["will_send"]["params"]) == json.dumps(call())   # byte-for-byte, order kept
    assert "Nothing was sent to the printer" in out["how"] and "confirm=true" in out["how"]
    assert out["verified_on_this_printer"] is False and "2003" in out["unverified"]
    assert any("waited for an idle machine" in n for n in out["notes"]), \
        "the wait is said, and the vendor dialog's freedom beside it"
    for n in out["notes"]:
        assert len(n) <= job.NOTE_CAP
    json.dumps(out)


async def test_a_preview_of_a_tray_that_holds_something_else_says_so_in_the_printers_words():
    # the whole point of the preview: the human sees the tray's current label beside what would
    # replace it, and the printer's placeholder colour for an empty tray is shown as the placeholder
    out = await tray.read_preview(scripted(), call(tray_id=0, filament_name="PETG", colour="#0BE2A0"))
    now = out["preview"]["tray_now"]
    assert now["material"] == "" and now["name"] == "" and now["status"] == 0
    assert now["colour"] == "000000" and (now["min_nozzle_temp"], now["max_nozzle_temp"]) == (0, 0)
    assert out["preview"]["will_send"]["params"]["filament_name"] == "PETG"


# --------------------------------------------------------------------------- the one command


async def test_the_confirmed_call_is_the_gate_read_then_one_2003_then_one_quiet_read():
    tr = scripted()
    result = await tray.set_tray_filament(tr, call())
    assert tr.methods() == [st.METHOD_STATUS, st.METHOD_CANVAS, 2003, st.METHOD_CANVAS]
    sent = tr.calls(2003)
    assert len(sent) == 1 and sent[0][1] == call() and sent[0][2] == 10.0
    # the two reads are the gate's and the courtesy's: both read-only 2005 with no params, neither
    # asked for the command's longer window
    assert [c[1] for c in tr.calls(st.METHOD_CANVAS)] == [{}, {}]
    assert [c[2] for c in tr.calls(st.METHOD_CANVAS)] == [None, None]
    assert result.status is tray.ControlStatus.ACCEPTED and result.accepted
    assert result.method == 2003 and result.sent == call() and result.waited_s == 10.0
    joined = " ".join(result.notes)
    assert "did not refuse" in joined and "may lag" in joined
    assert "waited for an idle machine" in joined
    assert not any("disagreement" in n for n in result.notes)     # the roster agrees: nothing to flag


async def test_the_two_readings_are_kept_apart_from_each_other_and_from_the_command():
    tr = scripted()
    d = (await tray.set_tray_filament(tr, call())).as_dict()
    assert set(d) == {"action", "method", "sent", "status", "accepted", "error_code", "waited_s",
                      "tray_now", "observed", "verified_on_this_printer", "unverified", "notes"}
    assert d["action"] == tray.ACTION and d["method"] == 2003 and d["status"] == "accepted"
    assert d["sent"]["filament_min_temp"] == 190          # the write's spelling, derived
    assert d["tray_now"]["min_nozzle_temp"] == 190        # the read's spelling, reported
    assert d["observed"] == d["tray_now"]                 # same tray, two moments, both kept
    json.dumps(d)


@pytest.mark.parametrize("answer", [{"error_code": 1003}, {"error_code": 1010}],
                         ids=["firmware-refusal", "no-active-print"])
async def test_a_refusal_keeps_its_code_and_earns_no_further_read(answer):
    tr = scripted(command=answer)
    result = await tray.set_tray_filament(tr, call())
    assert result.status is tray.ControlStatus.REFUSED and not result.accepted
    assert result.error_code == answer["error_code"]
    assert result.tray_now is not None and result.observed is None
    assert str(answer["error_code"]) in " ".join(result.notes)
    assert tr.methods() == [st.METHOD_STATUS, st.METHOD_CANVAS, 2003]   # gates, command, no reads


@pytest.mark.parametrize("answer", [{}, {"error_code": True}, {"error_code": "0"},
                                    {"error_code": 0.0}, {"error_code": None}],
                         ids=["empty", "bool-zero", "string-zero", "float-zero", "null-zero"])
async def test_answers_without_an_explicit_integer_are_ambiguous_never_retried(answer):
    # a reply arrived; what is missing is its meaning, so there is nothing to follow up: no second
    # 2003 and no read, and the caller reads the roster deliberately instead
    tr = scripted(command=answer)
    result = await tray.set_tray_filament(tr, call())
    assert result.status is tray.ControlStatus.AMBIGUOUS and not result.accepted
    assert result.error_code is None and result.observed is None
    assert tr.methods() == [st.METHOD_STATUS, st.METHOD_CANVAS, 2003]
    assert any("neither success nor refusal" in n for n in result.notes)


async def test_a_lost_answer_is_ambiguous_never_a_refusal_never_resent_with_one_look():
    tr = scripted(command=timeout())
    result = await tray.set_tray_filament(tr, call())
    assert result.status is tray.ControlStatus.AMBIGUOUS and not result.accepted
    joined = " ".join(result.notes)
    assert "neither a refusal nor a failure" in joined and "nothing was retried" in joined
    assert len(tr.calls(2003)) == 1, "a lost answer never becomes a resend"
    assert tr.methods() == [st.METHOD_STATUS, st.METHOD_CANVAS, 2003, st.METHOD_CANVAS]   # exactly the one look allowed
    assert result.observed["filament_name"] == "PLA"       # what the roster says rides along, unspun


async def test_a_dead_session_is_ambiguous_and_asks_that_session_for_nothing_further():
    tr = scripted(command=dead_session())
    result = await tray.set_tray_filament(tr, call())
    assert result.status is tray.ControlStatus.AMBIGUOUS and not result.accepted
    assert tr.methods() == [st.METHOD_STATUS, st.METHOD_CANVAS, 2003]  # no roster read a dead
                                                                       # session could carry
    assert any("nothing was resent" in n for n in result.notes)
    assert all(len(n) <= job.NOTE_CAP for n in result.notes)


async def test_a_command_the_roster_contradicts_on_colour_is_said_out_loud_may_lag_and_all():
    # the printer answered error_code 0 and its own roster still says the tray's colour is something
    # else: two facts, both reported, neither overwriting the other, and above all no second command
    tr = scripted(command={"error_code": 0})            # roster still reads F88D36 for tray A1
    result = await tray.set_tray_filament(tr, call(colour="#FFFFFF"))
    assert result.status is tray.ControlStatus.ACCEPTED and result.accepted
    contra = [n for n in result.notes if "disagreement" in n]
    assert len(contra) == 1
    assert "colour=#FFFFFF" in contra[0] and "colour=F88D36" in contra[0]   # both sides, as reported
    assert "tray A1 of canvas 0" in contra[0] and "may lag" in contra[0] and "nothing is resent" in contra[0]
    assert len(tr.calls(2003)) == 1 and tr.methods().count(st.METHOD_CANVAS) == 2


@pytest.mark.parametrize("name,expect_name,expect_type", [
    ("PETG", True, True), ("PLA+", True, False), ("PLA PRO", True, False)],
                         ids=["name-and-type", "name-only", "second-name-only"])
async def test_a_name_or_type_the_roster_does_not_show_is_flagged_beside_the_acceptance(
        name, expect_name, expect_type):
    # PLA PRO keeps the vendor type PLA and only renames the tray, so a name-only disagreement is
    # still a disagreement worth saying out loud, and PETG differs on both words
    tr = scripted()
    result = await tray.set_tray_filament(tr, call(filament_name=name))
    assert result.accepted
    contra = [n for n in result.notes if "disagreement" in n]
    assert len(contra) == 1
    assert ("name=" in contra[0]) is expect_name
    assert ("type=" in contra[0]) is expect_type


async def test_a_placeholder_reading_is_no_contradiction_and_an_unreadable_colour_is_none_either():
    tr = scripted(canvas=frame_2005(canvas_list=[{"canvas_id": 0, "connected": 1, "tray_list": [
        _tray(1, 1, ftype="\u2014", name="\u2014", colour="nonsense")]}]))
    result = await tray.set_tray_filament(tr, call())
    assert result.accepted
    assert result.observed["filament_name"] == "\u2014"      # what arrived, kept
    assert not any("disagreement" in n for n in result.notes)


@pytest.mark.parametrize("canvas", [timeout(), {"error_code": 0},
                                    frame_2005(canvas_list=[{"canvas_id": 0, "connected": 1,
                                                             "tray_list": [_tray(2, 1)]}])],
                         ids=["read-failed", "no-roster", "tray-vanished"])
async def test_the_follow_up_read_that_gives_nothing_stays_an_honest_unknown_and_earns_no_retry(canvas):
    # the command answered 0; the one look afterwards produced no reading for this tray. That is not
    # "the tray is empty", not a contradiction, and not a reason to send anything again
    tr = scripted(canvas=deque([frame_2005(), canvas]))      # gate read ok, follow-up read is `canvas`
    result = await tray.set_tray_filament(tr, call())
    assert result.status is tray.ControlStatus.ACCEPTED and result.accepted
    assert result.observed is None and result.tray_now is not None
    assert any("stays unknown" in n for n in result.notes)
    assert not any("disagreement" in n for n in result.notes)
    assert len(tr.calls(2003)) == 1 and tr.methods().count(st.METHOD_CANVAS) == 2


async def test_the_contradiction_note_survives_the_reply_cap_with_its_tail_intact():
    # a printer free to answer any string in these fields must not be able to cut this note's honesty
    # in half: the values are clipped (the whole ones ride in sent and observed), the sentence is not
    long = "P" * 200
    tr = scripted(canvas=frame_2005(canvas_list=[{"canvas_id": 0, "connected": 1, "tray_list": [
        _tray(1, 1, ftype=long, name=long, colour="Z" * 200)]}]))
    result = await tray.set_tray_filament(tr, call(filament_name="PLA Sparkle"))
    contra = [n for n in result.notes if "disagreement" in n]
    assert len(contra) == 1
    assert len(contra[0]) <= job.NOTE_CAP
    assert contra[0].endswith("nothing is resent."), contra[0]


# --------------------------------------------------------------------------- the shape and the label


async def test_no_command_but_the_roster_read_and_the_one_write_is_ever_sent():
    tr = scripted()
    await tray.read_preview(tr, call())
    await tray.set_tray_filament(tr, call())
    forbidden = {2004, 1020, job.METHOD_PAUSE, job.METHOD_STOP, job.METHOD_RESUME,
                 1028, 1029, 1030, 1031, 1043, 1047, 1055, 1061}
    assert not (set(tr.methods()) & forbidden), tr.methods()
    assert set(tr.methods()) == {st.METHOD_STATUS, st.METHOD_CANVAS, 2003}


def test_the_module_speaks_one_command_and_holds_the_two_spellings_apart():
    doc = " ".join((tray.__doc__ or "").split())
    assert tray.METHOD_SET_FILAMENT_INFO == 2003 and tray.ACTION == "set_tray_filament"
    assert not [m for m in vars(tray) if m.startswith("METHOD_") and m != "METHOD_SET_FILAMENT_INFO"]
    assert set(tray.WIRE_FIELDS) == set(WIRE_FIELDS)          # the write, all nine, no more
    assert "min_nozzle_temp" in tray.TRAY_FIELDS and "min_nozzle_temp" not in tray.WIRE_FIELDS
    assert "filament_min_temp" in tray.WIRE_FIELDS and "filament_min_temp" not in tray.TRAY_FIELDS
    assert "may lag" in doc or "may lag" in tray._ACCEPT_TAIL
    assert "never been offered to a CC2" in doc


async def test_every_outcome_says_the_command_has_never_met_a_cc2():
    for tr in (scripted(), scripted(command={"error_code": 1003}), scripted(command={}),
               scripted(command=timeout()), scripted(command=dead_session())):
        result = await tray.set_tray_filament(tr, call())
        assert result.verified_on_this_printer is False
        assert result.as_dict()["verified_on_this_printer"] is False
        assert "2003" in result.as_dict()["unverified"]
    preview = await tray.read_preview(scripted(), call())
    assert preview["verified_on_this_printer"] is False
    for canvas, canvas_id, gate_tray, *_ in GATE_REFUSALS:
        with pytest.raises(PrinterError):
            await tray.read_preview(scripted(canvas=canvas),
                                    call(canvas_id=canvas_id, tray_id=gate_tray))


async def test_every_note_and_message_of_every_outcome_fits_the_reply_cap():
    # nothing in this module may be the thing that loses its own tail: a note, a refusal's message or
    # a refusal's hint that runs past the cap is a promise cut mid-sentence, and the half that gets
    # cut is the half a model would be trusted to fill in
    seen_any = False
    commands = ({"error_code": 0}, {"error_code": 1003}, {"error_code": 1010}, {}, timeout(),
                dead_session())
    canvases = (frame_2005(),
                frame_2005(canvas_list=[{"canvas_id": 0, "connected": 1, "tray_list":
                                         [_tray(1, 1, ftype="ABS", name="ASA-CF", colour="2850DF",
                                                mn=240, mx=280)]}]),
                frame_2005(canvas_list=[{"canvas_id": 0, "connected": 0, "tray_list": [_tray(1, 1)]}]),
                frame_2005(canvas_list=[]), timeout())
    for command in commands:
        for canvas in canvases:
            tr = scripted(canvas=canvas, command=command)
            for over in ({}, {"filament_name": "PETG", "colour": "#FFFFFF"}, {"tray_id": 0},
                         {"tray_id": 3, "brand": "Bambu Lab"}):
                seen_any = True
                try:
                    strings = (await tray.set_tray_filament(tr, call(**over))).notes
                except PrinterError as e:               # a gate refusal: its own words are capped too
                    strings = (e.message, e.hint or "")
                for n in strings:
                    assert len(n) <= job.NOTE_CAP, (command, canvas, over, n)
    assert seen_any


async def test_no_address_no_serial_no_token_rides_any_reply():
    texts = [json.dumps((await tray.set_tray_filament(scripted(), call())).as_dict()),
             json.dumps((await tray.set_tray_filament(scripted(),
                                                      call(colour="#FFFFFF"))).as_dict()),
             json.dumps((await tray.set_tray_filament(scripted(command=timeout()),
                                                      call())).as_dict()),
             json.dumps((await tray.set_tray_filament(scripted(command=dead_session()),
                                                      call())).as_dict()),
             json.dumps((await tray.set_tray_filament(
                 scripted(canvas=deque([frame_2005(), timeout()])), call())).as_dict()),
             json.dumps(await tray.read_preview(scripted(), call()))]
    for canvas, canvas_id, gate_tray, *_ in GATE_REFUSALS:
        with pytest.raises(PrinterError) as caught:
            await tray.read_preview(scripted(canvas=canvas), call(canvas_id=canvas_id,
                                                                  tray_id=gate_tray))
        texts.append(json.dumps(caught.value.as_dict()))
    blob = "\n".join(texts)
    assert blob.count("\"") > 100                      # the replies really were rendered, not skipped
    assert HOST not in blob and FAKE_TOKEN not in blob and SN not in blob
