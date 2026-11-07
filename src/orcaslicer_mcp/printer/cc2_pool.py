"""One CC2 MQTT session, shared by every CC2 tool call this process makes.

Why this exists (measured on Nathan's printer, 2026-11-05). The CC2 speaks from its own MQTT broker,
and that broker takes about four clients; a slot is not handed back the instant a connection closes,
because the printer reaps a client on its own clock. Until this module every tool call opened its own
registered session — a fresh client id, a fresh registration, a close on the way out — so four
ordinary calls in a row against an idle, healthy printer produced ``reason: client_limit`` on the
fourth and a printer that then refused for a while. Every call also paid a registration it had to
clean up again, and the only advice the refusal could offer was to close other clients and wait,
which is a human doing the agent's bookkeeping. So: open once, register once, keep the heartbeat
running, and let the calls share it.

The rules this holds to, and why each one is here:

**One session, opened once.** ``acquire`` is serialised by a lock, so two calls that arrive at the
same moment cannot register twice and eat two slots — the second waits for the first's open and
shares what it opened. An established session answers any number of callers at once: the transport
correlates every answer by the request id it stamped on the wire, so concurrent requests do not
have to be serialised, only opens do.

**A dead session is dropped, never resurrected.** A session the transport itself reported dead
(``last_error``) or that someone closed is not handed to the next caller: it is retired and the next
call opens a fresh one. That is not a retry of anything. The command that was in flight when the
session died stays exactly as ambiguous as the reply that reported it said it was — this module
sends nothing, ever, and no caller's command is repeated on its way past a dead session.

**Configuration changes end the session.** The identity key is the configured address and the access
code (including a code file whose contents changed, since the config is re-read for every call), so
pointing the server at another printer, or changing the code, cannot leave a tool talking through
the old session.

**The printer gets its slot back when nobody is using it.** After the last holder leaves, an idle
session is closed once ``IDLE_CLOSE_S`` pass. That window is this server's policy, not a protocol
fact: it is long enough to cover the burst of calls one job actually makes (status, list, upload,
start, status) and short enough that a printer left alone for a while is not held open by a server
that has finished with it.

**Cancellation and shutdown are cleanups, not accidents.** The counting is synchronous, so a caller
cancelled mid-call cannot leave the pool holding a session nobody owns, and the close that follows a
retirement is shielded: the cancelled caller's teardown stays its own, and the close still finishes.

**The access code never rides along.** The pool's identity is a salted one-way digest of the code,
made with a per-process random salt that is never stored anywhere else, so it is not a copy of the
code someone holding a log could try offline, and it never reaches a reply, a message or a repr. The
code itself stays in memory for as long as the session lives — the MQTT client already holds it for
exactly as long, and an upload in the same session needs it — and only there.

**Across processes nothing is shared.** Two MCP server processes, or a second interpreter, each have
their own pool and therefore each take one of the printer's slots; two agents pointed at the same
printer are two clients, and this module changes nothing about that. It only stops one process from
being several clients to a printer it is already talking to. Within one process the same assumption
the transport already makes holds: one event loop owns the session, as it does any MQTT client — the
pool's counters and its lock are asyncio's, not thread-safe ones, and a second loop wanting the same
printer is a second client, like a second process.
"""
from __future__ import annotations

import asyncio
import anyio
import contextlib
import hashlib
import secrets
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

from .cc2_transport import Cc2Config

IDLE_CLOSE_S = 120.0   # our own policy, not the protocol's: see the module docstring


def session_unhealthy(transport: Any) -> bool:
    """Why a session in the pool may not be handed to the next caller: the transport reported its
    own death, or it was closed. Either way the next request could only meet a wall, and the honest
    answer is a fresh session for the *next* command — see the module's rule about resurrection."""
    if getattr(transport, "closed", False):
        return True
    return getattr(transport, "last_error", None) is not None


