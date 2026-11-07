"""The shared CC2 session pool: who may hold it, when it is replaced, and how it gets closed.

Every test here is a scripted session and a scripted configuration — no printer, no broker, no
socket, no credential of anyone's. What they hold is the behaviour the CC2 tools now depend on: one
registration for a run of calls, concurrent callers sharing it without duplicating a command, a
session that died being dropped rather than resurrected, a configuration that moved ending it, and a
cancelled or shutting-down caller leaving nothing behind.

The wire itself — that two requests over one real MQTT connection really do get their own answers —
is `tests/test_cc2_transport_mqtt.py`, which drives the same pool against a stub broker on loopback.
"""
from __future__ import annotations

import asyncio
import copy

import pytest

from orcaslicer_mcp.printer import cc2_pool as pool_mod
from orcaslicer_mcp.printer import cc2_transport as cc2
from orcaslicer_mcp.printer.errors import PrinterError

HOST = "192.0.2.10"
OTHER = "192.0.2.11"
CODE = "123456"
PW = "scripted-session"


# --------------------------------------------------------------------------- the scripted session


class FakeSession:
    """A registered session as the pool sees it: it records what it was asked for and when it was
    closed, it can be made to answer slowly (so concurrency is real rather than imagined), and it can
    be made to report its own death exactly the way `Cc2Transport.last_error` does."""

    def __init__(self, *, answer_after: float = 0.0) -> None:
        self.host, self.sn, self.display = HOST, "CC2POOLTEST01", "CC2-pool"
        self.answer_after = answer_after
        self.requests: list[tuple[int, dict]] = []
        self.closes = 0
        self.closed = False
        self.last_error: PrinterError | None = None

    async def request(self, method: int, params: dict | None = None, *, timeout_s=None) -> dict:
        self.requests.append((method, copy.deepcopy(params or {})))
        if self.answer_after:                       # answers arrive out of order on purpose
            await asyncio.sleep(self.answer_after * (4 - (method % 4)))
        return {"method": method, **copy.deepcopy(params or {})}

    async def close(self) -> None:
        self.closes += 1
        self.closed = True

    def die(self, because: str = "dropped") -> None:
        """The transport's own way of saying it is over: a structured error, kept, never cleared."""
        self.last_error = PrinterError("not_reachable",
                                       f"The MQTT session with the printer at {self.host} {because}. "
                                       "A command sent just before this may or may not have reached "
                                       "the printer; nothing will be retried.",
                                       reason="lost", ambiguous=True)

    def sent(self, method: int) -> int:
        return sum(1 for m, _p in self.requests if m == method)


class Configs:
    """The configuration the pool reads on every borrow, held by the test so it can move: a new
    address, a new code, or a configuration that cannot be read at all."""

    def __init__(self, host: str = HOST, code: str | None = CODE) -> None:
        self.calls = 0
        self.host = host
        self.code = code
        self.raises: PrinterError | None = None

    def __call__(self) -> cc2.Cc2Config:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return cc2.Cc2Config(host=self.host,
                             access_code=cc2.AccessCode(self.code) if self.code else None)


class Opens:
    """The seam that opens a fresh session: counts every call, hands back a recorded session, and can
    be made slow (to prove a second caller waits rather than registering again) or failing."""

    def __init__(self, *, raises: BaseException | None = None, gate: asyncio.Event | None = None,
                 answer_after: float = 0.0) -> None:
        self.calls = 0
        self.made: list[FakeSession] = []
        self.raises = raises
        self.gate = gate
        self.answer_after = answer_after

    async def __call__(self, config: cc2.Cc2Config) -> tuple[FakeSession, str]:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.raises is not None:
            raise self.raises
        session = FakeSession(answer_after=self.answer_after)
        self.made.append(session)
        return session, PW

    @property
    def last(self) -> FakeSession:
        return self.made[-1]


_POOL_KEYS = ("idle_close_s",)


