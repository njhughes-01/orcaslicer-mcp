"""The CC2's own print history: method 1036 (PRINT_TASK_LIST), one read-only request, asked exactly
once, read with nothing fitted to it. This module is a **candidate for live verification** and says so
in every reply it produces — see the last section of this docstring for exactly what is established
and what has never been seen.

**What is established, and by whom.** Elegoo's own LAN web client — the one served by the printer at
``/lan/``, read here as the published ``index.html`` bundle — asks for the history list like this::

    const Be = {method: Pt.GetHistoryTask}                    // no params key at all
    Be = await dn.getPrintHistoryList(serial, Be, 1e4 * 3)    // 30 s, its own call-site override
    if (Be.error_code !== 0)  toastMessages.getHistoryFailed  // "Failed to get print history."
    const list = Be.history_task_list.sort((a, b) => b.begin_time - a.begin_time)

and its history table then reads, per entry, ``task_id``, ``task_name``, ``begin_time``,
``end_time``, ``task_status``, ``time_lapse_video_status``, ``time_lapse_video_url``,
``time_lapse_video_size`` and ``time_lapse_video_duration`` (its row mapper reads those nine names
and nothing else; the table shows file name, status, start time, end time and duration). The method
number is the bundle's own enum — ``GetHistoryTask=1036``, ``GetHistoryTaskDetail=1037``,
``HistoryDelete=1038`` — and the timeout used here is that call site's: 30 s, which is the same
longer-of-the-two first-party windows rule the peripheral and temperature modules record (this
client's class default for the method is 5 s, this server's transport default is 10 s, the official
caller overrides both to 30 s). Two more facts come from the same component and are used rather than
invented: its duration column is ``end_time - begin_time`` rendered as hours/minutes/seconds, which
is the official client's own proof that **both stamps are unix seconds**, and its status column is
``m[A] || "Unknow"`` with ``m = {0:"Other", 1:"Complete", 2:"Cancel", 3:"Cancel", 4:"Printing",
5:"Pause"}``. That table is an *app label*, and an odd one — 2 and 3 are the same word — which is why
the reply always carries the printer's own ``task_status`` number next to the label and never
collapses them.

**The cloud path is the trap, and it is not this.** The same bundle also holds a cloud ("RTM")
history path: ``Rr.request("getPrintTaskList", {printerId, pageNumber, pageSize})``, answered with
``{totalTasks, taskList:[{taskId, taskName, beginTime, endTime, taskStatus}]}`` and *synthesised* into
the snake_case shape above, with the four ``time_lapse_*`` fields hard-filled to ``0``/``""``. That
is camelCase, paginated by page number, and its ``total`` is a cloud count. So: this module sends no
page parameter of any kind, reads none of the camelCase names, and treats a reply that arrives in the
cloud shape as a refusal with its own reason rather than as something to blend. The community
protocol manual's methods table lists 1036 with ``{"page": 1, "page_size": 10}`` — that row matches
the cloud path's shape, has no measurement behind it in the manual, and is contradicted by the
official LAN client, which sends no parameters; nothing on this wire follows the manual here. Same for
the manual's ``begin_time``/``end_time`` note, which is the only thing it says about history at all:
this module agrees with the note, but the note is not where the field list comes from.

**What has never been seen: a reply from Nathan's printer.** No capture of a 1036 answer from this
firmware exists in any source, so a reply that does not have the shape the official client reads is
reported as unrecognised, never as an empty history and never as a history read another way. Absent
fields stay null *and* are named in that entry's ``not_reported``, per cc2_info's rule for the
information reads: "the printer said nothing" and "the print has no name" are different facts. A
reply that carries no ``history_task_list`` field at all refuses with the *keys* it did carry (keys,
never values), because a firmware that renamed its fields is how that happens and the next reader
needs the names. A ``history_task_list: []`` is not a refusal and not an empty success either: it is
what the printer sent, and without a count of its own it cannot be told apart from a firmware that
keeps history somewhere else, which the reply says.

**Bounds, because this is a list and the printer decides how long it is.** The request carries no
page size, so nothing limits the answer: the reply keeps at most ``MAX_HISTORY_ENTRIES`` entries in
the printer's own order and says loudly that it truncated (how many arrived, how many it kept, and
that there is no page to ask for next because this method takes no parameters and 1038 *deletes*
history, which this server does not send). The cap is 100, deliberately under this server's own reply
cap of 200 per list, so the truncation is always this module's honest sentence and never the generic
sanitiser silently dropping rows. Text is bounded at 255 characters for the same reason — the
sanitiser would cut a long string at exactly that length, and a name must either be the printer's own
or be reported as withheld with its length named, never half a name with an ellipsis on the end. A
field that arrives as the wrong type is silence with its type named rather than a refused history: one
odd field must not cost a hundred readable prints, and a never-captured reply is exactly where the
type that arrived is the fact worth having.

**Nothing is assumed about a count, an order or a status.** If the printer sends ``total`` it is read
as its own number and used only to decide whether the reply accounts for itself: a count matching the
entries it sent is what earns ``complete: true``, a count above them says the list is not complete,
and a count that cannot be read is reported unreadable while the entries stay (a broken count must
not destroy a readable history). The entries are never sorted, filtered or de-duplicated here — the
official app sorts by ``begin_time`` for its own table and this server is not that app.

**Credentials:** the only field that can carry one is ``time_lapse_video_url``, so a link with a
user-info part (``scheme://user:pass@host``) is withheld entirely rather than echoed, with its
presence reported and the reason said — the same rule the transport applies to a login hidden in a
printer address. No host, no serial, no token and no local path is read or carried by anything here,
and no ``raw`` reply rides along: this is the one CC2 read that keeps no raw copy, because echoing a
hundred raw entries back inside one reply is exactly the size problem the module exists to bound.

**1037 (PRINT_TASK_DETAIL) is deliberately not implemented and is not offered.** The community manual
shows it with ``{"uuid": "..."}`` and nothing else; in Elegoo's whole 3.6 MB official LAN bundle the
name ``GetHistoryTaskDetail`` appears exactly twice, both in the enum that defines it, and is never
called — so no caller, no parameter shape and no response has ever been published for it. The bundle
does contain history *detail* calls, but under a different, older command namespace (a second
websocket client whose commands are ``0/1/128/258/320 CmdGetHistoryTasks/321 CmdGetTaskDetails`` with
``{Id: …}`` bodies and ``HistoryData`` replies), which is not this method family and proves nothing
about 1037's answer. The protocol manual itself leaves history on its not-yet-documented list.
Per the plan, a method whose request and response are not established is documented as omitted rather
than guessed at, and no ``uuid`` is ever taken from a 1002 status frame and sent somewhere as a task
handle. 1038 (deleting history entries) is a write to a record and likewise unimplemented.

Nothing here is tested against a printer: every test is a scripted reply. Until a real 1036 answer is
captured, the honest summary of a successful call is "the printer answered in the shape Elegoo's own
web client reads", which is what the reply says about itself, and the honest summary of a refusal is
"this firmware answers 1036 differently than that client does" — a fact worth recording, not a bug to
work around.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

# The envelope gate is shared with every other CC2 read on purpose: what an absent, zero, nonzero or
# unreadable error_code means cannot be allowed to drift between the file listing and the history.
from .cc2_state import Cc2DataError, _check_envelope

METHOD_PRINT_TASK_LIST = 1036       # PRINT_TASK_LIST — the official LAN client sends it parameterless
METHOD_PRINT_TASK_DETAIL = 1037     # PRINT_TASK_DETAIL — named for the record; never sent by this code

# The official caller's own window for this one method (its call site passes 30 s where its class
# default is 5 s, and this server's transport default is 10 s). A history list is the one read that
# may be long, so the longest first-party patience is the one taken.
HISTORY_TIMEOUT_S = 30.0

# Entries one reply may carry. Ours, not the printer's: no source establishes a page size for 1036
# because the official request asks for no paging at all. It sits under this server's own
# _CC2_LIST_MAX (200 per list) so that if it ever bites, it bites first, with its own sentence, and
# the generic sanitiser never silently drops a row behind our back.
MAX_HISTORY_ENTRIES = 100

# One ceiling for every string this module emits. It is the same number the reply sanitiser caps
# strings at (_CC2_STR_MAX), on purpose: a name, an id or a link is either the printer's own or
# refused with its length named — never the sanitiser's silent half-string with an ellipsis.
_MAX_TEXT = 255

# The wire names the official client reads, and the only ones this module reads. The camelCase cloud
# names are here to be *refused*, not accepted: they are how a cloud-shaped reply announces itself.
_ENTRY_FIELDS = (
    "task_id", "task_name", "begin_time", "end_time", "task_status",
    "time_lapse_video_status", "time_lapse_video_url", "time_lapse_video_size",
    "time_lapse_video_duration",
)
_CLOUD_NAMES = ("taskList", "totalTasks", "taskId", "taskName", "beginTime", "endTime", "taskStatus")

# Elegoo's own LAN client's status column: `m[A] || "Unknow"`. Verbatim, lower-cased for the words
# this server uses elsewhere, with 2 and 3 left as the same word because that is what its table says.
_APP_STATUS_WORDS = {0: "other", 1: "complete", 2: "cancel", 3: "cancel", 4: "printing", 5: "pause"}

# Above this, a number is not a plausible unix-seconds clock (it is past the year 5138) and is a
# plausible unix *milliseconds* clock (1973). Used only to say which, never to convert: this server
# does not guess at units.
_MS_SUSPECT_S = 100_000_000_000


# --------------------------------------------------------------------------- refusals


def _refuse(why: str, reason: str, **details) -> Cc2DataError:
    return Cc2DataError("protocol_error",
                        f"The printer's {METHOD_PRINT_TASK_LIST} reply {why}.", reason=reason,
                        method=METHOD_PRINT_TASK_LIST, **details)


def _keys_seen(result: dict, limit: int = 12) -> str:
    """The key names a reply carried, for a refusal that has to say what arrived. Keys only — never
    values, which is where a print name or a signed link would ride out in plain text."""
    return ", ".join(sorted(str(key) for key in result)[:limit]) or "(no keys at all)"


def _looks_like_cloud(result: dict) -> bool:
    return any(name in result for name in _CLOUD_NAMES)


# --------------------------------------------------------------------------- field readers


def _stamp(value: Any) -> int | None:
    """A documented unix timestamp: a plain non-negative int, else nothing. A bool is an int in
    Python and is not a time; a float, a digit string, a negative and an absent are all silence, and
    none of them is coerced or rounded into an answer."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _whole(value: Any) -> int | None:
    return _stamp(value)


