"""Method 1036 (the CC2's own print history) against scripted replies, in the same house style as
``test_cc2_info``: nothing opens a socket, the fake transport records every method, its params and the
timeout each request asked for, and every frame it answers is a shape Elegoo's own LAN web client is
read out of its published bundle — never a shape invented here. What the tests pin down:

* the wire shape: method 1036 **with no parameters at all** (the official client sends ``{method:
  1036}`` and nothing else), asked exactly once, waited for 30 s because that is what its own call
  site passes, and never resent after a timeout or a refusal. No paging key of any kind ever goes out:
  the ``page``/``page_size`` pair the community manual lists for 1036 belongs to Elegoo's *cloud*
  history call, which is paginated by ``pageNumber``/``pageSize`` and answers camelCase;
* a cloud-shaped reply refuses with its own reason instead of being read as a LAN reply, and a cloud
  ``totalTasks`` is never used as a count of the LAN list;
* missing fields stay null and are named in that entry's ``not_reported`` — the nine names the official
  client reads and no others — while an empty ``history_task_list`` is what the printer sent rather
  than a refusal, and is explicitly not tellable from a firmware that keeps history elsewhere;
* the reply may only claim to be complete when the printer's own ``total`` meets the entries it
  actually sent: a total above them, below them, unreadable, or absent each say so in their own
  sentence, and a list that had to be cut to 100 entries is reported as cut (with both counts) and
  never as the whole history;
* the size bound withholds, it does not truncate: an over-long text never reaches the reply (the
  generic sanitiser's own 255-character cut is never what trims a name) while the other entries of the
  same reply survive;
* the one credential-shaped field is handled: a time-lapse link carrying ``user:pass@`` is withheld,
  its presence reported, and the secret never rides the reply;
* what the numbers mean is not guessed: the status word next to the status number is labelled as
  Elegoo's own app's table (with 2 and 3 sharing a word), an unknown number gets no word, a duration
  is the printer's own two stamps subtracted exactly as its app does, 0 stays "not recorded", a
  reversed pair earns no duration, and an implausibly large stamp is reported as suspect-milliseconds
  rather than converted;
* and every shape this module cannot read is reported as that — an entry that is not an object, a list
  that is not a list, a reply that is not an object — with key names but never values, one request, and
  no field fitted afterwards.

Nothing here has been tested against a printer; the module says so in its own replies
(``verified_on_this_printer``), and the tests are the reason that sentence is honest."""
from __future__ import annotations

import copy
import json

import pytest

from orcaslicer_mcp.printer import cc2_history as hist
from orcaslicer_mcp.printer.cc2_transport import Cc2TimeoutError, PrinterError

HOST = "192.0.2.10"                   # documented TEST-NET, like the whole lane; never a live address
SN = "CC2TESTSN0001"
SECRET = "SIMULATED-NOT-A-CREDENTIAL"
T0 = 1772820155                       # a unix-seconds stamp in 2026, the shape the app expects
T1 = T0 + 4690