def build(**kw) -> tuple[pool_mod.Cc2SessionPool, Configs, Opens]:
    """A pool over a scripted config and a scripted open. Pool keyword arguments (`idle_close_s`) and
    seam keyword arguments (`raises`, `gate`, `answer_after`) are told apart by name."""
    configs = Configs(kw.pop("host", HOST), kw.pop("code", CODE))
    pool_kw = {k: kw.pop(k) for k in list(kw) if k in _POOL_KEYS}
    opens = Opens(**kw)
    return pool_mod.Cc2SessionPool(configs, opens, **pool_kw), configs, opens


async def borrow(pool) -> tuple[FakeSession, pool_mod.PooledSession]:
    handle = await pool.acquire()
    return handle.transport, handle


# --------------------------------------------------------------------------- one session, many calls


async def test_a_run_of_calls_shares_one_session_and_one_registration():
    """The fault this exists for, in one test: four ordinary calls used to mean four registrations,
    and a printer whose broker takes about four clients refused the fourth. Now: one open, one
    session, every call answered by it, and the hold given back each time."""
    pool, _configs, opens = build()
    sessions = []
    for _ in range(4):
        handle = await pool.acquire()
        await handle.transport.request(1002)
        sessions.append(handle.transport)
        await handle.release()
    assert opens.calls == 1 and len(opens.made) == 1
    assert len({id(s) for s in sessions}) == 1      # the same session, four times
    assert opens.last.sent(1002) == 4                  # and every call was answered by it
    assert opens.last.closed is False                  # nobody closed it mid-run
    assert pool.holds == 0 and pool.open is True       # it waits for the next call, briefly


async def test_a_second_caller_waits_for_the_open_in_flight_rather_than_registering_again():
    """Two calls that arrive at the same moment must not each register: the printer's roster is the
    scarce thing, and two registrations from one process is the exact shape that filled it. Only the
    opening is serialised; the second caller shares what the first opened."""
    gate = asyncio.Event()
    pool, _configs, opens = build(gate=gate)
    first = asyncio.create_task(pool.acquire())
    second = asyncio.create_task(pool.acquire())
    await asyncio.sleep(0.02)                          # both are now at the pool's door
    assert opens.calls == 1                            # exactly one got through to open
    gate.set()
    a, b = await asyncio.gather(first, second)
    assert a.transport is b.transport and opens.calls == 1
    assert pool.holds == 2                             # both hold it, and both will give it back
    await a.release()
    await b.release()
    assert pool.holds == 0


async def test_concurrent_callers_on_one_session_get_their_own_answers():
    """Reply correlation is what makes sharing safe: the requests are issued together and answered
    out of order, and each caller still receives the reply to the request it sent. The transport does
    this by id; nothing in the pool may make it happen some other way."""
    pool, _configs, opens = build(answer_after=0.005)
    handle = await pool.acquire()
    try:
        methods = [1021, 1022, 1029, 1031]
        answers = await asyncio.gather(*[handle.transport.request(m, {"n": m}) for m in methods])
    finally:
        await handle.release()
    assert [a["method"] for a in answers] == methods       # nobody was handed a stranger's reply
    assert [a["n"] for a in answers] == methods
    assert opens.calls == 1
    assert sorted(m for m, _p in handle.transport.requests) == sorted(methods)


async def test_concurrent_writes_leave_exactly_one_frame_each_on_the_wire():
    """The safety half of sharing: two callers issuing control commands at the same time must produce
    one frame per call and never a third copy of either. Nothing here sends anything twice, and a
    shared session is not a reason to duplicate a command."""
    pool, _configs, opens = build(answer_after=0.002)
    handles = [await pool.acquire() for _ in range(3)]
    try:
        await asyncio.gather(handles[0].transport.request(1021, {"why": "pause"}),
                             handles[1].transport.request(1022, {"why": "stop"}),
                             handles[2].transport.request(1021, {"why": "pause again"}))
    finally:
        for h in handles:
            await h.release()
    wire = [(m, p["why"]) for m, p in opens.last.requests]
    assert wire.count((1021, "pause")) == 1 and wire.count((1021, "pause again")) == 1
    assert wire.count((1022, "stop")) == 1
    assert len(opens.last.requests) == 3 and opens.calls == 1


