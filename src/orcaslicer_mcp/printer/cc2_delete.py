"""Delete one named file from a CC2 — method 1047 asked exactly once, believed only as far as the
printer's own follow-up listing lets it be believed — as an **internal candidate that is not a
tool yet**. Nothing imports this module and nothing registers it: a client is not shown a delete,
because nobody has offered this request shape to Nathan's firmware and the plan forbids advertising
a capability before a live test proves it (see ``openspec/changes/cc2-direct-print/revised-plan.md``,
"Files and remaining documented methods"). The adapter, its wire shape and its honesty contract are
complete and tested against mocks so that the live test is one call, not a build.

**What the sources actually say, and where they stop.** Elegoo's own LAN web client, the bundle the
printer serves itself (read from the minified build at ``~/scratch/tmp/webapp/lan/index.html``),
names the method ``DeleteFile=1047`` and builds the request literally as
``{method: Pt.DeleteFile, params: {storage_media: <media>, file_path: <path>}}``, sending it through
its own ``request()``, whose default timeout is 5 s and to which the delete call passes no timeout
of its own. Its storage enum spells the internal store ``local``. That is the whole of the first-party
evidence for the request shape, and it is client-side: it says what Elegoo's software sends, not what
this firmware does with it. The community manual (``docs/CC2_PROTOCOL.md`` of the local
``elegoo-homeassistant-2.16.0-beta.2``) describes 1047 and warns plainly that the file methods do not
share a parameter spelling — 1045 wants ``file_name``, 1046 wants ``filename``, 1047 wants
``file_path`` — and a previous attempt against a **different** firmware, which asked with
``filename``, answered ``error_code`` 1003 and deleted nothing. Neither fact is proof about this
printer; findings from other firmware never were adopted as proof here.

**Therefore: the official shape only, once, with no fallback.** ``delete_params`` emits exactly
``{"storage_media": "local", "file_path": <name>}`` — the pair the client sends, with the internal
store the manual measures. No other spelling of the parameter is ever put on the wire, no leading
slash is added or stripped, no directory prefix is invented, and a refused or unanswered delete is
never offered again in another shape. The leading-slash question is genuinely open: that same client
prepends ``"/"`` for 1045/1046 when the store is the removable one and does **nothing** to the path
for 1047, so what it sends for the internal store is whatever its file list showed — and this
adapter follows it by sending the listing's own name verbatim. Because
``cc2_info.checked_lookup_name`` is the bar (see below), a slashed path cannot reach here at all: it
refuses locally rather than becoming an experiment on the printer. If a live test shows this firmware
wants ``/name.gcode``, that is a new decision with evidence behind it, not a fallback to be coded
tonight.

**The wait is the sources' own, by the rule this package already uses.** The web client gives the
delete its default 5 s and the Home Assistant integration waits 10 s for every command it sends, so
``DELETE_WAIT_S`` is 10 s — the longer of the two first-party windows, the same arithmetic
``cc2_peripherals`` records for 1029/1031. A delete is not a resume: no source measures a long
answer, so no longer window is invented either.

**A fresh listing is the evidence, and it is read once, under one rule.** After the command this
module takes exactly one read-only 1044 in the only shape ever observed answering this printer (the
un-paged ``{"storage_media": "local", "path": "/"}``) and interprets it by the rule ``cc2_state``
built the pager on: **presence in a listing proves presence; absence proves anything only when the
reply accounts for its own files**, i.e. its own ``total`` equals the distinct names it delivered. So
``file_now`` is ``"still_present"`` whenever the name is among the entries (even in a listing that
proves nothing else), ``"gone"`` only when an accounting reply does not name it, and ``"unknown"``
when a reply that cannot account for itself does not name it. A partial listing is never allowed to
say "gone", which is the one sentence a caller must not get wrong about a delete. The read is never
combined with another, never repeated, and never a second command: ``status`` says what the printer
answered the 1047 and ``file_now`` says what the listing shows, and the notes spell out every
combination of the two — including the two contradictions worth saying out loud, an accepted command
whose file is still listed and a refused command whose file is gone anyway (the second is never
attributed to this call).

**Why the job controls' "no read after a refusal" convention does not transfer.** For pause, resume
and stop, an explicit refusal is a complete answer about a transient state and the courtesy read is
droppable (``cc2_control`` says so and its tests pin it). Deletion is irreversible and the file
methods' codes on this firmware are known to be uninterpreted — the same manual records a 1045
answering 1003 for a file in an otherwise working session, and ``cc2_info`` refuses to rename 1003
"file not found" for exactly that reason. So here the listing, not the code, is the evidence, and it
is taken after accepted **and** refused **and** unanswered commands alike, costing read-only requests
and buying the only proof available. The exception is a session that died: a session that cannot
carry a command cannot carry a read either, so that outcome reports ``file_now="not_checked"`` and
sends nothing further at all.

**Two gates stand before a command, and only those two.** The name is held to
``cc2_info.checked_lookup_name``, which is ``cc2_list_files``' own bar — a single plain name, no
separators in either dialect, no ``.``/``..``, no control characters, at most 255 characters — so
this can only ever be pointed at a file the printer's own listing could show, path traversal is
refused locally before a request exists, and nothing above the printer's file store is reachable.
And a file the printer's own 1002 names as its current job is never deleted; an active-looking print
state that names no file refuses too, because that is the same uncertainty ``Cc2MachineStatus``
``active_job`` reports rather than a guess in either direction. That read is re-taken on the
confirmed call on purpose: a job may have started between the preview and the confirmation, and
destroying the file a printer is mid-print reading is the one outcome no confirmation can undo.
Nothing else is pre-checked. There is no completeness precondition on the listing, no idle-state
requirement, no second confirmation and no block on "probably in use" heuristics: the user's
authorization covers deleting files, the printer refuses what it will not do with its own code, and
inventing CC2-specific barriers the plan does not ask for is the failure mode this branch has been
removing, not adding.

**Two-phase, like every other control on this server.** Without ``confirm=True`` the caller gets
``read_preview``, which sends the read-only 1002 and the read-only 1044 and never a 1047 — proven on
the recorded wire — and shows the exact params a confirmed call would send, the job the printer is
running, and whether the name is in the printer's own listing. The confirmation is a
decision-for-a-human device, not an access control: it carries the user's authorization for this
deletion, which
their standing yes for this session may already supply, so it obliges no fresh question. A confirmed
call sends 1002 (the active-file gate), one 1047, and one 1044 (the verification), in that order,
each once.

Privacy: nothing here takes an address, a token or an environment variable — the transport is the
pinned session — and no note, message or result carries a credential or a raw reply. Notes and
messages are written inside the direct tools' reply cap (``server.py``'s ``_CC2_STR_MAX``), with the
reason a long failure string is kept in its own field rather than pasted into a note where the cap
would cut its tail.

**Exposing it later takes four deliberate edits**, all of them after a live test on a uniquely named
test file has answered the open questions: add the tool to ``server.py`` (preview/``confirm=true``
like ``_cc2_control_tool``), add its name to ``CC2_TOOL_NAMES``, add its row to
``_TOOL_ANNOTATIONS`` (a write, and ``destructiveHint=True``: unlike the other writes on this
server, this one destroys a file the printer holds) and to ``_OPEN_WORLD_TOOLS``, then regenerate
``lhm.plugin.json``, and re-check the wording of the places that currently tell a user this server
never deletes (``README.md``, ``cc2_upload``'s docstring and ``cc2_upload_gcode``'s) — a delete that
exists but is unproven and a delete that is offered are different promises, and both of those
sentences were written when the second was the only one on the table.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import cc2_control as job
from . import cc2_info as info
from . import cc2_state as st
from .cc2_control import ControlStatus   # the same three-way answer, one vocabulary
from .cc2_transport import Cc2TimeoutError, PrinterError

METHOD_DELETE = 1047   # DELETE_FILE — {"storage_media", "file_path"} (the web client's own shape)

# The wait is the longer of the two first-party windows: the official LAN client gives this method
# its default 5 s (it passes no timeout) and the Home Assistant integration waits 10 s for every
# command it sends. Nothing longer is invented: no source measures a slow delete the way a resume
# is measured, so a bigger number here would be a guess dressed as caution.
DELETE_WAIT_S = 10.0

# What the follow-up listing can say about the named file. Four words, because "the listing proved
# nothing" and "the listing accounted for itself and did not name it" are different sentences, and
# only the second one is allowed to mean gone.
FILE_NOW_GONE = "gone"
FILE_NOW_PRESENT = "still_present"
FILE_NOW_UNKNOWN = "unknown"
FILE_NOW_NOT_CHECKED = "not_checked"

# What the same probe says before it becomes a verdict, in the preview, where "listed" is a fact
# about a listing and nothing yet has been deleted. "unknown" is what a reply that cannot account
# for itself says about a name it does not contain: silence, not absence.
_LISTED = "listed"
_NOT_LISTED = "not_listed"
_PROVES_NOTHING = "unknown"

# The print states that mean a file is being held. IDLE (the observed empty word with machine
# status 1), STANDBY, COMPLETE, CANCELLED and ERROR are not among them; UNKNOWN is, because a state
# word this adapter does not know is trusted with nothing — including with the safety of a name.
_HOLDING_STATES = (st.PrintState.PRINTING, st.PrintState.PAUSED, st.PrintState.UNKNOWN)

# Notes and messages are written to fit the direct tools' reply cap in server.py (_CC2_STR_MAX =
# 255), the same discipline cc2_control documents: a note cut mid-sentence loses the half a model
# acts on. A failure string longer than this is kept in its own field instead of being pasted into
# a note, where the cap would eat the honesty at the end.
NOTE_CAP = 255
_WHY_MAX = 80

# What a preview must show before anybody is asked to authorize an irreversible act, in the
# maintainer companion's words adapted to a delete: the companion previews a motion, this previews
# something no method undoes. Bounded by NOTE_CAP like the notes.
PREVIEW_HOW = ("Nothing was sent to the printer, and no CC2 method undoes a delete. Show this "
               "preview for the user's go-ahead unless they already authorized deleting this file "
               "(a standing yes counts), then call again with confirm=true. One file per "
               "confirmation.")

# The four sentences that must be in front of a person before they confirm, because each one is an
# open question rather than a settled fact. Each is inside NOTE_CAP and pinned by a test.
UNVERIFIED: tuple[str, ...] = (
    "1047 has never been answered by this printer's firmware: Elegoo's own LAN client sends exactly "
    "these params, and a different firmware refused a delete asked with `filename` at error_code 1003.",
    "file_path is sent exactly as cc2_list_files names the file — no leading slash, no directory "
    "prefix. The official client adds a slash only for removable media on other file methods, and "
    "no variant of this ask has been offered to this printer.",
    "The check afterwards is one un-paged 1044 reply: it can prove the name is there, and it can "
    "only prove it is gone when that reply accounts for its own files.",
    "A deleted file is gone for good: stock CC2 offers no way to read a file's content back and no "
    "method restores one.",
)

# Acceptance means one thing here and one thing only, and the tail is the delete's own: the
# acceptance base is cc2_control's, imported so every control tool on this server says the same
# thing about what an error_code 0 does and does not mean.
_ACCEPT_TAIL = ("A delete is irreversible: no CC2 method reads a file back and none undoes a "
                "delete, so acceptance is only ever as good as the fresh listing reported beside "
                "it. Nothing is resent either way.")

# The two contradictions an accepted/refused command can arrive with — an accepted command whose file
# is still listed, and a refused command whose file is gone anyway — are not special-cased with their
# own constants: they are two cells of ``plain_answer``, written there so that neither fact overwrites
# the other and the refused one is never credited with a removal this call cannot prove.

# The sentence a partial listing earns, with the reason in its own field and the rule in the note.
_INCOMPLETE_LIST_NOTE = ("The follow-up listing could not certify itself ({why}), so its silence "
                         "about a name is not evidence of absence and no claim of gone is made "
                         "from it.")


# --------------------------------------------------------------------------- the two gates


def checked_delete_path(value: Any) -> str:
    """The name exactly as the printer's own listing shows it, or a local refusal. This is
    ``cc2_info.checked_lookup_name`` and nothing else — deliberately the same bar the file reads
    use, because a delete must accept exactly what ``cc2_list_files`` can print and nothing above
    it. A name carrying a path (in either dialect), a ``.``/``..``, a control character, leading or
    trailing space, an empty string or anything over 255 characters refuses here, before a session
    exists, so path traversal is never something the printer gets to interpret and a slashed
    ``file_path`` is never something this module guesses at."""
    return info.checked_lookup_name(value, label="file name")


def checked_storage_media(value: Any) -> str:
    """``local`` and nothing else. The removable volumes are spelled two ways by the two
    first-party sources and no 1047 has ever been answered for any store on this firmware, so
    asking for one would be inventing a request twice over — refused with those words, before a
    request exists, exactly as ``cc2_info`` refuses them for 1045/1046."""
    if isinstance(value, str) and value == info.STORAGE_LOCAL:
        return info.STORAGE_LOCAL
    spelling = (f" {str(value)[:24]!r} is not a storage this server asks for" if isinstance(value, str)
                else " that is not a storage this server asks for")
    raise st.Cc2DataError(
        "protocol_error",
        f"Only the printer's internal storage ('local') is ever asked of a 1047, and even that has "
        f"never been answered by this firmware;{spelling}, so nothing was sent.",
        hint="cc2_list_files lists the internal store, and that is the store this deletes from. The "
             "removable volumes are spelled 'u-disk'/'udisk' and 'sd-card'/'sdcard' by the sources "
             "and were never offered to this firmware, so their spelling is not guessed at.",
        reason="storage_unsupported", method=METHOD_DELETE)


def delete_params(path: Any, storage: Any = info.STORAGE_LOCAL) -> dict:
    """The 1047 params exactly as Elegoo's own client builds them: ``storage_media`` and
    **``file_path``** — a third spelling in the file-method family, pinned by a test so a
    "consistency" cleanup cannot quietly turn it into ``filename`` (the shape a previous firmware
    refused at 1003) or ``file_name`` (what 1045 wants). The path is sent verbatim: no slash added,
    none stripped, no directory prefix."""
    return {"storage_media": checked_storage_media(storage), "file_path": checked_delete_path(path)}


def _refuse_if_active(name: str, machine: st.Cc2MachineStatus) -> None:
    """Never delete the file the printer is holding, and never assume it isn't holding one. The
    first case is the printer's own word — its 1002 names this file as the job — and it refuses
    whatever the state word says, because a paused or completing job still owns its file. The second
    is the uncertainty ``Cc2MachineStatus.active_job`` documents: a print state that means "a job is
    running" while the filename field is empty says only that something may own a file, so the
    delete refuses rather than betting a name against it."""
    if machine.job_filename == name:
        raise st.Cc2DataError(
            "protocol_error",
            f"{name!r:.80} is the file the printer's own status names as its current job, so it is "
            "an active file and this server never deletes one. Nothing was sent.",
            hint="Read cc2_status. When the printer no longer names that file as its job, "
                 "cc2_list_files will still list it and this tool can be pointed at it.",
            reason="active_file", method=METHOD_DELETE)
    if machine.job_filename is None and machine.print_state in _HOLDING_STATES:
        word = job._state_word(machine) or "an unrecognised print state"
        raise st.Cc2DataError(
            "protocol_error",
            f"The printer reports {word} without naming its file, so it cannot be ruled out that "
            f"{name!r:.80} is the file it is holding. Nothing was sent.",
            hint="Read cc2_status and wait until the printer is idle and names no job, then ask "
                 "again. A state this adapter cannot read is never treated as an idle one.",
            reason="active_file_unknown", method=METHOD_DELETE)


async def _gate(transport: Any, path: Any, *, timeout_s: float | None = None
                ) -> tuple[str, st.Cc2MachineStatus]:
    """Both gates, in the order that costs least: the name is validated with no I/O at all, then one
    read-only 1002 rules out the active file. A status read that fails is raised to the caller, not
    papered over — neither a preview nor a delete may be built on a guess about what is printing."""
    name = checked_delete_path(path)
    machine = await st.fetch_machine_status(transport, timeout_s=timeout_s)
    _refuse_if_active(name, machine)
    return name, machine


# --------------------------------------------------------------------------- the verification read


@dataclass(frozen=True)
class _Listing:
    """One read-only 1044 reply, read for the one question this module asks: is this name in it?
    ``listed`` is ``"listed"`` (proof, needing no certification), ``"not_listed"`` (only when
    ``certified``) or ``"unknown"``; ``certified`` means the reply's own ``total`` equals the count
    of distinct names it delivered; ``why`` is the compact reason a non-certifying reply proves
    nothing about absence, and ``why_long`` the same reason at whatever length it needs."""
    listed: str
    certified: bool
    count: int | None
    total: int | None
    size: int | None = None
    why: str | None = None
    why_long: str | None = None


def _short(text: str) -> str:
    """A failure string trimmed to fit inside a note, cut at a word boundary with the ellipsis that
    says something was cut. The untrimmed text is not lost: it rides in its own field, where the
    reply cap has the whole budget to itself instead of a shared note losing its tail."""
    if len(text) <= _WHY_MAX:
        return text
    return text[:_WHY_MAX].rsplit(" ", 1)[0] + "…"


async def _read_listing(transport: Any, name: str, *, timeout_s: float | None = None) -> _Listing:
    """One 1044, asked once, read by ``cc2_state``'s own gates — the same envelope rule, the same
    entry parser and the same refusal of a listing that names a file twice. It is the un-paged
    request shape (``{"storage_media": "local", "path": "/"}``), the only 1044 shape ever observed
    answering this printer, and this module never pages: a delete needs one decisive answer, and the
    pager's semantics (``dir``/``offset``/``limit``) have never been observed on this firmware
    either. Anything that stops the reply from being read at all — a timeout, a declined code, a
    malformed entry, a dead session — is a ``"unknown"`` with the printer's reason kept, never an
    exception that would throw away the delete's own outcome and never a reason to ask twice."""
    try:
        reply = await transport.request(st.METHOD_FILES, {"storage_media": "local", "path": "/"},
                                        timeout_s=timeout_s)
        entries, total = st._read_files_page(reply)
        rows = st._read_rows(entries, {}, where="in the listing read around the 1047 command")
    except PrinterError as e:
        why = getattr(e, "message", None) or str(e)
        return _Listing(_PROVES_NOTHING, False, None, None, None,
                        _short(f"it could not be read — {why}"), why_long=why)
    found = next((r for r in rows if r.filename == name), None)
    if found is not None:
        # presence is presence whatever the reply's own arithmetic says about itself
        return _Listing(_LISTED, total is not None and len(rows) == total, len(rows), total,
                        found.size)
    certified = total is not None and len(rows) == total
    if certified:
        return _Listing(_NOT_LISTED, True, len(rows), total)
    if total is None:
        why = (f"it named {len(rows)} files and reported no total to account for them against")
    elif len(rows) > total:
        why = (f"it named {len(rows)} files while reporting a total of {total}")
    else:
        why = (f"it delivered {len(rows)} of the {total} files it counts")
    return _Listing(_PROVES_NOTHING, False, len(rows), total, None, _short(why), why_long=why)


