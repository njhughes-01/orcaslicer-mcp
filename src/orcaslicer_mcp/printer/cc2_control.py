"""The three CC2 job controls — pause, resume, stop — each one command at most, reported exactly
as the printer lets it be reported: accepted (an explicit ``error_code`` 0), refused (an explicit
nonzero code, kept), or ambiguous (no answer, an unreadable one, or a dead session) — and a lost
answer is never called a refusal, never called a failure, and never resent.

The wire facts are the protocol doc's and the Home Assistant integration's (v2.16.0-beta.2,
``cc2/client.py``), both local: 1021 PAUSE_PRINT, 1022 STOP_PRINT and 1023 RESUME_PRINT take no
parameters (the doc's print-methods table spells them ``None``; the integration sends ``{}``), the
error table documents 1010 NOT_PRINTING ("no active print"), the documented state machine ends a
stopped job at STOPPED, and the pause/resume sub-states (2501 pausing, 2502 paused, 2401 resuming,
2503 stopping, 2504 stopped) ride in ``machine_status.sub_status`` — reported here as numbers,
interpreted by nothing. The decode bar is ``cc2_start``'s own, applied to a command write: only an
explicit integer ``error_code`` 0 is acceptance; a missing, bool, float or text code is an
ambiguity, and any other integer is the printer declining on record.

The waits are the official Elegoo UI's own, not invented: it waits about 60 s for pause and stop
and 60–120 s for resume. Resume deserves the whole window and the honesty that comes with it: the
integration measured, on firmware 02.01.00.00, that the printer does not answer 1023 until the
resume has actually completed — it reheats, homes and returns to position first — 122 s between
the request and the ``error_code`` 0 answer. That measurement cuts both ways, and this module says
both out loud: an error_code 0 that does land inside 120 s is the printer's own completion word,
while an expiry of the window is a known-good outcome of a real resume (the measured answer came
home at 122 s, already outside it) — ambiguous, "the print may well be resuming right now", and
the one command already sent is not reconsidered. Following the transport's rule, nothing in this
module ever resends: the only follow-up a sent command may cause is one read-only 1002 after an
accepted or unanswered command, and an explicit refusal causes none.

No pre-check of the current job state stands before a command, on purpose: the printer refuses a
command that does not apply (1010 is documented for exactly that), the state can change between
one tool call and the next anyway, and the revised plan bars CC2-specific barriers that are not
required to work. Whether the command makes sense right now is decided by the human in front of
the preview; the printer's reply decides the rest.

The two-phase confirmation is the maintainer's Klipper MCP companion's convention, kept for CC2
because a client can run with permission prompts off and tool annotations are advice, not a
control: without ``confirm=True`` the tools send no control method at all — the preview path
below reads the printer exactly once (1002, read-only) and reports what a confirmed call would
send — and a confirmed call sends the one command once. That confirmation is not an access
control either: it carries the user's authorization for this action, which their standing yes for
this print or session may already supply, so it does not oblige a fresh question every time. It is
the preview that reaches a person when nothing authorizes the action yet. Privacy: nothing here takes an
address, a token or an environment variable — the transport is the pinned session — and no note
or result carries credentials or raw replies.

The acceptance vocabulary is public on purpose (``ControlStatus``, ``plain_int``, ``refusal_note``
and the note templates below): ``cc2_peripherals`` — the light (1029) and print-speed (1031)
controls — imports these words instead of rewording them, so every control tool on this server says
the same thing about acceptance, refusal, an unreadable answer and a lost one."""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

from . import cc2_state as st
from .cc2_transport import Cc2TimeoutError, PrinterError

METHOD_PAUSE = 1021    # PAUSE_PRINT — no params (the doc's table; the integration sends {})
METHOD_STOP = 1022     # STOP_PRINT  — no params
METHOD_RESUME = 1023   # RESUME_PRINT — no params

# action -> the one method it sends. The whole vocabulary this module speaks.
_METHODS: dict[str, int] = {"pause": METHOD_PAUSE, "stop": METHOD_STOP, "resume": METHOD_RESUME}