class FakeTransport:
    """A session that records and answers: one entry per method, a callable answering per call, an
    exception raising as given, and an unscripted method answering ``{}`` — which is the no-field trap
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


def entry(**over) -> dict:
    """One entry of ``history_task_list`` carrying all nine of the names Elegoo's own web client reads
    out of it — the field list its row mapper performs on the LAN reply, no more and no less."""
    base = {"task_id": "b52af24c-764e-4092-8a50-00e5f8f02b46", "task_name": "benchy-petg.gcode",
            "begin_time": T0, "end_time": T1, "task_status": 1,
            "time_lapse_video_status": 1, "time_lapse_video_url": f"http://{HOST}/tl.mp4",
            "time_lapse_video_size": 4096, "time_lapse_video_duration": 120}
    return {**base, **over}


def reply(*entries, **over) -> dict:
    body = list(entries) if entries else []
    return {"error_code": 0, "history_task_list": body, **over}


async def read(body) -> dict:
    """One scripted reply through the whole read, as the dict a caller would be handed."""
    return (await hist.fetch_history(FakeTransport({1036: body}))).as_dict()


# --------------------------------------------------------------------------- 1. the wire shape

async def test_history_is_asked_with_no_parameters_at_all():
    transport = FakeTransport({1036: reply(entry(), total=1)})
    await hist.fetch_history(transport)
    assert transport.methods() == [1036]
    params, _timeout = transport.one(1036)
    assert params == {}                       # the parameterless request, as the official client sends


async def test_no_paging_key_ever_leaves_this_module():
    # the manual's page/page_size row for 1036 is Elegoo's cloud shape (pageNumber/pageSize against
    # the cloud getPrintTaskList). Sending it to the LAN endpoint is the confusion this pins out.
    for banned in ("page", "page_size", "pageNumber", "pageSize", "offset", "limit", "dir",
                   "storage_media", "uuid", "printerId"):
        assert banned not in hist.history_params()
    assert hist.history_params() == {}


async def test_the_wait_is_the_official_callers_own_thirty_seconds():
    transport = FakeTransport({1036: reply(total=0)})
    await hist.fetch_history(transport)
    _params, timeout = transport.one(1036)
    assert timeout == 30.0


async def test_a_caller_supplied_timeout_reaches_the_request():
    transport = FakeTransport({1036: reply(total=0)})
    await hist.fetch_history(transport, timeout_s=7.5)
    assert transport.one(1036)[1] == 7.5


def test_the_method_numbers_are_the_ones_elegoos_own_enum_defines():
    assert hist.METHOD_PRINT_TASK_LIST == 1036
    assert hist.METHOD_PRINT_TASK_DETAIL == 1037    # named for the record — never sent by any code


async def test_the_detail_method_is_never_asked_even_when_the_list_refuses():
    # 1037 has no published request or response anywhere, so nothing on any path asks for it: not the
    # refusal path, not the empty-list path, not after a timeout.
    for body in ({}, {"error_code": 1003}, {"history_task_list": "nope"},
                   {"error_code": 0, "totalTasks": 2, "taskList": []}):
        transport = FakeTransport({1036: body, 1037: {"error_code": 0, "secret": SECRET}})
        with pytest.raises(PrinterError):
            await hist.fetch_history(transport)
        assert transport.methods() == [1036]


async def test_a_lost_reply_stays_lost_and_is_never_resent():
    transport = FakeTransport({1036: Cc2TimeoutError("not_reachable", "simulated: the printer never answered",
                                            reason="timeout")})
    with pytest.raises(Cc2TimeoutError):
        await hist.fetch_history(transport)
    assert transport.methods() == [1036]


# --------------------------------------------------------------------------- 2. the envelope

@pytest.mark.parametrize("code", [1003, 1, 1001])
async def test_a_refusal_keeps_the_printers_own_code_and_becomes_no_list(code):
    transport = FakeTransport({1036: {"error_code": code, "history_task_list": [entry()]}})
    with pytest.raises(PrinterError) as got:
        await hist.fetch_history(transport)
    assert got.value.code == "protocol_error"
    assert got.value.details["error_code"] == code
    assert got.value.details["reason"] == "declined"
    assert transport.methods() == [1036]     # and nothing was asked a second time


@pytest.mark.parametrize("code", ["0", None, 0.0, True, [0]])
async def test_an_error_code_that_cannot_be_read_refuses_rather_than_becoming_zero(code):
    with pytest.raises(PrinterError) as got:
        await hist.fetch_history(FakeTransport({1036: {"error_code": code}}))
    assert got.value.details["reason"] == "envelope_malformed"


@pytest.mark.parametrize("bad", [None, [], "history", 7])
async def test_a_reply_that_is_not_an_object_at_all_refuses(bad):
    with pytest.raises(PrinterError) as got:
        await hist.fetch_history(FakeTransport({1036: bad}))
    assert got.value.details["reason"] == "history_malformed"


async def test_a_missing_error_code_reads_as_success_like_every_other_cc2_read():
    out = await read({"history_task_list": [entry()], "total": 1})
    assert out["entries_kept"] == 1 and out["complete"] is True


# --------------------------------------------------------------------------- 3. the shape it cannot read

async def test_a_reply_with_no_history_field_names_its_keys_and_never_its_values():
    body = {"error_code": 0, "task_records": [{"task_name": SECRET}], "cursor": SECRET}
    with pytest.raises(PrinterError) as got:
        await hist.fetch_history(FakeTransport({1036: body}))
    assert got.value.details["reason"] == "no_history_field"
    assert "task_records" in got.value.message        # the key it did carry
    assert SECRET not in got.value.message            # never the value it carried with it


async def test_a_cloud_shaped_reply_refuses_as_the_cloud_shape_it_is():
    # The RTM path paginates by pageNumber/pageSize and answers totalTasks/taskList in camelCase; it is
    # a different request to a different place and this tool does not read one as the other.
    cloud = {"error_code": 0, "totalTasks": 5, "taskList": [{"taskId": "x", "taskName": "a.gcode"}]}
    with pytest.raises(PrinterError) as got:
        await hist.fetch_history(FakeTransport({1036: cloud}))
    assert got.value.details["reason"] == "cloud_shape"
    assert "cloud" in got.value.message.lower()
    assert "pageNumber" in got.value.message


async def test_a_cloud_count_beside_a_lan_list_is_never_read_as_the_lan_count():
    # both families in one reply: the LAN field is what gets read, and totalTasks is not silently
    # borrowed as `total` to make the list look self-accounting.
    out = await read({"error_code": 0, "history_task_list": [entry()], "totalTasks": 99})
    assert out["entries_kept"] == 1
    assert out["total_from_printer"] is None
    assert out["complete"] is False


@pytest.mark.parametrize("wrong", [{}, "nope", 5, None, {}])
async def test_a_history_list_that_is_not_a_list_refuses(wrong):
    with pytest.raises(PrinterError) as got:
        await hist.fetch_history(FakeTransport({1036: {"error_code": 0, "history_task_list": wrong}}))
    assert got.value.details["reason"] == "history_malformed"


@pytest.mark.parametrize("bad", ["a string", 5, None, ["nested"]])
async def test_an_entry_that_is_not_an_object_refuses_the_reply_naming_its_index(bad):
    body = {"error_code": 0, "history_task_list": [entry(), bad]}
    with pytest.raises(PrinterError) as got:
        await hist.fetch_history(FakeTransport({1036: body}))
    assert got.value.details["reason"] == "history_malformed"
    assert got.value.details["entry"] == 1        # which row broke it, without quoting what it held


async def test_an_empty_list_is_what_the_printer_sent_rather_than_a_failure():
    out = await read(reply(total=0))
    assert out["entries"] == [] and out["complete"] is True   # it counted nothing and sent nothing
    assert "warnings" not in out


async def test_an_empty_list_with_no_count_says_what_cannot_be_known():
    out = await read(reply())
    assert out["entries"] == [] and out["complete"] is False
    assert any("cannot tell an empty history" in w for w in out["warnings"])


# --------------------------------------------------------------------------- 4. one entry

async def test_the_nine_names_the_official_client_reads_are_read_field_for_field():
    out = await read(reply(entry(), total=1))
    got = out["entries"][0]
    assert got["task_id"] == "b52af24c-764e-4092-8a50-00e5f8f02b46"
    assert got["task_name"] == "benchy-petg.gcode"
    assert got["begin_time"] == T0 and got["end_time"] == T1
    assert got["duration_seconds"] == T1 - T0     # the app's own arithmetic: end minus begin
    assert got["task_status"] == 1
    assert got["time_lapse_video_status"] == 1
    assert got["time_lapse_video_url"] == f"http://{HOST}/tl.mp4"
    assert got["time_lapse_video_size"] == 4096
    assert got["time_lapse_video_duration"] == 120
    assert got["not_reported"] == []


async def test_the_stamps_arrive_as_themselves_and_as_dates():
    out = await read(reply(entry(), total=1))
    got = out["entries"][0]
    assert got["began_at"].startswith("2026-") and got["ended_at"].endswith("+00:00")
    assert got["begin_time"] == T0               # the number it arrived as, kept regardless


async def test_every_documented_field_that_never_arrived_is_null_and_named():
    out = await read(reply({}, total=1))
    got = out["entries"][0]
    assert set(got["not_reported"]) == {"task_id", "task_name", "begin_time", "end_time",
                                        "task_status", "time_lapse_video_status",
                                        "time_lapse_video_url", "time_lapse_video_size",
                                        "time_lapse_video_duration"}
    for key in ("task_id", "task_name", "begin_time", "end_time", "task_status",
                "duration_seconds", "time_lapse_video_status", "time_lapse_video_url",
                "time_lapse_video_size", "time_lapse_video_duration"):
        assert got[key] is None


async def test_an_empty_string_is_silence_not_an_answer():
    # this firmware is measured answering text fields empty elsewhere (the 1001 version strings); an
    # empty print name is the printer saying nothing, not a print named "".
    out = await read(reply(entry(task_name="", task_id="   "), total=1))
    got = out["entries"][0]
    assert got["task_name"] is None and got["task_id"] is None
    assert {"task_name", "task_id"} <= set(got["not_reported"])


@pytest.mark.parametrize(("status", "word"), [(0, "other"), (1, "complete"), (2, "cancel"),
                                              (3, "cancel"), (4, "printing"), (5, "pause")])
async def test_the_status_word_is_elegoos_own_app_table_and_two_numbers_share_one_word(status, word):
    out = await read(reply(entry(task_status=status), total=1))
    got = out["entries"][0]
    assert got["task_status"] == status          # the number is never dropped for the word
    assert got["task_status_label"] == word


async def test_a_number_off_the_apps_table_earns_no_word():
    out = await read(reply(entry(task_status=77), total=1))
    assert out["entries"][0]["task_status"] == 77
    assert out["entries"][0]["task_status_label"] is None


@pytest.mark.parametrize("bad", [True, "1", 1.0, {}, []])
async def test_a_status_that_is_not_a_number_is_silence_rather_than_zero(bad):
    out = await read(reply(entry(task_status=bad), total=1))
    assert out["entries"][0]["task_status"] is None
    assert "task_status" in out["entries"][0]["not_reported"]


async def test_zero_means_not_recorded_rather_than_midnight_january_1970():
    out = await read(reply(entry(begin_time=0, end_time=0), total=1))
    got = out["entries"][0]
    assert got["begin_time"] == 0 and got["began_at"] is None
    assert got["duration_seconds"] is None       # two silences do not subtract into a duration of 0


async def test_a_duration_is_never_invented_from_a_reversed_pair_of_stamps():
    out = await read(reply(entry(begin_time=T1, end_time=T0), total=1))
    got = out["entries"][0]
    assert got["duration_seconds"] is None
    assert any("end_time is earlier" in note for note in got["notes"])


async def test_a_stamp_too_big_for_seconds_is_reported_as_suspicious_not_converted():
    out = await read(reply(entry(begin_time=T0 * 1000, end_time=T1 * 1000), total=1))
    got = out["entries"][0]
    assert got["began_at"] is None and got["ended_at"] is None
    assert got["begin_time"] == T0 * 1000              # the printer's number, kept as it arrived
    assert got["duration_seconds"] is not None          # the subtraction is unit-free
    assert any("milliseconds" in note for note in got["notes"])


async def test_the_order_the_printer_sent_is_the_order_the_reply_uses():
    # the official app sorts by begin_time for its own table; this server is not that app.
    first, second = entry(task_name="older.gcode", begin_time=T0 - 100), entry(task_name="newer.gcode",
                                                                                begin_time=T0 + 100)
    out = await read(reply(first, second, total=2))
    assert [e["task_name"] for e in out["entries"]] == ["older.gcode", "newer.gcode"]


async def test_a_duplicated_name_stays_duplicated_rather_than_being_tided_away():
    out = await read(reply(entry(), entry(), total=2))
    assert [e["task_name"] for e in out["entries"]] == ["benchy-petg.gcode"] * 2


# --------------------------------------------------------------------------- 5. size bounds

async def test_an_over_long_field_is_withheld_rather_than_cut_in_half():
    out = await read(reply(entry(task_name="y" * 5000), total=1))
    got = out["entries"][0]
    assert got["task_name"] is None
    assert any("5000 characters" in note for note in got["notes"])
    assert "y" * 300 not in json.dumps(out)
    # and not one character of it reaches the reply as the generic sanitiser's 255-character cut
    # either: that cut is what a plausible-looking fake name would arrive as, so the bound here has to
    # withhold the field before the reply is made safe, which is what this proves it does.
    assert "\u2026" not in json.dumps(out)


async def test_the_sanitisers_own_ceiling_is_never_what_trims_a_name():
    # _cc2_out caps strings at 255 characters; a name exactly at that ceiling must arrive whole, so
    # the module's own bound and the sanitiser's cannot both be quietly in play.
    whole = "n" * 255
    out = await read(reply(entry(task_name=whole), total=1))
    assert out["entries"][0]["task_name"] == whole


async def test_the_bound_costs_one_field_not_the_history():
    second = entry(task_id="z" * 400, task_name="readable.gcode")
    out = await read(reply(entry(task_name="x" * 9000), second, total=2))
    assert out["entries_kept"] == 2
    assert out["entries"][1]["task_name"] == "readable.gcode"
    assert out["entries"][1]["task_id"] is None
    assert any("task_id" in note for note in out["entries"][1]["notes"])


@pytest.mark.parametrize("bad", [5, 5.5, True, ["a"]])
async def test_a_field_of_the_wrong_type_is_silence_with_its_type_named(bad):
    out = await read(reply(entry(task_name=bad), total=1))
    got = out["entries"][0]
    assert got["task_name"] is None
    assert "task_name" in got["not_reported"]
    assert any("not a piece of text" in note for note in got["notes"])


async def test_more_entries_than_one_reply_may_carry_are_reported_as_cut():
    many = [entry(task_name=f"print-{i}.gcode") for i in range(hist.MAX_HISTORY_ENTRIES + 7)]
    out = await read(reply(*many, total=len(many)))
    assert out["entries_kept"] == hist.MAX_HISTORY_ENTRIES
    assert out["entries_in_reply"] == len(many)
    assert out["truncated_in_reply"] is True
    assert out["complete"] is False
    assert any("not the whole history" in w for w in out["warnings"])
    assert not any(e["task_name"] == f"print-{len(many) - 1}.gcode" for e in out["entries"])
    assert "no \u2014 no next page" not in json.dumps(out)     # (wording guard, see next test)


async def test_a_cut_list_says_there_is_no_next_page_to_ask_for():
    many = [entry(task_name=f"print-{i}.gcode") for i in range(hist.MAX_HISTORY_ENTRIES + 1)]
    out = await read(reply(*many))
    assert "no page parameters" in out["completeness"]
    assert "1038" in out["completeness"]        # deleting history is not offered as a way to page


async def test_the_cap_sits_under_the_servers_own_list_cap_so_our_sentence_wins():
    from orcaslicer_mcp import server
    assert hist.MAX_HISTORY_ENTRIES < server._CC2_LIST_MAX


# --------------------------------------------------------------------------- 6. what the list may claim

async def test_the_printers_own_count_meeting_its_own_entries_is_the_only_complete_list():
    out = await read(reply(entry(), entry(), total=2))
    assert out["complete"] is True
    assert "meets" in out["completeness"]


async def test_a_count_above_the_entries_sent_is_reported_as_an_incomplete_list():
    out = await read(reply(entry(), total=57))
    assert out["entries_kept"] == 1 and out["total_from_printer"] == 57
    assert out["complete"] is False
    assert "disagree" in " ".join(out["warnings"]).lower()
    assert "do not meet" in out["completeness"]


async def test_a_count_below_the_entries_sent_is_a_contradiction_said_out_loud():
    out = await read(reply(entry(), entry(), entry(), total=1))
    assert out["complete"] is False
    assert any("disagree" in w for w in out["warnings"])


@pytest.mark.parametrize("bad", ["2", -1, True, 1.0, {}])
async def test_a_count_that_cannot_be_read_does_not_destroy_the_readable_list(bad):
    out = await read(reply(entry(), total=bad))
    assert out["entries_kept"] == 1                       # the prints are still reported
    assert out["total_from_printer"] is None
    assert out["complete"] is False
    assert any("could not be read" in w for w in out["warnings"])


async def test_no_count_of_its_own_means_no_claim_of_being_whole():
    out = await read(reply(entry()))
    assert out["total_from_printer"] is None and out["complete"] is False
    assert "no total of its own" in out["completeness"]


# --------------------------------------------------------------------------- 7. credentials and replies

async def test_a_time_lapse_link_carrying_a_login_is_withheld_and_its_secret_never_rides():
    url = f"http://nathan:{SECRET}@{HOST}/timelapse.mp4"
    out = await read(reply(entry(time_lapse_video_url=url), total=1))
    got = out["entries"][0]
    assert got["time_lapse_video_url"] is None
    assert "user name" in got["time_lapse_video_url_withheld"]
    assert SECRET not in json.dumps(out)
    assert "time_lapse_video_url" in got["not_reported"]   # silence about the link itself


async def test_a_link_without_a_login_is_carried_as_it_arrived():
    out = await read(reply(entry(time_lapse_video_url=f"http://{HOST}:8080/a/b.mp4?x=1"), total=1))
    assert out["entries"][0]["time_lapse_video_url"] == f"http://{HOST}:8080/a/b.mp4?x=1"


async def test_a_plain_time_lapse_and_a_withheld_one_are_not_the_same_sentence():
    plain = await read(reply(entry(time_lapse_video_url=None), total=1))
    hidden = await read(reply(entry(time_lapse_video_url=f"http://u:p@{HOST}/x"), total=1))
    assert "time_lapse_video_url_withheld" not in plain["entries"][0]
    assert "time_lapse_video_url_withheld" in hidden["entries"][0]


async def test_no_raw_reply_rides_any_of_these_replies():
    out = await read(reply(entry(), total=1))
    assert "raw" not in json.dumps(out)
    assert "history_task_list" not in json.dumps(out)      # the wire shape is not echoed back


# --------------------------------------------------------------------------- 8. the candidate label

async def test_a_clean_read_still_says_it_has_never_been_seen_on_this_printer():
    out = await read(reply(entry(), total=1))
    assert out["verified_on_this_printer"] is False
    assert "ever been captured" in out["unverified"]
    assert out["method"] == 1036


async def test_the_reply_names_where_its_status_words_come_from():
    out = await read(reply(entry(), total=1))
    assert "Elegoo's own LAN web client" in out["status_labels"]
    assert "no unit" in out["numbers"]


# --------------------------------------------------------------------------- 9. the whole call

async def test_one_successful_call_asks_for_exactly_one_thing():
    transport = FakeTransport({1036: reply(entry(), total=1)})
    out = await hist.fetch_history(transport)
    assert out.entries[0].task_name == "benchy-petg.gcode"
    assert transport.methods() == [1036]


async def test_every_reply_here_is_json_a_client_can_hold():
    out = await read(reply(entry(), entry(task_status=9), total=2))
    assert json.loads(json.dumps(out)) == out