def _text(value: Any, *, field_name: str) -> tuple[str | None, str | None]:
    """A documented piece of text, as (the text, a note about it). Clean text comes back as itself;
    anything else comes back as None plus the sentence that says why, never as a half value.

    Two silences are chosen over two refusals here, and both choices are deliberate. A field that
    arrives as the wrong type (a number where a name belongs) is reported as silence *with its type
    named*, rather than refusing the whole history: a never-captured reply is exactly where a reader
    wants the type that arrived, and one odd field must not cost a hundred readable prints. A field
    longer than ``_MAX_TEXT`` is reported as withheld rather than shortened: the alternative is this
    server's own sanitiser cutting it at that same 255 characters and handing back half a file name as
    though the printer had written it. Withholding is also the bound that keeps an enormous reply out
    of the conversation without lying about what was in it."""
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, (f"its {field_name} arrived as type {type(value).__name__}, which is not a piece "
                      "of text, so it is reported as silence with its type named rather than spelled "
                      "out as if it were one")
    text = value.strip()
    if not text:
        return None, None
    if len(text) > _MAX_TEXT:
        return None, (f"its {field_name} is {len(text)} characters long, over this server's "
                      f"{_MAX_TEXT} character ceiling for one field, so it is withheld rather than "
                      "cut down to fit: half a file name or task id is a claim the printer never made")
    return text, None


