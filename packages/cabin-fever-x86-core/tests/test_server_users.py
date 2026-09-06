"""User isolation through the real server handshake and session protocol."""

import asyncio
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import InvalidStatus

from cabin_fever_x86_core.config import Config, ServerConfig
from cabin_fever_x86_core.server import _game, _main
from cabin_fever_x86_core.session_client import SessionCommandError, list_sessions, open_session
from cabin_fever_x86_core.sessions import SERVER_COMPONENT, session_dir
from cabin_fever_x86_core.telegram_gateway._main import TelegramGateway, _load_state, _state_path
from cabin_fever_x86_core.zello_gateway import _main as zello


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


async def test_telegram_uses_configured_users_for_all_session_operations(start_server):
    config = Config.model_validate(
        {
            "users": [
                {
                    "user_id": "alice",
                    "identities": [
                        {"type": "telegram", "account_id": "111"},
                        {"type": "telegram", "account_id": "112"},
                    ],
                },
                {"user_id": "bob", "identities": [{"type": "telegram", "account_id": "222"}]},
                {
                    "user_id": "guest",
                    "identities": [{"type": "guest"}, {"type": "telegram", "account_id": "333"}],
                },
            ]
        }
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    async with start_server() as uri:
        gateway = TelegramGateway(bot, uri, config.telegram_accounts())
        try:
            for account_id, user_id in [(111, "alice"), (222, "bob"), (333, "guest")]:
                await gateway._handle_text(account_id, account_id, "/start")
                session = gateway.sessions[account_id]
                assert session_dir(
                    session.session_id, SERVER_COMPONENT, user_id=user_id, create=False
                ).is_dir()
                assert session.transcript.dir == session_dir(
                    session.session_id, "telegram_gateway", user_id=user_id, create=False
                )
                assert session.transcript.path.is_file()
                assert _load_state(_state_path(user_id)) == {account_id: session.session_id}

            alice_id = gateway.sessions[111].session_id
            bob_id = gateway.sessions[222].session_id
            guest_id = gateway.sessions[333].session_id
            for account_id, session_id in [(111, alice_id), (222, bob_id), (333, guest_id)]:
                await gateway._handle_text(account_id, account_id, "/sessions")
                listing = bot.send_message.call_args.args[1]
                assert str(session_id) in listing
                assert all(
                    str(other) not in listing
                    for other in (alice_id, bob_id, guest_id)
                    if other != session_id
                )
                for command in ("/resume", "/continue", f"/resume {session_id}"):
                    await gateway._handle_text(account_id, account_id, command)
                    assert gateway.sessions[account_id].session_id == session_id
                await gateway.close_session(account_id)
                assert (
                    await gateway._ensure_session(account_id, account_id)
                ).session_id == session_id

            with pytest.raises(SessionCommandError, match="no such session"):
                await gateway._handle_text(222, 222, f"/resume {alice_id}")

            await gateway._handle_text(112, 112, "/continue")
            assert gateway.sessions[112].session_id == alice_id
            assert _load_state(_state_path("alice")) == {111: alice_id, 112: alice_id}
        finally:
            await gateway.close()


async def test_zello_shared_owner_is_used_for_creation_and_automatic_resuming(
    start_server, monkeypatch
):
    class Channel:
        def __init__(self, credentials, channel):
            assert channel == "game"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    monkeypatch.setattr(zello, "_zello_api", lambda: (object, Channel, object))
    monkeypatch.setattr(zello, "load_credentials", lambda *_args: object())
    monkeypatch.setattr(zello, "ElevenLabs", lambda **_kwargs: object())
    opened = []

    async def relay(gateway):
        assert gateway.authorized_users == {"alice", "bob"}
        assert gateway.transcript.path.is_file()
        assert gateway.transcript.dir.parts[:3] == ("data", "users", "player")
        clip = gateway.transcript.save_audio("player", "test", b"audio", "ogg")
        assert (gateway.transcript.dir / clip).read_bytes() == b"audio"
        sessions = await list_sessions(gateway.upstream)
        assert len(sessions) == 1
        opened.append(sessions[0].session_id)

    monkeypatch.setattr(zello.ZelloGateway, "run", relay)
    async with start_server() as uri:
        port = int(uri.rsplit(":", 1)[1])
        async with connect(uri) as connection:
            guest_session = await open_session(connection)

        async def run():
            await zello.run_gateway(
                "127.0.0.1",
                port,
                "keys.yaml",
                "game",
                {"alice", "bob"},
                "test-key",
                user_id="player",
                sessions=zello.ChannelSessions("player"),
            )

        await run()
        player_session = opened[0]
        assert session_dir(
            player_session, SERVER_COMPONENT, user_id="player", create=False
        ).is_dir()
        for _ in range(2):
            await run()
            assert opened[-1] == player_session
        state = zello.ChannelSessions("player")
        assert state.sessions == {"game": player_session}
        state.remember("game", guest_session)
        with pytest.raises(SessionCommandError, match="no such session"):
            await run()
        assert zello.ChannelSessions("player").sessions == {"game": guest_session}

        async with connect(uri, additional_headers={"X-CF86-User-ID": "player"}) as connection:
            assert [info.session_id for info in await list_sessions(connection)] == [player_session]