# --------------------------------------------------------------------------- the result


@dataclass(frozen=True)
class Cc2DeleteResult:
    """The whole outcome of one delete attempt, in two facts that are never merged into one.
    ``status``/``accepted``/``error_code`` say what the printer answered the single 1047 (the
    job controls' vocabulary, imported so acceptance means one thing on this server); ``file_now``
    says what the single follow-up listing shows. ``deletion_proved`` is true only for the one pair
    that earns it — an accepted command **and** a self-accounting listing that no longer names the
    file — because an unanswered command whose file is gone is evidence the command *may* have
    landed, not that this call deleted it. ``sent_params`` is the exact dict that went on the wire
    and ``command_attempted`` is whether a 1047 was published at all, so a refusal from a gate is
    never confused with a command that went out. The ``listing_*`` fields are the printer's own
    numbers from the verification read; ``listing_why`` is the full reason a read proved nothing.
    ``notes`` carry what must be said out loud, including the plain answer that reads this pair of
    facts as a sentence."""
    filename: str
    status: ControlStatus
    waited_s: float
    file_now: str = FILE_NOW_NOT_CHECKED
    accepted: bool = False
    error_code: int | None = None
    method: int = METHOD_DELETE
    command_attempted: bool = False
    sent_params: dict | None = None
    deletion_proved: bool = False
    listing_certified: bool | None = None
    listing_count: int | None = None
    listing_total: int | None = None
    listing_why: str | None = None
    job_state: str | None = None
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "filename": self.filename,
            "method": self.method,
            "status": str(self.status),
            "accepted": self.accepted,
            "error_code": self.error_code,
            "waited_s": self.waited_s,
            "command_attempted": self.command_attempted,
            "sent_params": dict(self.sent_params) if self.sent_params else None,
            "file_now": self.file_now,
            "deletion_proved": self.deletion_proved,
            "listing_certified": self.listing_certified,
            "listing_count": self.listing_count,
            "listing_total": self.listing_total,
            "listing_why": self.listing_why,
            "job_state": self.job_state,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- the notes


