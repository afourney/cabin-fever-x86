"""The client half of the session protocol.

Opening or resuming a game is the same conversation whichever client is
having it, so all three share this.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection

from cabin_fever_x86.messages import (
    SERVER_MESSAGE_ADAPTER,
    ErrorResult,
    ListSessionsCommand,
    NewGameCommand,
    ResumeGameCommand,
    SessionCommand,
    SessionInfo,
    SessionListResult,
    SessionResult,
)


class SessionCommandError(Exception):
    """The server refused a session command, or answered in a way we can't use."""


async def send_command(
    connection: ClientConnection, command: SessionCommand
) -> SessionResult | SessionListResult:
    """Send a session command and wait for the server's answer to it."""
    await connection.send(command.model_dump_json())

    while True:
        raw = await connection.recv()
        try:
            message = SERVER_MESSAGE_ADAPTER.validate_json(raw)
        except ValidationError as exc:
            raise SessionCommandError(f"unreadable reply from the server: {exc}") from exc

        if isinstance(message, ErrorResult):
            raise SessionCommandError(message.message)
        if isinstance(message, SessionResult | SessionListResult):
            if message.request_id != command.id:
                continue  # an answer to something else; keep waiting
            return message
        # Nothing else should arrive before a game exists; ignore it.


async def open_session(connection: ClientConnection, resume: UUID | None = None) -> UUID:
    """Start or resume a game, returning the session it opened."""
    command: SessionCommand = ResumeGameCommand(session_id=resume) if resume else NewGameCommand()
    result = await send_command(connection, command)
    if not isinstance(result, SessionResult):
        raise SessionCommandError(f"expected a session id, got {result.type!r}")
    return result.session_id


async def list_sessions(connection: ClientConnection) -> list[SessionInfo]:
    """Ask what sessions the server holds data for, most recent first."""
    result = await send_command(connection, ListSessionsCommand())
    if not isinstance(result, SessionListResult):
        raise SessionCommandError(f"expected a session list, got {result.type!r}")
    return result.sessions