def _moment(stamp: int | None) -> datetime | None:
    """A unix-seconds stamp as a date, or None: 0 is the printer's own "not recorded", and a number
    the calendar cannot hold is no date. Callers report the number as it arrived regardless."""
    if stamp is None or stamp == 0:
        return None
    with contextlib.suppress(ValueError, OSError, OverflowError):
        return datetime.fromtimestamp(stamp, tz=UTC)
    return None


def _unit_suspect(stamp: int | None) -> bool:
    """Is this number too far out to be a seconds clock and about right for a milliseconds one? That
    is a sentence to say, not a value to convert: the official client's own arithmetic
    (``end_time - begin_time`` rendered as h/m/s) says seconds, and if this firmware sent
    milliseconds, the honest answer is that the unit differs and the date stays null."""
    return stamp is not None and stamp > _MS_SUSPECT_S


# --------------------------------------------------------------------------- one entry


@dataclass(frozen=True)
class Cc2HistoryEntry:
    """One finished (or abandoned, or current) print as method 1036 lists it. Every documented field
    is None when the printer said nothing about it and is named again in ``not_reported``.
    ``task_status`` is the printer's own number and ``task_status_label`` is the word Elegoo's own
    web client paints for that number — the label is provenance-labelled in the reply because it is
    an app's vocabulary, not a protocol's, and because two of its numbers share one word.
    ``duration_seconds`` is arithmetic on the printer's own two stamps (the official client derives it
    the same way) and is null, never invented, when either stamp is missing or they are reversed."""
    task_name: str | None = None
    task_id: str | None = None
    task_status: int | None = None
    task_status_label: str | None = None
    begin_time: int | None = None
    began_at: datetime | None = None
    end_time: int | None = None
    ended_at: datetime | None = None
    duration_seconds: int | None = None
    time_lapse_video_status: int | None = None
    time_lapse_video_url: str | None = None
    url_withheld: str | None = None
    time_lapse_video_size: int | None = None
    time_lapse_video_duration: int | None = None
    not_reported: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @classmethod
    def from_entry(cls, entry: Any, *, index: int) -> "Cc2HistoryEntry":
        """One entry of ``history_task_list``. An entry that is not an object refuses the whole
        reply — a list with a hole in it is not a list, and this is the same rule the file listing
        applies to its own entries. Nothing in here is required: a print the printer listed without a
        name is listed without a name, and that is reported as silence."""
        if not isinstance(entry, dict):
            raise _refuse(f"sent entry number {index} of its history list as a "
                          f"{type(entry).__name__} rather than an object, so the list has a hole in "
                          "it and cannot be read whole",
                          "history_malformed", entry=index)
        notes: list[str] = []
        name, name_note = _text(entry.get("task_name"), field_name="task_name")
        task_id, id_note = _text(entry.get("task_id"), field_name="task_id")
        url_raw, url_note = _text(entry.get("time_lapse_video_url"),
                                  field_name="time_lapse_video_url")
        for note in (name_note, id_note, url_note):
            if note:
                notes.append(f"the printer listed {index}: {note}")
        status = _whole(entry.get("task_status"))
        begin = _stamp(entry.get("begin_time"))
        end = _stamp(entry.get("end_time"))
        # 0 is the printer's own "not recorded" (the file-detail read documents last_print_time the
        # same way), so a duration is computed only between two stamps that both claim a moment. The
        # subtraction itself is the official client's own: its duration column is end minus begin.
        duration = (end - begin if begin and end and end >= begin else None)

        url = url_raw
        withheld = None
        if url is not None and _carries_login(url):
            # A link with a login in it is a credential, however innocent it looks here: the transport
            # refuses to route to one, so this read does not repeat one either. Its existence stays.
            withheld = ("the printer sent a time-lapse link that carries a user name in it "
                        "(scheme://user@host), which this server never repeats; only its presence "
                        "is reported")
            url = None

        if _unit_suspect(begin) or _unit_suspect(end):
            notes.append("a timestamp in this entry is far too large to be unix seconds and about "
                         "the right size for unix milliseconds; the dates are left null rather than "
                         "converted, because the unit is not established for this firmware")
        if begin is not None and end is not None and end < begin:
            notes.append("the printer's own end_time is earlier than its begin_time, so no duration "
                         "is computed from them")

        # Keyed by the wire names, because that is what not_reported names: a field a reader looks up
        # under a different name than the printer wrote is how a silence gets mistaken for a fact.
        parsed = {
            "task_name": name, "task_id": task_id, "task_status": status,
            "begin_time": begin, "end_time": end,
            "time_lapse_video_status": _whole(entry.get("time_lapse_video_status")),
            "time_lapse_video_size": _whole(entry.get("time_lapse_video_size")),
            "time_lapse_video_duration": _whole(entry.get("time_lapse_video_duration")),
            # An absent link and a withheld one are both silence about the link itself; the note
            # above is what keeps them from reading as the same answer.
            "time_lapse_video_url": url,
        }
        missing = tuple(sorted(wire for wire in _ENTRY_FIELDS if parsed.get(wire) is None))
        return cls(
            task_name=name, task_id=task_id, task_status=status,
            task_status_label=(None if status is None else _APP_STATUS_WORDS.get(status)),
            begin_time=begin, began_at=_moment(begin), end_time=end, ended_at=_moment(end),
            duration_seconds=duration,
            time_lapse_video_status=parsed["time_lapse_video_status"],
            time_lapse_video_url=url, url_withheld=withheld,
            time_lapse_video_size=parsed["time_lapse_video_size"],
            time_lapse_video_duration=parsed["time_lapse_video_duration"],
            not_reported=missing, notes=tuple(notes))

    def as_dict(self) -> dict:
        """One row, in the printer's own field names, with the dates readable alongside the numbers
        they came from. The status number and the app's word for it sit next to each other so a
        reader can disagree with the label and still have the fact."""
        out: dict = {
            "task_name": self.task_name,
            "task_id": self.task_id,
            "task_status": self.task_status,
            "task_status_label": self.task_status_label,
            "begin_time": self.begin_time,
            "began_at": self.began_at.isoformat() if self.began_at else None,
            "end_time": self.end_time,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_seconds": self.duration_seconds,
            "time_lapse_video_status": self.time_lapse_video_status,
            "time_lapse_video_duration": self.time_lapse_video_duration,
            "time_lapse_video_size": self.time_lapse_video_size,
            "time_lapse_video_url": self.time_lapse_video_url,
            "not_reported": list(self.not_reported),
        }
        if self.url_withheld:
            out["time_lapse_video_url_withheld"] = self.url_withheld
        if self.notes:
            out["notes"] = list(self.notes)
        return out