def delete_refusal_note(code: int) -> str:
    """A refused delete in the printer's own words, with its code kept and its meaning left alone.
    ``cc2_control.refusal_note`` is the shared wording for a refusal, but its 1010 gloss ("no active
    print") is a job-control fact and not a fact about a file, so the delete says the part that
    transfers and adds the part the job controls do not have to say: what a code means per-file on
    this firmware has never been established, and 1003 in particular is the code one firmware gave
    for a file method it went on to refuse for other reasons."""
    if code == 1003:
        return (f"The printer refused the delete command (error_code 1003 — the code a previous "
                f"firmware returned for a file operation it did not carry out, and what it means "
                f"here is not established). It is the printer's own answer on record.")
    return (f"The printer refused the delete command (error_code {code}). What it means on this "
            f"firmware is not established; it is the printer's own answer on record.")


def plain_answer(status: ControlStatus, file_now: str, code: int | None) -> str:
    """The two facts read as one sentence — every cell of the matrix, including the two
    contradictions. Phrased so that neither fact overwrites the other and nothing is credited or
    blamed that the evidence does not reach: an accepted command plus a vanished name is the only
    combination called deleted, and an unanswered command whose name is gone stays "consistent
    with", never "deleted by this call"."""
    refused = f"error_code {code}" if code is not None else "a nonzero code"
    if status is ControlStatus.ACCEPTED:
        if file_now == FILE_NOW_GONE:
            return ("Deleted: the printer answered the delete with an explicit error_code 0 and one "
                    "fresh listing of its own files no longer names it. Nothing undoes this.")
        if file_now == FILE_NOW_PRESENT:
            return ("NOT gone: the printer accepted the delete and its own listing still names the "
                    "file. That is a contradiction and it is reported as one; nothing was resent.")
        return ("The printer accepted the delete, but the follow-up listing did not account for its "
                "own files, so whether the file is gone is unproven. Nothing was resent.")
    if status is ControlStatus.REFUSED:
        if file_now == FILE_NOW_PRESENT:
            return (f"Not deleted: the printer refused the command ({refused}) and the file is in a "
                    f"fresh listing of its own files. Nothing was resent.")
        if file_now == FILE_NOW_GONE:
            return (f"The printer refused the command ({refused}) and the file is gone from its own "
                    f"listing anyway, so this call is not credited with removing it. Nothing resent.")
        return (f"The printer refused the command ({refused}); the follow-up listing proved nothing, "
                f"so whether the file remains is unproven. Nothing was resent.")
    if file_now == FILE_NOW_PRESENT:
        return ("The printer never answered the delete, and the file is in a fresh listing: "
                "consistent with the command not having landed. Nothing was resent.")
    if file_now == FILE_NOW_GONE:
        return ("The printer never answered the delete, and the file is gone from a fresh listing: "
                "consistent with the command having landed. This call cannot prove who removed it.")
    if file_now == FILE_NOW_NOT_CHECKED:
        return ("The session ended without an answer and could carry no follow-up read, so the "
                "outcome is unknown. Nothing was resent, not even a read-only listing.")
    return ("The printer never answered the delete and the follow-up listing proved nothing, so the "
            "outcome is unknown. Nothing was resent.")