async def test_releasing_twice_is_a_no_op_and_never_closes_a_session_someone_else_holds():
    """A double release is a bug in the caller, but it must not become a closed session under another
    caller's feet — and it must not underflow the count into a session that closes itself early."""
    pool, _configs, opens = build()
    a = await pool.acquire()
    b = await pool.acquire()
    await a.release()
    await a.release()                              # the second release does nothing
    assert pool.holds == 1 and opens.last.closed is False
    await b.release()
    assert pool.holds == 0 and opens.last.closed is False


# --------------------------------------------------------------------------- what ends a session


async def test_a_session_that_died_is_not_lent_out_again_and_the_next_call_opens_a_fresh_one():
    """A session the transport reported dead can only refuse, so the next call gets a new one. This
    is not a retry: the pool sends nothing, and the command that was in flight when it died stays as
    ambiguous as the reply that reported the death said it was."""
    pool, _configs, opens = build()
    first, handle = await borrow(pool)
    await handle.release()
    first.die()
    _second, second_handle = await borrow(pool)
    assert opens.calls == 2 and len(opens.made) == 2
    assert second_handle.transport is not first
    assert first.closed is True                    # the dead one is given back to the printer
    assert second_handle.transport.closed is False
    await second_handle.release()


async def test_a_death_detected_on_release_closes_the_session_immediately():
    """The last holder leaving a dead session is not a reason to keep it: an untrusted session that
    stays open is one more dead client in the printer's roster."""
    pool, _configs, _opens = build()
    session, handle = await borrow(pool)
    session.die()
    await handle.release()
    assert session.closed is True and pool.open is False and pool.holds == 0


async def test_a_session_someone_closed_out_of_band_is_never_lent_out():
    """`closed` and `died` are two different facts and both rule a session out; a session someone
    closed is not handed to the next caller just because it has no error on it."""
    pool, _configs, opens = build()
    session, handle = await borrow(pool)
    await handle.release()
    await session.close()
    _next, next_handle = await borrow(pool)
    assert opens.calls == 2 and next_handle.transport is not session
    await next_handle.release()


async def test_a_changed_address_or_a_changed_code_ends_the_session_it_left_behind():
    """Re-pointing the server, or changing the access code, cannot leave a tool talking through the
    old session — which is why the configuration is re-read on every borrow rather than remembered."""
    for change in ("address", "code", "no_code"):
        pool, configs, opens = build()
        first, handle = await borrow(pool)
        await handle.release()
        if change == "address":
            configs.host = OTHER
        elif change == "code":
            configs.code = "654321"
        else:
            configs.code = None
        second, second_handle = await borrow(pool)
        assert opens.calls == 2, change
        assert second is not first, change
        assert first.closed is True, change        # the abandoned session is closed, not kept
        await second_handle.release()


async def test_the_same_address_and_code_do_not_look_like_a_change():
    """The other direction matters too: re-reading an unchanged configuration is not a reason to pay
    for a new registration, however the config object was rebuilt."""
    pool, configs, opens = build()
    first, handle = await borrow(pool)
    await handle.release()
    configs.host, configs.code = HOST, CODE
    second, second_handle = await borrow(pool)
    assert opens.calls == 1 and second is first
    await second_handle.release()


async def test_a_configuration_that_cannot_be_read_borrows_nothing_and_opens_nothing():
    """A missing address or an unreadable code file is refused by the pool before it lends or opens:
    the refusal is a fact about this process's settings, and no session — old or new — is involved."""
    pool, configs, opens = build()
    session, handle = await borrow(pool)
    await handle.release()
    configs.raises = PrinterError("not_configured", "No CC2 printer address is set (ORCA_CC2_URL is empty).")
    with pytest.raises(PrinterError) as e:
        await pool.acquire()
    assert e.value.code == "not_configured"
    assert opens.calls == 1                        # nothing new was opened for the failed call
    assert pool.holds == 0
    # A refusal about this process's own settings is not evidence about the session, so it is not an
    # excuse to tear one down either: what retires a session is its own health, a configuration that
    # changed, the idle window or shutdown. Here the session waits for the configuration to come
    # back, and if it outlives its welcome the idle window closes it like any other unused one.
    assert session.closed is False and pool.open is True
    configs.raises = None
    _after, after_handle = await borrow(pool)       # and the tools work again, on the same session
    assert after_handle.transport is session and opens.calls == 1
    await after_handle.release()