# action -> how long an answer is waited for before it counts as lost: the official UI's own
# budgets (~60 s pause/stop, up to 120 s resume). A shorter window would call an accepted command
# lost while the printer is still thinking; a longer one cannot save the measured case — the
# 122 s resume answer was already outside 120 s, and nothing is ever resent to catch it.
CONTROL_WAIT_S: dict[str, float] = {"pause": 60.0, "stop": 60.0, "resume": 120.0}

CONTROL_HINT = "Read the printer's state (cc2_status) before deciding anything about this command."


class ControlStatus(enum.StrEnum):
    """What the printer's answer to the one command supports: ACCEPTED — the explicit integer
    ``error_code`` 0; REFUSED — the printer declined on record with its code kept; AMBIGUOUS — no
    readable answer, which is neither failure nor success and never licenses a resend."""
    ACCEPTED = "accepted"
    REFUSED = "refused"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class Cc2ControlResult:
    """The whole outcome of one job-control command. ``status`` says what the printer answered and
    ``accepted`` is true only for its explicit 0; ``error_code`` keeps a refusal's number. The
    ``observed_*`` fields are one read-only 1002 taken after an accepted or unanswered command —
    never after a refusal on record, never on a dead session — and they report what the printer
    said, not what the command hoped: an accepted resume is complete in itself whether or not the
    status word already reads ``printing``. ``observed_state`` is the print-state word the start
    module reads the same way (``"printing"``, ``"idle"`` for the observed idle pair, any other
    documented word exactly as it arrives, None when the read did not land or the word is one this
    adapter does not know). ``notes`` carry what must be said out loud; ``as_dict()`` is the shape
    a tool reports."""
    action: str
    method: int
    status: ControlStatus
    waited_s: float
    accepted: bool = False
    error_code: int | None = None
    observed_state: str | None = None
    observed_job: str | None = None
    machine_status: int | None = None
    sub_status: int | None = None
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "method": self.method,
            "status": str(self.status),
            "accepted": self.accepted,
            "error_code": self.error_code,
            "waited_s": self.waited_s,
            "observed_state": self.observed_state,
            "observed_job": self.observed_job,
            "machine_status": self.machine_status,
            "sub_status": self.sub_status,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- notes, worded to the fact

# Every note here is written to fit the direct tools' reply cap in server.py (_CC2_STR_MAX), and
# NOTE_CAP below records that budget — a combined note truncated mid-sentence would lose exactly
# the tail carrying the honesty, so the multi-part messages are separate notes instead.
NOTE_CAP = 255

# Bounded by the direct tools' reply cap in server.py (_CC2_STR_MAX): this is one sentence fewer
# than it could be, because a `how` truncated mid-instruction loses the half a model acts on.
PREVIEW_HOW = ("Nothing was sent to the printer. Show this preview for the user's go-ahead unless "
               "they already authorized this action (a standing yes for this print or session "
               "counts, no fresh question), then call again with confirm=true. One action per "
               "confirmation.")

# Acceptance is "the printer did not refuse" plus a per-verb tail, because the verbs differ in
# what the answer can mean; the observation rides in its own fields and is never retro-blessed.
ACCEPT_BASE = ("Accepted means the printer answered the {verb} command with an explicit "
               "error_code 0 — its documented success answer: the printer did not refuse.")
_ACCEPT_TAILS = {
    "pause": ("What the machine is doing now is the observed fields and nothing else; they never "
              "change acceptance."),
    # The measured resume timing is this tail: an answer inside the window is the completion word,
    # so acceptance here is stronger than "queued" and weaker than "running" — and the observed
    # look stays a courtesy that nobody may demand read "printing".
    "resume": ("Measured on firmware 02.01.00.00, the printer answers 1023 only once the resume "
               "has completed (122 s to its error_code 0): a 0 inside this 120 s window is the "
               "printer's own completion word. The observed look is a courtesy, never the "
               "evidence."),
    "stop": ("Stop is the irreversible one of the three: the documented machine state ends a "
             "stopped job at STOPPED and the protocol offers no way back — pause and resume are "
             "the reversible pair, stop is not."),
}