# --------------------------------------------------------------------------- the preview


async def read_preview(transport: Any, path: Any, *, timeout_s: float | None = None) -> dict:
    """What a confirmed call would send, and the two facts a person needs to decide: what the printer
    is doing right now, and whether the name it holds is in its own listing. The read-only 1002 and
    the read-only 1044 below are the only methods this path ever sends — no 1047 leaves the wire
    without ``confirm=True``, which the tests prove on the recorded wire — and both are asked once.

    The name and the active file are refused here before anything is sent, so a caller cannot reach a
    preview with a path or with the file being printed. A listing that cannot account for itself is
    reported as ``"unknown"`` with its own numbers rather than as ``"not_listed"``, because "it was
    not in the part of the list that arrived" is not a fact about a file."""
    name, machine = await _gate(transport, path, timeout_s=timeout_s)
    listing = await _read_listing(transport, name)
    preview = {
        "will_send": {"method": METHOD_DELETE, "params": delete_params(name)},
        "waits_up_to_s": DELETE_WAIT_S,
        "then_reads": {"method": st.METHOD_FILES, "params": {"storage_media": "local", "path": "/"}},
        "job_state": job._state_word(machine),
        "job_filename": machine.job_filename,
        "file_in_this_listing": listing.listed,
        "file_size_bytes": listing.size,
        "listing_count": listing.count,
        "listing_total": listing.total,
        "listing_certified": listing.certified,
        "listing_proves_nothing_because": listing.why_long or listing.why,
        "unverified": list(UNVERIFIED),
    }
    return {"confirm_required": True, "action": "delete", "preview": preview, "how": PREVIEW_HOW}