async def test_an_open_that_fails_leaves_nothing_to_borrow_and_is_never_retried_inside_the_call():
    """One open per call, whatever it costs. A failed open is reported as itself; the pool keeps
    nothing half-built, and the next call makes its own single attempt."""
    pool, _configs, opens = build(raises=PrinterError("not_reachable", "simulated: discovery silence"))
    with pytest.raises(PrinterError) as e:
        await pool.acquire()
    assert e.value.code == "not_reachable"
    assert pool.open is False and pool.holds == 0 and opens.calls == 1
    with pytest.raises(PrinterError):
        await pool.acquire()
    assert opens.calls == 2                        # one attempt per call, no hidden loop


# --------------------------------------------------------------------------- cancellation, idle, shutdown


async def test_a_cancelled_call_gives_its_hold_back_and_leaves_the_session_for_the_next():
    """A tool call cancelled by its client is an ordinary ending: the count goes back, and a healthy
    session another agent might want stays open rather than being closed by a caller that left."""
    pool, _configs, opens = build()
    started = asyncio.Event()

    async def holder():
        handle = await pool.acquire()
        started.set()
        try:
            await asyncio.Event().wait()           # until this task is cancelled
        finally:
            await handle.release()

    task = asyncio.create_task(holder())
    await started.wait()
    assert pool.holds == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.holds == 0 and opens.last.closed is False and pool.open is True
    _next, next_handle = await borrow(pool)        # borrowable again, and the same session
    assert next_handle.transport is opens.last
    await next_handle.release()


async def test_cancelling_one_holder_never_closes_a_session_another_holder_is_using():
    """The concurrency rule that keeps a shared session safe: leaving does not decide the fate of a
    session someone else is still in."""
    pool, _configs, opens = build()
    started = asyncio.Event()
    keep = await pool.acquire()

    async def holder():
        handle = await pool.acquire()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await handle.release()

    task = asyncio.create_task(holder())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.holds == 1 and opens.last.closed is False
    await keep.transport.request(1002)             # the surviving holder has a live session
    await keep.release()
    assert pool.holds == 0


async def test_a_session_that_died_while_its_last_holder_was_cancelled_still_gets_closed():
    """A cancelled teardown must not strand a dead session open. The close is shielded, so the
    caller's cancellation ends its own wait and the close still finishes."""
    pool, _configs, opens = build()
    started = asyncio.Event()

    async def holder():
        handle = await pool.acquire()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            handle.transport.die()
            await handle.release()

    task = asyncio.create_task(holder())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)                      # the shielded close finishes on its own
    assert opens.last.closed is True and pool.open is False and pool.holds == 0


async def test_a_configuration_change_while_a_call_holds_the_session_waits_for_that_call():
    """Pulling a session out from under a caller mid-request is worse than holding a slot a moment
    longer: the retired session closes when the call that was using it ends, and the next call opens
    a new one immediately."""
    pool, configs, opens = build()
    first, holder = await borrow(pool)             # someone is still inside a tool body
    configs.host = OTHER
    second, next_holder = await borrow(pool)
    assert second is not first and opens.calls == 2
    assert first.closed is False                   # not closed while `holder` is in it
    await next_holder.release()
    await holder.release()
    assert first.closed is True and pool.holds == 0


async def test_an_idle_session_gives_the_printer_its_slot_back():
    """Nobody is using it and nobody came: the window closes and the client leaves the broker. The
    window is this server's policy, so the test sets its own short one."""
    pool, _configs, _opens = build(idle_close_s=0.02)
    session, handle = await borrow(pool)
    await handle.release()
    assert session.closed is False                 # not the instant the last caller left
    await asyncio.sleep(0.1)
    assert session.closed is True and pool.open is False


async def test_a_session_that_is_asked_for_again_inside_the_window_stays_open():
    """The whole point of the window: a call arriving before it expires does not pay a new
    registration, and an unexpired timer must not close a session that is being used."""
    pool, _configs, opens = build(idle_close_s=0.05)
    session, handle = await borrow(pool)
    await handle.release()
    await asyncio.sleep(0.02)
    again = await pool.acquire()
    await asyncio.sleep(0.09)                      # well past the original window
    assert again.transport is session and session.closed is False and opens.calls == 1
    await again.release()
    await asyncio.sleep(0.1)
    assert session.closed is True                  # and it does expire once nobody is in it