class _Slot:
    """The pool's bookkeeping for one live session: the transport, the credential its MQTT client was
    met with (memory only, and the client object beside it holds the same value anyway), how many
    callers are using it, whether it has been retired, and the timer that gives the printer its slot
    back when nobody is."""
    __slots__ = ("_timer", "holds", "identity", "password", "retired", "transport")

    def __init__(self, transport: Any, password: str | None, identity: tuple[str, str]) -> None:
        self.transport = transport
        self.password = password
        self.identity = identity
        self.holds = 0
        self.retired = False
        self._timer: asyncio.TimerHandle | None = None

    def cancel_idle(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


@dataclass
class PooledSession:
    """One caller's hold on the shared session. ``transport`` and ``password`` are the same objects
    a per-call session handed out before pooling, so nothing in the tool bodies has to know the
    difference; ``release`` gives the hold back, and giving it twice is a no-op rather than a bug
    that closes someone else's session."""
    pool: Cc2SessionPool
    slot: _Slot
    _done: bool = False

    @property
    def transport(self) -> Any:
        return self.slot.transport

    @property
    def password(self) -> str | None:
        return self.slot.password

    async def release(self) -> None:
        await self.pool.release(self)


class Cc2SessionPool:
    """Holds at most one CC2 session for one configuration and lends it to every caller.

    ``config_fn`` reads the configuration (the transport's own ``load_config``), so an address or
    code that moved between two calls is caught there, and a configuration that cannot even be read
    is refused before any session is opened or reused. ``open_fn(config)`` does the actual opening
    and returns ``(transport, password)``; the pool calls it at most once per configuration and never
    a second time for the same call — an open that fails is reported, not tried again."""

    def __init__(self, config_fn: Callable[[], Cc2Config],
                 open_fn: Callable[[Cc2Config], Awaitable[tuple[Any, str | None]]],
                 *, idle_close_s: float = IDLE_CLOSE_S) -> None:
        self._config_fn = config_fn
        self._open_fn = open_fn
        self._idle_s = max(0.0, idle_close_s)
        self._lock = asyncio.Lock()
        self._slot: _Slot | None = None
        self._closers: set[asyncio.Task] = set()
        # This pool's own salt, for the identity digest only. It never leaves this process.
        self._salt = secrets.token_bytes(16)

    # -- what the pool reports about itself ---------------------------------------------------------

    @property
    def holds(self) -> int:
        """How many callers are using the shared session right now (0 when there is none)."""
        return self._slot.holds if self._slot is not None else 0

    @property
    def open(self) -> bool:
        """Is there a session this pool is holding open?"""
        return self._slot is not None

    def identity(self, config: Cc2Config) -> tuple[str, str]:
        """The pair that says which configuration a session belongs to: the configured address, and a
        salted digest of the access code — never the code. ``token_status``/``lan_status`` are not
        part of it because they are the printer's answers, not the caller's settings, and they are
        re-checked by every open."""
        code = config.access_code
        material = b"\x00none" if code is None else b"\x00code" + code.value.encode("utf-8", "replace")
        digest = hashlib.sha256(self._salt + material).hexdigest()
        return (config.host, digest)

    # -- lending -------------------------------------------------------------------------------------

    async def acquire(self) -> PooledSession:
        """Borrow the shared session, opening it if there is nothing to borrow. Everything that
        decides *which* session a call gets happens under the lock, so concurrent callers cannot
        register twice, and a caller that arrives while an open is in progress shares what that open
        gets rather than paying for its own.

        A configuration this pool cannot read raises the transport's own refusal — before a session
        is reused and before a new one is opened, so a missing address or an unreadable code file
        means nothing was contacted on this call by anybody."""
        config = self._config_fn()
        identity = self.identity(config)
        async with self._lock:
            slot = self._slot
            if slot is not None and (slot.identity != identity or slot.retired
                                     or session_unhealthy(slot.transport)):
                await self._await(self._retire(slot))   # not this configuration's session any more
                slot = None
            if slot is None:
                transport, password = await self._open_fn(config)
                slot = _Slot(transport, password, identity)
                self._slot = slot
            else:
                slot.cancel_idle()
            slot.holds += 1
            return PooledSession(pool=self, slot=slot)

    async def release(self, session: PooledSession) -> None:
        """Give a hold back. The counting is synchronous on purpose: a caller cancelled on its way
        out of a tool body must still leave the count right, and must never close a session another
        caller is holding. A session that died during the call — the transport said so, or something
        closed it — is retired on the spot; a healthy one stays open for the next call and gets its
        idle timer."""
        slot = session.slot
        if session._done:
            return
        session._done = True
        slot.holds -= 1
        if session_unhealthy(slot.transport):
            slot.retired = True
        if slot.holds <= 0:
            if slot.retired:
                await self._await(self._retire(slot))
            else:
                self._arm_idle(slot)

    async def close(self) -> None:
        """Give the printer its slot back: close the session this pool holds and wait for any close
        still running. A later ``acquire`` opens a fresh session, so this is a courtesy to the
        printer's roster at shutdown, not a switch that turns the printer tools off. A session a
        caller still holds is left open for that caller's own release to close — closing it under it
        would be the surprise this module exists to prevent."""
        task = asyncio.create_task(self._close_all())
        cancelled = False
        with anyio.CancelScope(shield=True):
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _close_all(self) -> None:
        with anyio.CancelScope(shield=True):
            async with self._lock:
                slot = self._slot
                if slot is not None:
                    if slot.holds <= 0:
                        self._retire(slot)
                    else:
                        # Someone is standing in it. Mark it retired so their own release does the closing:
                        # closing under them would be the surprise this module exists to prevent, and leaving
                        # it un-retired would leave a client the printer has to time out on its own.
                        slot.cancel_idle()
                        slot.retired = True
            await self._drain()

    def forget(self) -> None:
        """Drop the pooled session without closing it, for a test that stands a different scripted
        printer in place of the one already borrowed. Production code closes (``close``) or retires;
        nothing that talks to a real printer uses this."""
        slot = self._slot
        self._slot = None
        if slot is not None:
            slot.cancel_idle()
            slot.retired = True

    # -- the closing ---------------------------------------------------------------------------------

    def _arm_idle(self, slot: _Slot) -> None:
        """Nobody holds it. If nobody comes for ``_idle_s``, hand the printer's slot back."""
        if slot.retired or slot._timer is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:      # no loop to be late on: the session simply stays until close()
            return
        slot._timer = loop.call_later(self._idle_s, self._on_idle, slot)

    def _on_idle(self, slot: _Slot) -> None:
        """The idle window expired. Only retire it if nothing else happened in the meantime: this
        runs on the loop, so a caller that came back already holds the session and the timer was
        cancelled, and a session that was replaced is not ours to close."""
        slot._timer = None
        if self._slot is not slot or slot.holds > 0 or slot.retired:
            return
        self._retire(slot)

    def _retire(self, slot: _Slot) -> asyncio.Task | None:
        """Take a session out of the pool and close it — unless a caller still holds it, in which
        case that caller's own release does the closing, which is the only ordering that cannot pull
        a session out from under someone using it. Returns the close to wait for, or None."""
        slot.cancel_idle()
        slot.retired = True
        if self._slot is slot:
            self._slot = None
        if slot.holds > 0:
            return None
        return self._close(slot)

    def _close(self, slot: _Slot) -> asyncio.Task:
        task = asyncio.create_task(self._close_slot(slot), name="cc2-session-close")
        self._closers.add(task)
        task.add_done_callback(self._closers.discard)
        return task

    @staticmethod
    async def _close_slot(slot: _Slot) -> None:
        with contextlib.suppress(Exception):   # cleanup never masks the failure that caused it
            await slot.transport.close()

    async def _await(self, task: asyncio.Task | None) -> None:
        """Wait for a close we started, without ever letting it fail our caller or be abandoned by
        one: the task is shielded, so a caller cancelled here leaves the close running to completion
        (it is in ``_closers``, so ``close`` will still wait for it), and a close that itself fails
        is the transport's own report, not this call's business."""
        if task is None:
            return
        with contextlib.suppress(Exception):
            await asyncio.shield(task)

    async def _drain(self) -> None:
        while self._closers:
            pending: Iterable[asyncio.Task] = tuple(self._closers)
            with anyio.CancelScope(shield=True):
                try:
                    await asyncio.gather(*(asyncio.shield(task) for task in pending), return_exceptions=True)
                except asyncio.CancelledError:
                    # Finish teardown before allowing server shutdown to end the loop.
                    await asyncio.gather(*(asyncio.shield(task) for task in pending), return_exceptions=True)
                    raise

async def close_pools(pools: Iterable[Cc2SessionPool]) -> None:
    """Close each pool in order, never letting one pool's failed teardown stop the next from giving
    the printer its slot back. Used by the server's shutdown path, where there is nothing left to
    report to anyway."""
    cancelled = False
    with anyio.CancelScope(shield=True):
        for pool in pools:
            try:
                await pool.close()
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                pass
    if cancelled:
        raise asyncio.CancelledError