# --------------------------------------------------------------------------- the one command


async def delete_file(transport: Any, path: Any, *, timeout_s: float | None = None
                      ) -> Cc2DeleteResult:
    """Ask for one deletion: two gates, one 1047, one verifying listing, in that order, each asked
    once and none of them ever repeated. ``timeout_s`` overrides the documented wait for tests.

    The answer decides the words, exactly as the job controls read an answer (only an explicit
    integer ``error_code`` 0 is acceptance; a missing, bool, float or text code is an ambiguity; any
    other integer is the printer declining on record), and the listing decides what is true about
    the file. Both are reported whatever the other says, in every combination, and no combination
    earns a second command — least of all the two contradictions, which are what a caller has to
    resolve deliberately, with ``cc2_list_files`` and their own eyes."""
    name, machine = await _gate(transport, path)
    wait_s = DELETE_WAIT_S if timeout_s is None else timeout_s
    params = delete_params(name)
    state_word = job._state_word(machine)

    def outcome(status: ControlStatus, command_notes: tuple[str, ...], listing: _Listing | None,
                *, file_now: str, code: int | None = None, accepted: bool = False
                ) -> Cc2DeleteResult:
        proved = status is ControlStatus.ACCEPTED and file_now == FILE_NOW_GONE
        notes = list(command_notes)
        notes.append(plain_answer(status, file_now, code))
        if listing is not None and listing.why is not None:
            notes.append(_INCOMPLETE_LIST_NOTE.format(why=listing.why))
        return Cc2DeleteResult(
            filename=name, status=status, waited_s=wait_s, file_now=file_now, accepted=accepted,
            error_code=code, command_attempted=True, sent_params=dict(params),
            deletion_proved=proved, job_state=state_word, notes=tuple(notes),
            listing_certified=None if listing is None else listing.certified,
            listing_count=None if listing is None else listing.count,
            listing_total=None if listing is None else listing.total,
            listing_why=None if listing is None else (listing.why_long or listing.why))

    # ---- gate + one command, then one listing. The command is published exactly once.
    try:
        reply = await transport.request(METHOD_DELETE, params, timeout_s=wait_s)
    except Cc2TimeoutError:
        # cc2_control's own lost-answer note: neither a refusal nor a failure, and never a resend.
        # A delete that was never answered is the case the verifying listing exists for.
        listing = await _read_listing(transport, name)
        return outcome(ControlStatus.AMBIGUOUS,
                       (job.LOST_NOTE.format(verb="delete", wait=wait_s),), listing,
                       file_now=_file_now(listing))
    except PrinterError as e:
        # The session died or the command could not be published. Nothing more can be read from a
        # session that cannot carry a request, so the listing is reported as not attempted rather
        # than as unknown — and the command is still not reconsidered.
        why = getattr(e, "message", None) or str(e)
        return outcome(ControlStatus.AMBIGUOUS,
                       (job.DEAD_NOTE.format(verb="delete") + why,), None,
                       file_now=FILE_NOW_NOT_CHECKED)

    # ``error_code``: only an explicit integer answers (cc2_control's bar, and the reason a reply
    # that merely arrived is not a printer that agreed).
    if not isinstance(reply, dict) or "error_code" not in reply:
        detail = "carried no error code at all"
        listing = await _read_listing(transport, name)
        return outcome(ControlStatus.AMBIGUOUS,
                       (job.UNREADABLE_NOTE.format(verb="delete", detail=detail),), listing,
                       file_now=_file_now(listing))
    code = reply["error_code"]
    if not job.plain_int(code):
        detail = f"carried an error code that cannot be read ({type(code).__name__})"
        listing = await _read_listing(transport, name)
        return outcome(ControlStatus.AMBIGUOUS,
                       (job.UNREADABLE_NOTE.format(verb="delete", detail=detail),),
                       listing, file_now=_file_now(listing))
    listing = await _read_listing(transport, name)
    if code != 0:
        return outcome(ControlStatus.REFUSED, (delete_refusal_note(code),), listing,
                       file_now=_file_now(listing), code=code)
    return outcome(ControlStatus.ACCEPTED,
                   (job.ACCEPT_BASE.format(verb="delete"), _ACCEPT_TAIL), listing,
                   file_now=_file_now(listing), accepted=True)


def _file_now(listing: _Listing) -> str:
    """The listing's verdict as the result word. A reply that lists the name says it is there —
    that needs no certification — and a reply that does not list it says anything only if it
    accounted for its own files."""
    if listing.listed == _LISTED:
        return FILE_NOW_PRESENT
    if listing.listed == _NOT_LISTED:
        return FILE_NOW_GONE
    return FILE_NOW_UNKNOWN