def _carries_login(url: str) -> bool:
    """Does this link hide a login in its authority? ``user:pass@host`` is what the transport refuses
    to route to, and it is what makes a URL a credential rather than an address. A link that is not
    parseable is not treated as carrying one — it stays a link, bounded like every other string."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return bool(parts.username or parts.password)


# --------------------------------------------------------------------------- the list


@dataclass(frozen=True)
class Cc2PrintHistory:
    """What the printer listed: its own entries in its own order, its own count when it sent one that
    could be read, and an explicit statement of whether the reply accounts for itself. ``complete`` is
    True only when the printer's own total meets the entries it actually sent and nothing had to be
    dropped for size; everything else says so in ``complete: false`` plus a reason, because a
    history list that quietly stops halfway is the one way this read could mislead."""
    entries: tuple[Cc2HistoryEntry, ...] = ()
    entries_in_reply: int = 0
    entries_kept: int = 0
    total_from_printer: int | None = None
    total_present: bool = False
    total_unreadable: bool = False
    truncated: bool = False
    verified_on_this_printer: bool = False
    raw_total_value_type: str | None = None

    @classmethod
    def from_result(cls, result: Any) -> "Cc2PrintHistory":
        """A 1036 reply, read. Object, then not a refusal, then ``history_task_list`` as a list, then
        every entry. What is refused, in order: not an object; a nonzero ``error_code`` (the printer's
        own answer, with its number kept — the official client shows that case as "Failed to get
        print history." and this server does not decide what any particular code means); no
        ``history_task_list`` field, which is said with the keys that did arrive and, when one of the
        cloud names is among them, named as the cloud shape it is; a list that is not a list; an entry
        that is not an object."""
        if not isinstance(result, dict):
            raise _refuse("was not an object at all", "history_malformed")
        _check_envelope(result, METHOD_PRINT_TASK_LIST)
        if "history_task_list" not in result:
            if _looks_like_cloud(result):
                raise _refuse(
                    "is shaped like Elegoo's cloud (RTM) history answer — it carries "
                    f"{', '.join(name for name in _CLOUD_NAMES if name in result)} — rather than the "
                    "LAN answer this method gives, whose field is history_task_list. The two are not "
                    "read alike: the cloud list is paginated by pageNumber/pageSize and this request "
                    "sends no paging at all",
                    "cloud_shape",
                    hint="This tool speaks to the printer over the LAN, where the official web "
                         "client asks method 1036 with no parameters and reads history_task_list. A "
                         "cloud-shaped answer on the LAN wire is worth recording, not adapting.")
            raise _refuse(f"carried no history_task_list field, only the key(s) {_keys_seen(result)}",
                          "no_history_field",
                          hint="Nothing was assumed in its place and nothing was asked again. If "
                               "this firmware names its history list differently, that is a fact "
                               "worth recording against method 1036; 1037 (task detail) has never "
                               "had a request or a response published at all and is not offered.")
        raw = result["history_task_list"]
        if not isinstance(raw, list):
            raise _refuse(f"sent history_task_list as a {type(raw).__name__} rather than a list",
                          "history_malformed")

        total_raw = result.get("total")
        total_present = "total" in result
        total = _whole(total_raw) if total_present else None
        total_unreadable = total_present and total is None
        entries_in_reply = len(raw)

        # The cap is applied before parsing: the entries beyond it are not read, are not summarised,
        # and are not pretended to be the end of the list.
        kept = raw[:MAX_HISTORY_ENTRIES]
        entries = tuple(Cc2HistoryEntry.from_entry(item, index=i) for i, item in enumerate(kept))
        return cls(entries=entries, entries_in_reply=entries_in_reply, entries_kept=len(entries),
                   total_from_printer=total, total_present=total_present,
                   total_unreadable=total_unreadable,
                   truncated=entries_in_reply > len(entries),
                   raw_total_value_type=(type(total_raw).__name__ if total_unreadable else None))

    # -- what the reply may claim ----------------------------------------------------------

    @property
    def complete(self) -> bool:
        """Does this reply account for every print the printer says it has? Only its own count can
        say so, and only if nothing had to be dropped to fit the reply."""
        if self.truncated or self.total_from_printer is None:
            return False
        return self.total_from_printer == self.entries_in_reply == self.entries_kept

    @property
    def completeness_reason(self) -> str:
        if self.complete:
            return ("the printer's own total meets the entries it sent, and nothing was dropped, so "
                    "this is the whole list it reported")
        if self.truncated:
            return (f"the printer sent {self.entries_in_reply} entries and this reply carries "
                    f"{self.entries_kept} of them, so it is not the whole list — and there is no "
                    "next page to ask for, because method 1036 is sent no page parameters and "
                    "1038, which removes history, is not something this server sends")
        if self.total_from_printer is None and self.total_present:
            return ("the printer sent a total that could not be read as a count, so completeness "
                    "cannot be checked against it")
        if self.total_from_printer is None:
            return ("the printer sent no total of its own, so nothing here can tell a short history "
                    "from the whole of one")
        return (f"the printer counted {self.total_from_printer} but sent "
                f"{self.entries_in_reply}, so its own two answers do not meet")

    def as_dict(self) -> dict:
        """The tool's reply: the entries, then the statements a reader needs next to them — whether
        the list accounts for itself and why not if it does not, where the status words come from,
        what order this is, and the standing admission that no reply of this shape has ever been
        captured from this printer. No raw reply and no host ride along."""
        out = {
            "method": METHOD_PRINT_TASK_LIST,
            "entries": [entry.as_dict() for entry in self.entries],
            "entries_kept": self.entries_kept,
            "entries_in_reply": self.entries_in_reply,
            "total_from_printer": self.total_from_printer,
            "complete": self.complete,
            "completeness": self.completeness_reason,
            "truncated_in_reply": self.truncated,
            "order": "the printer's own order — nothing here sorts, filters, de-duplicates or "
                     "re-reads what it sent",
            "status_labels": (
                "task_status is the printer's own number. task_status_label is the word Elegoo's own "
                "LAN web client paints for that number (0 other, 1 complete, 2 cancel, 3 cancel, "
                "4 printing, 5 pause; a number off its table carries no label). That table is an "
                "app's vocabulary, not a protocol's, and two of its numbers share a word, which is "
                "why the number is always here too."),
            "numbers": (
                "begin_time and end_time are unix seconds — Elegoo's own client subtracts one from "
                "the other for its duration column, which is its own proof of the unit — and "
                "began_at/ended_at are those same numbers turned into dates. task_status, "
                "time_lapse_video_status, time_lapse_video_size and time_lapse_video_duration are "
                "carried under the printer's own names with no unit and no vocabulary claimed for "
                "them, because no source says what unit the last two arrive in or what any of the "
                "numbers mean on this firmware."),
            "verified_on_this_printer": self.verified_on_this_printer,
            "unverified": (
                "No 1036 reply has ever been captured from this printer. The fields read here are "
                "the fields Elegoo's own LAN web client reads from that method; a reply shaped "
                "differently is refused rather than fitted, so a refusal from this tool is evidence "
                "about this firmware, not a broken printer."),
        }
        warnings: list[str] = []
        if self.truncated:
            warnings.append(f"truncated: the printer sent {self.entries_in_reply} entries and this "
                            f"reply carries the first {self.entries_kept} of them in the printer's "
                            "own order. This is not the whole history.")
        if self.total_unreadable:
            warnings.append(f"the printer sent a total that could not be read (it arrived as a "
                            f"{self.raw_total_value_type}), so completeness could not be checked "
                            "against it; the entries it sent are still reported")
        if self.entries_in_reply == 0 and not self.complete:
            # Only worth saying when the printer did not account for the emptiness: a printer that
            # counted zero prints and sent zero prints answered the question, that is an empty history.
            warnings.append("the printer listed no prints at all. With no count of its own that is "
                            "what it sent, and this tool cannot tell an empty history from a "
                            "firmware that keeps history somewhere else")
        if not self.complete and not self.truncated and self.total_from_printer is not None \
                and self.total_from_printer != self.entries_in_reply:
            warnings.append("the printer's own count and the entries it sent disagree, so neither "
                            "number here should be trusted as the size of the history")
        if warnings:
            out["warnings"] = warnings
        return out


# --------------------------------------------------------------------------- the read


def history_params() -> dict:
    """The 1036 request parameters: none, and there is no argument for any. The official LAN client
    sends ``{method: 1036}`` with no params key at all; the community manual's ``page``/``page_size``
    row matches Elegoo's *cloud* history call, which is paginated by ``pageNumber``/``pageSize`` and
    answers a different shape. Sending the cloud's paging to the LAN endpoint, or reading the cloud's
    answer as this one, is the confusion this function exists to make impossible."""
    return {}


async def fetch_history(transport: Any, *, timeout_s: float | None = None) -> Cc2PrintHistory:
    """Ask the printer for its print history (method 1036, no parameters) exactly once and read what
    comes back. One request: a timeout stays a timeout — this method is a read, but a retry would
    still be a second request nobody established this firmware tolerates, and nothing here resends.
    The wait is the official caller's own 30 s unless a caller names another. Malformed, empty-shaped
    and cloud-shaped replies refuse with their own reasons; missing fields stay null and named."""
    params = history_params()
    result = await transport.request(METHOD_PRINT_TASK_LIST, params,
                                     timeout_s=HISTORY_TIMEOUT_S if timeout_s is None else timeout_s)
    return Cc2PrintHistory.from_result(result)
