"""User isolation through the real server handshake and session protocol."""

import asyncio
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import InvalidStatus

from cabin_fever_x86_core.config import ServerConfig
from cabin_fever_x86_core.server import _game, _main
from cabin_fever_x86_core.session_client import SessionCommandError, list_sessions, open_session
from cabin_fever_x86_core.sessions import SERVER_COMPONENT, session_dir


@pytest.fixture
def no_model(monkeypatch):
    factory = Mock(return_value=(SimpleNamespace(close=AsyncMock()), "test-model"))
    monkeypatch.setattr(_game, "create_client", factory)
    monkeypatch.setattr(_game, "load_interruptions", lambda: [])
    monkeypatch.setattr(_game.Game, "open_channel", AsyncMock())
    return factory


@pytest.fixture
def start_server(tmp_path, monkeypatch, no_model):
    monkeypatch.chdir(tmp_path)

    @asynccontextmanager
    async def start():
        ready = asyncio.Event()
        uri = None

        @asynccontextmanager
        async def bound_server(*args, **kwargs):
            nonlocal uri
            async with serve(*args, **kwargs) as server:
                uri = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                ready.set()
                yield server

        monkeypatch.setattr(_main, "serve", bound_server)
        task = asyncio.create_task(_main.run_server("127.0.0.1", 0, ServerConfig()))
        task.add_done_callback(lambda _: ready.set())
        try:
            await asyncio.wait_for(ready.wait(), timeout=5)
            if task.done():
                await task
            yield uri
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    return start


@pytest.mark.parametrize(
    "headers",
    [
        {"X-CF86-User-ID": value}
        for value in ("", " ", "Alice", "../alice", "/alice", "a/b", "a.b", "a b", "a" * 65)
    ]
    + [
        [("X-CF86-User-ID", "alice"), ("x-cf86-user-id", "alice")],
        {"X-CF86-User-ID": "alice,bob"},
    ],
)
async def test_invalid_identity_rejects_handshake_before_game_creation(
    start_server, headers, tmp_path, no_model
):
    async with start_server() as uri:
        with pytest.raises(InvalidStatus) as error:
            async with connect(uri, additional_headers=headers):
                pytest.fail("invalid identity opened a connection")
        assert error.value.response.status_code == 400
    no_model.assert_not_called()
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("user_id", ["a", "alice_2-test", "a" * 64])
async def test_valid_ids_are_used_without_a_user_registry(start_server, user_id):
    async with (
        start_server() as uri,
        connect(uri, additional_headers={"x-cf86-user-id": user_id}) as client,
    ):
        session_id = await open_session(client)
        assert session_dir(session_id, SERVER_COMPONENT, create=False, user_id=user_id).is_dir()
        assert [s.session_id for s in await list_sessions(client)] == [session_id]


async def test_users_can_only_list_and_resume_their_own_sessions(start_server):
    async with start_server() as uri:
        async with connect(uri, additional_headers={"X-CF86-User-ID": "alice"}) as client:
            alice_session = await open_session(client)
        async with connect(uri) as client:
            assert await list_sessions(client) == []
            with pytest.raises(SessionCommandError, match="no such session"):
                await open_session(client, alice_session)
            guest_session = await open_session(client)
        async with connect(uri, additional_headers={"X-CF86-User-ID": "bob"}) as client:
            assert await list_sessions(client) == []
            for session_id in (alice_session, guest_session):
                with pytest.raises(SessionCommandError, match="no such session"):
                    await open_session(client, session_id)
            bob_session = await open_session(client)
            assert [s.session_id for s in await list_sessions(client)] == [bob_session]
        async with connect(uri, additional_headers={"X-CF86-User-ID": "alice"}) as client:
            assert [s.session_id for s in await list_sessions(client)] == [alice_session]
            assert await open_session(client, alice_session) == alice_session
        async with connect(uri, additional_headers={"X-CF86-User-ID": "guest"}) as client:
            assert [s.session_id for s in await list_sessions(client)] == [guest_session]
            assert await open_session(client, guest_session) == guest_session
