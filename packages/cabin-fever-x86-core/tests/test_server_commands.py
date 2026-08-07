"""Server-side handling of session commands."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cabin_fever_x86_core.config import ServerConfig
from cabin_fever_x86_core.messages import CompactionCompleted, CompactSessionCommand
from cabin_fever_x86_core.server._main import CommandRefused, _run_command


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