async def test_shutdown_closes_the_shared_session_and_a_later_call_opens_a_fresh_one():
    """Closing the server must not leave a client the printer has to time out on its own. It is not a
    switch: a later call simply opens again."""
    pool, _configs, opens = build()
    session, handle = await borrow(pool)
    await handle.release()
    await pool.close()
    assert session.closed is True and pool.open is False
    _next, next_handle = await borrow(pool)
    assert opens.calls == 2 and next_handle.transport is not session
    await next_handle.release()
    await pool.close()


async def test_shutdown_does_not_pull_a_session_out_from_under_a_call_in_flight():
    """Shutdown is not permission to close a session somebody is standing in; that caller's own
    release does the closing."""
    pool, _configs, _opens = build()
    session, handle = await borrow(pool)
    await pool.close()
    assert session.closed is False and pool.holds == 1
    await handle.release()
    assert session.closed is True and pool.holds == 0


async def test_close_pools_closes_every_one_and_a_failing_teardown_does_not_stop_the_rest():
    """The shutdown path runs over the pools in order and never lets one bad close leave the others
    holding the printer's slots."""
    class Rude:
        def __init__(self) -> None:
            self.tried = False

        async def close(self) -> None:
            self.tried = True
            raise RuntimeError("simulated: teardown failed")

    rude_pool = pool_mod.Cc2SessionPool(Configs(), Opens())
    rude_session = Rude()
    rude_pool._slot = pool_mod._Slot(rude_session, PW, ("h", "d"))   # as good as held
    good_pool, _configs, opens = build()
    session, handle = await borrow(good_pool)
    await handle.release()
    await pool_mod.close_pools([rude_pool, good_pool])
    assert rude_session.tried is True             # it was asked, and its failure was not fatal
    assert session.closed is True and good_pool.open is False
    assert opens.calls == 1                       # and the good pool's session was never reopened


async def test_close_waits_for_a_close_that_started_as_a_background_task():
    """`close()` is the last thing the server does, so it waits for the closes already running
    instead of returning while one is still on its way to the broker."""
    pool, _configs, _opens = build()
    session, handle = await borrow(pool)
    await handle.release()
    session.die()
    second, second_handle = await borrow(pool)     # retirement started a close task on the first
    assert second is not session
    await second_handle.release()
    await pool.close()
    assert session.closed is True and second.closed is True
    assert pool._closers == set()


# --------------------------------------------------------------------------- the credential rule


async def test_the_identity_keys_a_session_without_holding_its_access_code():
    """The pool has to notice a code that changed without keeping a copy of it: the identity carries
    a salted digest, and the code appears in no identity, no repr and no message. The salt is this
    pool's own, so two pools disagree on the digest of the same code — which is the same fact, seen
    from the side: a digest leaked from one process is not a code, or even a comparable number."""
    pool, _configs, _opens = build()
    configs = Configs(HOST, CODE)
    identity = pool.identity(configs())
    assert identity[0] == HOST and CODE not in identity[1]
    joined = repr(identity) + repr(pool) + str(identity) + repr(vars(pool))
    assert CODE not in joined and PW not in joined
    other_pool = pool_mod.Cc2SessionPool(Configs(), Opens())
    assert other_pool.identity(configs()) != identity     # per-process salt, not a fingerprint
    same_pool = pool_mod.Cc2SessionPool(Configs(), Opens(), idle_close_s=1)
    assert same_pool._salt != pool._salt


async def test_no_code_configured_is_a_different_identity_from_an_empty_one():
    """'No code' and 'a code that is the empty string' are different configurations and must not share
    a session; neither is the code, and the difference is visible only as a different digest."""
    pool, _configs, _opens = build()
    no_code = pool.identity(cc2.Cc2Config(host=HOST, access_code=None))
    empty = pool.identity(cc2.Cc2Config(host=HOST, access_code=cc2.AccessCode("")))
    assert no_code != empty
    assert no_code == pool.identity(cc2.Cc2Config(host=HOST, access_code=None))


