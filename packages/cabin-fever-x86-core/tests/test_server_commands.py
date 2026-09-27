"""Server-side handling of session commands."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cabin_fever_x86_core.config import ServerConfig
from cabin_fever_x86_core.messages import (
    CompactionCompleted,
    CompactSessionCommand,
    NewGameCommand,
    UserMessage,
)
from cabin_fever_x86_core.server._main import CommandRefused, _run_command, handle_connection


async def test_compact_session_command_compacts_the_active_game() -> None:
    session_id = uuid4()
    game = SimpleNamespace(session_id=session_id, compact=AsyncMock())
    command = CompactSessionCommand()

    returned_game, result = await _run_command(
        command,
        game,
        ServerConfig(),
        AsyncMock(),
        AsyncMock(),
    )

    game.compact.assert_awaited_once_with()
    assert returned_game is game
    assert isinstance(result, CompactionCompleted)
    assert result.request_id == command.id
    assert result.session_id == session_id


async def test_compact_session_command_requires_an_active_game() -> None:
    with pytest.raises(CommandRefused, match="no game in progress"):
        await _run_command(
            CompactSessionCommand(),
            None,
            ServerConfig(),
            AsyncMock(),
            AsyncMock(),
        )


def staged_connection(*, pause_reply=False):
    incoming = asyncio.Queue()
    incoming.put_nowait(NewGameCommand().model_dump_json())
    waiting, sending, release_reply = asyncio.Event(), asyncio.Event(), asyncio.Event()
    if not pause_reply:
        release_reply.set()

    class Connection:
        owner = None
        user_id = "guest"
        remote_address = ("127.0.0.1", 1234)
        close = AsyncMock()
        received = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.received:
                waiting.set()
            frame = await incoming.get()
            if frame is None:
                raise StopAsyncIteration
            self.received += 1
            return frame

        async def send(self, _raw):
            sending.set()
            await release_reply.wait()

    game = SimpleNamespace(
        session_id=uuid4(), receive=AsyncMock(), compact=AsyncMock(), open_channel=AsyncMock()
    )
    owner = SimpleNamespace(game=game, active=True, token="test-token")
    registry = SimpleNamespace(acquire=AsyncMock(return_value=owner), release=AsyncMock())
    connection = Connection()
    task = asyncio.create_task(handle_connection(connection, ServerConfig(), registry))
    return SimpleNamespace(
        incoming=incoming,
        waiting=waiting,
        sending=sending,
        release_reply=release_reply,
        game=game,
        owner=owner,
        registry=registry,
        connection=connection,
        task=task,
    )


@pytest.mark.parametrize("active", [True, False])
@pytest.mark.parametrize("command", [UserMessage(content="hello"), CompactSessionCommand()])
async def test_dispatch_checks_owner_after_awaiting_next_frame(command, active):
    staged = staged_connection()
    try:
        await asyncio.wait_for(staged.waiting.wait(), 1)
        staged.game.open_channel.assert_awaited_once()
        # The iterator is suspended awaiting input when takeover retires the owner.
        staged.owner.active = active
        staged.incoming.put_nowait(command.model_dump_json())
        staged.incoming.put_nowait(None)
        await asyncio.wait_for(staged.task, 1)
        assert staged.game.receive.await_count == int(active and isinstance(command, UserMessage))
        assert staged.game.compact.await_count == int(
            active and isinstance(command, CompactSessionCommand)
        )
        staged.registry.release.assert_awaited_once_with("guest", staged.owner)
        staged.connection.close.assert_not_awaited()
    finally:
        staged.task.cancel()
        await asyncio.gather(staged.task, return_exceptions=True)


async def test_takeover_while_sending_session_reply_does_not_queue_opening_greeting():
    staged = staged_connection(pause_reply=True)
    try:
        await asyncio.wait_for(staged.sending.wait(), 1)
        staged.owner.active = False
        staged.release_reply.set()
        staged.incoming.put_nowait(None)
        await asyncio.wait_for(staged.task, 1)
        staged.game.open_channel.assert_not_awaited()
        staged.registry.release.assert_awaited_once_with("guest", staged.owner)
        staged.connection.close.assert_not_awaited()
    finally:
        staged.task.cancel()
        await asyncio.gather(staged.task, return_exceptions=True)
