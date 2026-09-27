"""Ownership races and cancellation boundaries, without network or providers."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cabin_fever_x86_core.async_utils import finish_on_cancel
from cabin_fever_x86_core.config import ServerConfig
from cabin_fever_x86_core.server import _ownership
from cabin_fever_x86_core.server._game import Game
from cabin_fever_x86_core.server._ownership import OwnershipError, SessionRegistry
from cabin_fever_x86_core.server._tools import ToolOutput


@pytest.fixture
async def registry(monkeypatch):
    games = []

    class Game:
        def __init__(self, config, send, session_id, *, user_id):
            assert not any(
                g.session_id == session_id and g.user_id == user_id and not g.closed for g in games
            )
            self.session_id, self.user_id, self.send = session_id, user_id, send
            self.closed = False
            self.stopping = asyncio.Event()
            self.release = asyncio.Event()
            self.release.set()
            self.failure = None
            games.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            self.stopping.set()
            await self.release.wait()
            if self.failure:
                raise self.failure
            self.closed = True

    monkeypatch.setattr(_ownership, "Game", Game)
    registry = SessionRegistry()
    registry.games = games
    yield registry
    for game in games:
        game.release.set()
    await registry.close()


async def acquire(registry, session_id, *, user="alice", mode="takeover", token=None):
    return await registry.acquire(
        user,
        session_id,
        SimpleNamespace(close=AsyncMock()),
        ServerConfig(),
        AsyncMock(),
        mode=mode,
        token=token,
    )


async def test_takeover_waits_for_cleanup_and_late_release_does_not_remove_replacement(registry):
    session = uuid4()
    old = await acquire(registry, session)
    old.game.release.clear()
    pending = asyncio.create_task(acquire(registry, session))
    await old.game.stopping.wait()
    assert not old.active
    assert not pending.done()
    assert len(registry.games) == 1
    # Different keys must not wait for this cleanup.
    await acquire(registry, uuid4())
    await acquire(registry, session, user="bob")
    old.game.release.set()
    new = await pending
    assert old.token != new.token
    old.connection.close.assert_awaited_once_with(code=4001, reason="Session taken over elsewhere")
    await registry.release("alice", old)
    assert registry.slots[("alice", session)].owner is new


async def test_simultaneous_takeovers_never_overlap(registry):
    session = uuid4()
    owners = await asyncio.gather(*(acquire(registry, session) for _ in range(8)))
    assert sum(owner.active for owner in owners) == 1
    assert len({owner.token for owner in owners}) == 8


async def test_recovery_retains_token_and_rejects_old_owner_even_after_new_owner_disconnects(
    registry,
):
    session = uuid4()
    first = await acquire(registry, session)
    recovered = await acquire(registry, session, mode="recover", token=first.token)
    assert recovered.token == first.token
    # Losing the recovery acknowledgement leaves the token usable.
    again = await acquire(registry, session, mode="recover", token=first.token)
    assert again.token == first.token
    new = await acquire(registry, session)
    await registry.release("alice", new)
    with pytest.raises(OwnershipError, match="Explicitly resume") as error:
        await acquire(registry, session, mode="recover", token=first.token)
    assert error.value.code == "resume_required"
    assert registry.slots[("alice", session)].owner is None
    await acquire(registry, session, mode="recover", token=new.token)


async def test_missing_wrong_user_and_restart_tokens_require_explicit_resume(registry):
    session = uuid4()
    owner = await acquire(registry, session)
    for user, token in [
        ("alice", None),
        ("alice", "unknown"),
        ("alice", "é"),
        ("bob", owner.token),
    ]:
        with pytest.raises(OwnershipError):
            await acquire(registry, session, user=user, mode="recover", token=token)
    assert owner.active
    restarted = SessionRegistry()
    with pytest.raises(OwnershipError):
        await acquire(restarted, session, mode="recover", token=owner.token)


async def test_legacy_resume_cannot_displace_active_owner(registry):
    session = uuid4()
    old = await acquire(registry, session)
    with pytest.raises(OwnershipError) as error:
        await acquire(registry, session, mode="legacy")
    assert error.value.code == "session_in_use"
    assert old.active
    await registry.release("alice", old)
    await acquire(registry, session, mode="legacy")


async def test_cleanup_failure_keeps_session_blocked(registry):
    session = uuid4()
    old = await acquire(registry, session)
    old.game.failure = RuntimeError("write failed")
    for _ in range(2):
        with pytest.raises(OwnershipError) as error:
            await acquire(registry, session)
        assert error.value.code == "cleanup_failed"
    assert len(registry.games) == 1


async def test_cleanup_timeout_reports_pending_and_retains_ownership(registry, monkeypatch):
    monkeypatch.setattr(_ownership, "CLEANUP_TIMEOUT", 0)
    session = uuid4()
    old = await acquire(registry, session)
    old.game.release.clear()
    with pytest.raises(OwnershipError) as error:
        await acquire(registry, session)
    assert error.value.code == "cleanup_pending"
    assert registry.slots[("alice", session)].owner is old
    assert len(registry.games) == 1
    old.game.release.set()
    await old.stop()
    assert (await acquire(registry, session)).active


async def test_cancelled_takeover_cannot_abandon_cleanup(registry):
    session = uuid4()
    old = await acquire(registry, session)
    old.game.release.clear()
    pending = asyncio.create_task(acquire(registry, session))
    await old.game.stopping.wait()
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    other = asyncio.create_task(acquire(registry, session))
    await asyncio.sleep(0)
    assert not pending.done() and not other.done()
    old.game.release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert (await other).active


async def test_startup_failure_cleans_partial_game_before_retry(registry, monkeypatch):
    original = _ownership.Game.__aenter__

    async def fail(self):
        raise RuntimeError("startup failed")

    monkeypatch.setattr(_ownership.Game, "__aenter__", fail)
    session = uuid4()
    with pytest.raises(RuntimeError, match="startup failed"):
        await acquire(registry, session)
    assert registry.games[0].closed
    monkeypatch.setattr(_ownership.Game, "__aenter__", original)
    assert (await acquire(registry, session)).active


async def test_cancelled_startup_finishes_and_cleans_before_another_game(registry, monkeypatch):
    starting, release = asyncio.Event(), asyncio.Event()

    async def start(self):
        starting.set()
        await release.wait()
        return self

    monkeypatch.setattr(_ownership.Game, "__aenter__", start)
    session = uuid4()
    pending = asyncio.create_task(acquire(registry, session))
    await starting.wait()
    pending.cancel()
    other = asyncio.create_task(acquire(registry, session))
    await asyncio.sleep(0)
    assert len(registry.games) == 1
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert (await other).active
    assert registry.games[0].closed


async def test_cancellation_waits_for_worker_thread_to_finish(tmp_path):
    started, release = threading.Event(), threading.Event()
    path = tmp_path / "save"

    def write():
        started.set()
        assert release.wait(5)
        path.write_text("complete")

    pending = asyncio.create_task(finish_on_cancel(asyncio.to_thread(write)))
    try:
        await asyncio.to_thread(started.wait, 5)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert path.read_text() == "complete"


async def test_operation_failure_does_not_swallow_pending_cancellation():
    started, release = asyncio.Event(), asyncio.Event()

    async def operation():
        started.set()
        await release.wait()
        raise RuntimeError("failed while being cancelled")

    pending = asyncio.create_task(finish_on_cancel(operation()))
    await started.wait()
    pending.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending


async def test_game_cleanup_waits_for_tool_state_write(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    started, release = threading.Event(), threading.Event()
    path = tmp_path / "save"

    def write():
        started.set()
        assert release.wait(5)
        path.write_text("complete")

    async def execute(_args):
        await asyncio.to_thread(write)
        return ToolOutput("saved")

    game = Game(ServerConfig(), AsyncMock())
    game._tools["test_write"] = SimpleNamespace(
        name="test_write", cabin_event_only=False, execute=execute
    )
    call = SimpleNamespace(name="test_write", arguments="{}", call_id="test")
    game._worker = asyncio.create_task(game._run_tool(call, False, parent_turn_id=uuid4()))
    await asyncio.to_thread(started.wait, 5)
    cleanup = asyncio.create_task(game.__aexit__(None, None, None))
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not cleanup.done()
        assert not path.exists()
    finally:
        release.set()
    await cleanup
    assert path.read_text() == "complete"