NO_OBSERVE_AFTER_REFUSAL = ("Nothing was resent, and no status was read after an on-record "
                            "refusal — cc2_status reads on demand.")
LOST_NOTE = ("The printer did not answer the {verb} command within {wait:g}s: neither a refusal "
             "nor a failure — a refusal is an explicit answer, and there was none. The command "
             "may or may not have landed; nothing was retried. cc2_status reads its state.")
_RESUME_LOST_NOTE = ("On a real resume an expired window is an expected outcome, not bad news: "
                     "the printer answers 1023 only after the resume move has finished (measured "
                     "122 s on firmware 02.01.00.00 — past this window), so the print may well be "
                     "resuming right now.")
UNREADABLE_NOTE = ("The printer's answer to the {verb} command {detail}, which is neither success "
                   "nor refusal. Nothing was resent. " + CONTROL_HINT)
DEAD_NOTE = ("The session ended without an answer to the {verb} command — the printer may or "
             "may not have taken it, and nothing was resent, not even a read-only status, "
             "because this session can carry no more. Why it ended: ")
OBSERVE_LOST_NOTE = ("The one read-only status look this call allows itself did not land, so what "
                     "the printer is doing right now is unknown — nothing was resent (")


# --------------------------------------------------------------------------- small readers


def plain_int(value: Any) -> bool:
    """A whole protocol number as the reply's index reads: an int, and never a bool, which is an
    int in Python and not an error code on any wire (the bar cc2_start holds the 1020 answer to)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _method_or_die(action: str) -> int:
    try:
        return _METHODS[action]
    except KeyError:
        raise ValueError(f"unknown CC2 control action {action!r} — this module speaks only "
                         f"{sorted(_METHODS)}") from None


def _state_word(machine: st.Cc2MachineStatus) -> str | None:
    """The print-state word exactly as cc2_start's observation reads it: "printing", "idle" for
    the observed idle pair, any other documented word as it arrives, None when it is one this
    adapter does not know — an uncertainty, never a guess of either state."""
    if machine.print_state is st.PrintState.PRINTING:
        return "printing"
    if machine.print_state is st.PrintState.UNKNOWN:
        return None
    if machine.print_state is st.PrintState.IDLE and machine.machine_status == 1:
        return "idle"
    return machine.raw_print_state


def _observed_fields(machine: st.Cc2MachineStatus) -> dict:
    return {"observed_state": _state_word(machine), "observed_job": machine.job_filename,
            "machine_status": machine.machine_status, "sub_status": machine.sub_status}


# --------------------------------------------------------------------------- the preview (read-only)


async def read_preview(transport: Any, action: str, *, timeout_s: float | None = None) -> dict:
    """What a confirmed call would do, and what the printer says right now — the maintainer
    companion's ``confirm_required`` shape, adapted to this protocol. The 1002 status read below
    is the only method this path ever sends: no control method leaves the wire without
    ``confirm=True``, and the tests watch exactly that. A status read that fails is raised to the
    tool, not papered over — a preview built on nothing would be an invitation to confirm a
    guess."""
    method = _method_or_die(action)
    machine = await st.fetch_machine_status(transport, timeout_s=timeout_s)
    preview = {
        "will_send": {"method": method, "params": {}},
        "waits_up_to_s": CONTROL_WAIT_S[action],
        "job_state": _state_word(machine),
        "job_state_known": machine.print_state is not st.PrintState.UNKNOWN,
        "filename": machine.job_filename,
        "machine_status": machine.machine_status,
        "sub_status": machine.sub_status,
        "progress": machine.progress,
    }
    return {"confirm_required": True, "action": action, "preview": preview, "how": PREVIEW_HOW}


# --------------------------------------------------------------------------- the one command


def refusal_note(action: str, code: int) -> str:
    """A refusal in the printer's own words, kept on record: the number it sent, and for 1010 the
    documented meaning with it, because "there is no active print" is the answer a caller needs and
    not an error to interpret. Public because a peripheral refusal (1029, 1031) is the same fact with
    the same gloss and must not read as a different kind of no — and phrased without a verb that only
    fits pause/resume/stop, because "there was nothing to light" is not a sentence a printer said."""
    if code == 1010:
        head = (f"The printer refused the {action} command (error_code 1010 — the protocol's "
                f"'no active print': the printer says there was no print for it to act on).")
    else:
        head = f"The printer refused the {action} command (error_code {code})."
    return head + " It is the printer's own answer on record."


async def _observe_quietly(transport: Any) -> tuple[dict | None, str | None]:
    """Exactly one read-only 1002, or the honest note that it did not land. The only follow-up a
    sent command may ever cause; the command itself is never reconsidered."""
    try:
        return _observed_fields(await st.fetch_machine_status(transport)), None
    except PrinterError as e:
        why = getattr(e, "message", None) or str(e)
        return None, f"{OBSERVE_LOST_NOTE}{why})"


async def control_job(transport: Any, action: str, *, timeout_s: float | None = None
                      ) -> Cc2ControlResult:
    """Send the one control method for ``action``, once, and report what came back — or report,
    truthfully, that nothing readable came back. ``timeout_s`` overrides the documented wait for
    tests; nothing in this call is ever repeated, and the only extra read it may cause is the one
    1002 ``_observe_quietly`` takes after an accepted or unanswered command.

    The answer decides the words, never the hope: explicit integer 0 → ACCEPTED (per-verb honesty
    note, plus the observed look); any other integer → REFUSED with the code kept and no follow-up
    read; a missing or unreadable code → AMBIGUOUS, no resend and no read (a reply did arrive —
    what is missing is its meaning, and the caller reads state deliberately afterwards); a lost
    answer → AMBIGUOUS with the lost-answer note (resume's longer variant included), then that
    single read-only look; a dead session → AMBIGUOUS and no further read it could not carry
    anyway."""
    method = _method_or_die(action)
    wait_s = CONTROL_WAIT_S[action] if timeout_s is None else timeout_s

    def base(status: ControlStatus, **over) -> dict:
        d = {"action": action, "method": method, "waited_s": wait_s, "status": status}
        d.update(over)
        return d

    try:
        reply = await transport.request(method, {}, timeout_s=wait_s)
    except Cc2TimeoutError:
        notes = [LOST_NOTE.format(verb=action, wait=wait_s)]
        if action == "resume":
            notes.append(_RESUME_LOST_NOTE)
        observed, lost_note = await _observe_quietly(transport)
        if lost_note is not None:
            notes.append(lost_note)
        return Cc2ControlResult(**base(ControlStatus.AMBIGUOUS, **(observed or {}),
                                       notes=tuple(notes)))
    except PrinterError as e:
        why = getattr(e, "message", None) or str(e)
        return Cc2ControlResult(**base(ControlStatus.AMBIGUOUS,
                                       notes=(DEAD_NOTE.format(verb=action) + why,)))

    # ``error_code``: only an explicit integer answers. No code at all, or one that cannot be
    # read, is an ambiguity the caller resolves by reading state deliberately — never here, where
    # it would tempt a second command (the bar proven on the 1020 answer in cc2_start).
    if not isinstance(reply, dict) or "error_code" not in reply:
        detail = "carried no error code at all"
        return Cc2ControlResult(**base(ControlStatus.AMBIGUOUS,
                                       notes=(UNREADABLE_NOTE.format(verb=action, detail=detail),)))
    code = reply["error_code"]
    if not plain_int(code):
        detail = f"carried an error code that cannot be read ({type(code).__name__})"
        return Cc2ControlResult(**base(ControlStatus.AMBIGUOUS,
                                       notes=(UNREADABLE_NOTE.format(verb=action, detail=detail),)))
    if code != 0:
        return Cc2ControlResult(**base(
            ControlStatus.REFUSED, error_code=code,
            notes=(refusal_note(action, code), NO_OBSERVE_AFTER_REFUSAL)))

    observed, lost_note = await _observe_quietly(transport)
    notes = [ACCEPT_BASE.format(verb=action), _ACCEPT_TAILS[action]]
    if lost_note is not None:
        notes.append(lost_note)
    return Cc2ControlResult(**base(ControlStatus.ACCEPTED, accepted=True, **(observed or {}),
                                   notes=tuple(notes)))