def test_the_idle_window_is_stated_where_the_printers_slots_are_documented():
    """A guard on the guard: the window is a policy, so it has to be readable and settable rather than
    buried, and a pool built without one still gets a sane default."""
    assert pool_mod.IDLE_CLOSE_S > 0
    default_pool = pool_mod.Cc2SessionPool(Configs(), Opens())
    assert default_pool._idle_s == pool_mod.IDLE_CLOSE_S


async def test_forget_is_a_test_seam_and_leaves_the_real_session_alone():
    """`forget` exists so a test can stand another scripted printer in place of the one already
    borrowed. It must not close anything (production code closes) and must not leave the abandoned
    session borrowable by the next call."""
    pool, _configs, opens = build()
    session, handle = await borrow(pool)
    await handle.release()
    pool.forget()
    assert session.closed is False and pool.open is False
    _next, next_handle = await borrow(pool)
    assert next_handle.transport is not session and opens.calls == 2
    await next_handle.release()
    assert session.closed is False       # nobody closed it: it was a scripted session, dropped

async def test_shutdown_during_open_retires_the_new_session():
    gate = asyncio.Event()
    pool, _, opens = build(gate=gate)
    acquiring = asyncio.create_task(pool.acquire())
    while not opens.calls:
        await asyncio.sleep(0)
    closing = asyncio.create_task(pool.close())
    await asyncio.sleep(0)
    gate.set()
    handle = await acquiring
    await closing
    await handle.release()
    assert opens.last.closed
    assert not pool.open

async def test_cancelled_shutdown_keeps_transport_close_running():
    pool, _, opens = build()
    handle = await pool.acquire()
    await handle.release()
    started, finish = asyncio.Event(), asyncio.Event()
    async def slow_close():
        started.set()
        await finish.wait()
        opens.last.closed = True
    opens.last.close = slow_close
    shutdown = asyncio.create_task(pool.close())
    await started.wait()
    shutdown.cancel()
    await asyncio.sleep(0)
    assert not shutdown.done()
    assert not opens.last.closed
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await shutdown
    assert opens.last.closed
    await pool.close()

def test_cancelled_anyio_scope_waits_for_cleanup_before_loop_exit():
    import anyio
    finished = []
    async def scenario():
        pool, _, opens = build()
        handle = await pool.acquire()
        await handle.release()
        async def slow_close():
            await anyio.sleep(0.01)
            finished.append(True)
        opens.last.close = slow_close
        with anyio.CancelScope() as scope:
            scope.cancel()
            await pool.close()
    anyio.run(scenario)
    assert finished == [True]

def test_cancelled_anyio_shutdown_waits_for_locked_open():
    import anyio
    closed = []
    async def scenario():
        pool, _, opens = build()
        entered = asyncio.Event()
        async def opener(config):
            entered.set()
            await anyio.sleep(0.02)
            session = FakeSession()
            opens.made.append(session)
            return session, PW
        pool._open_fn = opener
        async def borrower():
            handle = await pool.acquire()
            await handle.release()
        async with anyio.create_task_group() as group:
            group.start_soon(borrower)
            await entered.wait()
            with anyio.CancelScope() as scope:
                scope.cancel()
                await pool.close()
            closed.append(opens.last.closed)
    anyio.run(scenario)
    assert closed == [True]

async def test_close_pools_continues_after_cancelled_pool():
    calls = []
    class Pool:
        def __init__(self, cancel):
            self.cancel = cancel
        async def close(self):
            calls.append(self.cancel)
            if self.cancel:
                raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await pool_mod.close_pools([Pool(True), Pool(False)])
    assert calls == [True, False]

async def test_native_cancel_during_lock_wait_still_closes_session():
    pool, _, opens = build()
    handle = await pool.acquire()
    await handle.release()
    await pool._lock.acquire()
    shutdown = asyncio.create_task(pool_mod.close_pools([pool]))
    await asyncio.sleep(0)
    shutdown.cancel()
    await asyncio.sleep(0)
    pool._lock.release()
    with pytest.raises(asyncio.CancelledError):
        await shutdown
    assert opens.last.closed
    assert not pool.open
